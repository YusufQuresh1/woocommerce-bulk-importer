#!/usr/bin/env python3
"""
WooCommerce Server-Side Ingestion Engine
=========================================
Imports products from a lightbulbs.csv export into WooCommerce via WP-CLI.

Column mappings (lightbulbs.csv):
  Title                                    → product name
  SKU                                      → SKU
  Item ID                                  → fallback SKU source
  Buy It Now Price                         → regular_price (×0.9 reduction)
  Qty Currently Listed                     → stock_quantity

Business rules:
  - Skip rows where Qty Currently Listed is 0, empty, or non-numeric.
  - Only process rows where eBay Shipping Service Cost Override List contains
    '0.00' or 'Free' (case-insensitive).
  - If Use Variations is 'True', skip and log SKU to variations_to_review.log.
  - Price = float(Buy It Now Price) * 0.9, rounded to 2 dp.
  - Every product gets meta field _import_batch = 'test_run_01'.
  - Images: extract filename from Pictures Path column (ignores drive/prefix)
    and resolve against the selected local images directory.

Usage:
  python3 engine.py                  # desktop GUI
  python3 engine.py --gui
  python3 engine.py --cli            # command-line defaults
  python3 engine.py --upload-limit 10
  python3 engine.py --dry-run
  python3 engine.py --csv /custom/path.csv --auto --debug-limit 5
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

# Increase CSV field size limit — the eBay Description column contains large
# HTML blobs that exceed Python's default 128 KB limit, causing _csv.Error.
csv.field_size_limit(10 * 1024 * 1024)  # 10 MB

import requests
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "3.1.0"

import sys
# Script location — used to resolve default relative paths.
if getattr(sys, "frozen", False):
    _SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    _SCRIPT_DIR = Path(__file__).resolve().parent

# Default paths (relative to this script: tools/ingestion/ → data/)
DEFAULT_CSV_PATH   = str(_SCRIPT_DIR / ".." / ".." / "data" / "website" / "lightbulbs.csv")
DEFAULT_XML_PATH   = str(_SCRIPT_DIR / ".." / ".." / "data" / "website" / "all products.xml")
DEFAULT_IMAGES_DIR = str(_SCRIPT_DIR / ".." / ".." / "data" / "pics")

# Batch tag applied to every imported product.
IMPORT_BATCH = "test_run_01"

# Lightbulbs CSV column names.
COL_TITLE        = "Title"
COL_SKU          = "SKU"
COL_ITEM_NUM     = "Item ID"
COL_PRICE        = "Fixed Price eBay"          # actual selling price column
COL_QTY          = "Qty Currently Listed"      # primary stock column
COL_QTY_LIST     = "Qty To List"               # secondary stock cross-check
COL_CATEGORY     = "eBay Store Category1Name"  # WooCommerce product category
COL_SHIPPING     = "eBay Shipping Service Cost Override List"
COL_VARIATIONS   = "Use Variations"
COL_PICTURES     = "Pictures Path"
COL_DESC         = "eBay Description"

# Prefix that marks dynamic attribute columns in the CSV.
IS_COLUMN_PREFIX = "IS_"

# SKU prefix used when falling back to Item ID.
ITEM_SKU_PREFIX = "ITEM-"

# Supported image extensions.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}

# WordPress attachment meta key for MD5 deduplication.
META_KEY_MD5 = "_ingestion_md5"

# Batch meta key / value.
META_KEY_BATCH = "_import_batch"

# Price multiplier: 10% UK discount.
# Currently disabled (set to 1.0) so the exact eBay price is uploaded.
# Change back to 0.9 to re-enable the 10% discount.
PRICE_MULTIPLIER = Decimal("1.0")
TWO_PLACES = Decimal("0.01")

# Number of recent log lines kept for the live dashboard feed.
STATUS_LOG_LIMIT = 5

# Variations log filename (written next to the script).
VARIATIONS_LOG = str(_SCRIPT_DIR / "variations_to_review.log")

# Next.js storefront cache invalidation.
# REVALIDATE_URL and REVALIDATE_SECRET are read dynamically at runtime (after
# load_runtime_environment() has been called) inside bust_frontend_cache().
# Set them in the .env.local beside the exe.


# ---------------------------------------------------------------------------
# Runtime Environment
# ---------------------------------------------------------------------------

def load_runtime_environment():
    """Load credentials from the repo path and, in bundled builds, beside the exe."""
    candidates = [
        _SCRIPT_DIR / ".." / ".." / ".env.local",
        Path.cwd() / ".env.local",
    ]
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend([
            executable_dir / ".env.local",
            executable_dir / ".env",
        ])

    seen = set()
    for dotenv_path in candidates:
        resolved = Path(dotenv_path).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            load_dotenv(resolved, override=False)


# ---------------------------------------------------------------------------
# Attribute Helpers
# ---------------------------------------------------------------------------

def split_attribute_values(value):
    """
    Split SixBit-style composite attribute values into separate Woo terms.
    Example: "Dimmable||Filament||Frosted" -> ["Dimmable", "Filament", "Frosted"].
    """
    return [part.strip() for part in value.split("||") if part.strip()]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_file=None, extra_handlers=None, console=True):
    handlers = []
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        handlers.append(console_handler)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        handlers.append(fh)
    if extra_handlers:
        handlers.extend(extra_handlers)

    logger = logging.getLogger("ingestion")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in handlers:
        logger.addHandler(handler)
    return logger


def open_variations_log():
    """Return an open file handle for the variations log (append mode)."""
    return open(VARIATIONS_LOG, "a", encoding="utf-8")


# ---------------------------------------------------------------------------
# WooCommerce REST API Client
# ---------------------------------------------------------------------------

class WooCommerceAPI:
    """REST API Client for WooCommerce and WordPress Media."""

    def __init__(self, dry_run=False, logger=None):
        self.dry_run = dry_run
        self.log = logger or logging.getLogger("ingestion")
        
        load_runtime_environment()
        self.url = os.environ.get("WOOCOMMERCE_URL") or os.environ.get("WORDPRESS_URL")
        if not self.url:
            self.log.error("WOOCOMMERCE_URL or WORDPRESS_URL not found in environment.")
            sys.exit(1)
        self.url = self.url.rstrip("/")
        
        # Priority 1: WordPress Application Passwords
        wp_user = os.environ.get("WORDPRESS_USER")
        wp_pass = os.environ.get("WORDPRESS_APP_PASSWORD")
        
        # Priority 2: WooCommerce Consumer Keys
        wc_key = os.environ.get("WOOCOMMERCE_CONSUMER_KEY")
        wc_secret = os.environ.get("WOOCOMMERCE_CONSUMER_SECRET")
        
        if wp_user and wp_pass:
            self.auth = (wp_user, wp_pass)
        elif wc_key and wc_secret:
            self.auth = (wc_key, wc_secret)
        else:
            self.log.error("No API credentials found in environment. Please set WOOCOMMERCE_CONSUMER_KEY/SECRET or WORDPRESS_USER/APP_PASSWORD.")
            sys.exit(1)
            
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        })

    def _request(self, method, endpoint, **kwargs):
        """Internal wrapper with rate limiting and exponential backoff for 503/504 errors."""
        time.sleep(0.5)  # Global rate limit
        
        url = f"{self.url}{endpoint}"
        if self.dry_run and method.upper() != "GET":
            self.log.info("  [DRY RUN] %s %s", method.upper(), url)
            return None
            
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = self.session.request(method, url, timeout=90, **kwargs)
                if response.status_code in (503, 504):
                    self.log.warning("  API %s on %s - attempt %d/%d", response.status_code, url, attempt, max_retries)
                    if attempt < max_retries:
                        time.sleep(2 ** attempt)
                        continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as e:
                is_term_exists = False
                err_json = None
                if getattr(e, "response", None) is not None:
                    try:
                        err_json = e.response.json()
                        if e.response.status_code == 400 and err_json.get("code") == "term_exists":
                            is_term_exists = True
                    except ValueError:
                        pass
                
                if is_term_exists:
                    return err_json
                
                self.log.error("  API error: %s %s -> %s", method.upper(), url, str(e))
                if getattr(e, "response", None) is not None and getattr(e.response, "text", None):
                    self.log.error("  API response: %s", e.response.text)
                
                if attempt < max_retries:
                    if getattr(e, "response", None) is None or getattr(e.response, "status_code", 0) in (502, 503, 504):
                        self.log.info("  Retrying API call (%d/%d)...", attempt + 1, max_retries)
                        time.sleep(2 ** attempt)
                        continue
                return None
        return None

    # -- Media ---------------------------------------------------------------

    def media_import(self, image_path, filename=None):
        import mimetypes
        if not filename:
            filename = os.path.basename(image_path)
        content_type, _ = mimetypes.guess_type(filename)
        if not content_type:
            content_type = "image/jpeg"
            
        if self.dry_run:
            self.log.info("  [DRY RUN] Upload media: %s", filename)
            return 999999
            
        try:
            with open(image_path, "rb") as f:
                headers = {
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Type": content_type
                }
                res = self._request("POST", "/wp-json/wp/v2/media", headers=headers, data=f)
                if res and "id" in res:
                    return res["id"]
        except FileNotFoundError:
            self.log.error("  File not found for media upload: %s", image_path)
        return None

    def set_attachment_meta(self, attachment_id, key, value):
        if self.dry_run:
            return
        # Assumes the WP REST API meta endpoints are available or we can update it via standard meta payload
        payload = {"meta": {key: value}}
        self._request("POST", f"/wp-json/wp/v2/media/{attachment_id}", json=payload)

    def find_attachment_by_hash(self, md5_hash):
        # Uses the custom md5_hash parameter implemented by the WPCode snippet
        res = self._request("GET", f"/wp-json/wp/v2/media?md5_hash={md5_hash}")
        if res and isinstance(res, list) and len(res) > 0:
            return res[0]["id"]
        return None

    # -- Products ------------------------------------------------------------

    def product_exists(self, sku):
        res = self._request("GET", f"/wp-json/wc/v3/products?sku={sku}")
        if res and isinstance(res, list) and len(res) > 0:
            return res[0]["id"]
        return None

    def delete_product(self, product_id):
        if self.dry_run:
            self.log.info("  [DRY RUN] Delete product %d", product_id)
            return True
        res = self._request("DELETE", f"/wp-json/wc/v3/products/{product_id}?force=true")
        return res is not None

    def create_product(self, fields):
        if self.dry_run:
            self.log.info("  [DRY RUN] Create product %s", fields.get("sku"))
            return 999999
        res = self._request("POST", "/wp-json/wc/v3/products", json=fields)
        if res and "id" in res:
            return res["id"]
        return None

    def update_product(self, product_id, fields):
        if self.dry_run:
            self.log.info("  [DRY RUN] Update product %d (%s)", product_id, fields.get("sku", "existing SKU"))
            return product_id
        res = self._request("PUT", f"/wp-json/wc/v3/products/{product_id}", json=fields)
        if res and "id" in res:
            return res["id"]
        return None

    def fetch_all_products(self):
        """Fetch all existing products from WooCommerce to build a local cache."""
        if self.dry_run:
            return {}
        
        self.log.info("Fetching all existing products from WooCommerce...")
        products_cache = {}
        page = 1
        while True:
            url = f"/wp-json/wc/v3/products?per_page=100&page={page}&_fields=id,sku,name,regular_price,stock_quantity"
            res = self._request("GET", url)
            if not res or not isinstance(res, list):
                break
            
            for p in res:
                sku = p.get("sku")
                if sku:
                    products_cache[sku] = {
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "regular_price": p.get("regular_price"),
                        "stock_quantity": p.get("stock_quantity")
                    }
            
            self.log.info("  Fetched %d products so far...", len(products_cache))
            if len(res) < 100:
                break
            page += 1
            
        self.log.info("Total existing products fetched: %d", len(products_cache))
        return products_cache

    def fetch_products_by_skus(self, skus):
        """Fetch only the specified SKUs from WooCommerce to build a target cache."""
        if self.dry_run or not skus:
            return {}
        
        self.log.info("Fetching %d target products from WooCommerce...", len(skus))
        products_cache = {}
        for idx, sku in enumerate(skus, 1):
            self.log.info("  [%d/%d] Querying WooCommerce for SKU: %s", idx, len(skus), sku)
            res = self._request("GET", f"/wp-json/wc/v3/products?sku={sku}&_fields=id,sku,name,regular_price,stock_quantity")
            if res and isinstance(res, list) and len(res) > 0:
                p = res[0]
                products_cache[sku] = {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "regular_price": p.get("regular_price"),
                    "stock_quantity": p.get("stock_quantity")
                }
        self.log.info("Fetched %d existing products for target SKUs.", len(products_cache))
        return products_cache

    def batch_operations(self, create_list, update_list):
        """Send a batch of create and update operations."""
        if self.dry_run:
            self.log.info("  [DRY RUN] Batch operation: %d creates, %d updates.", len(create_list), len(update_list))
            return True
            
        payload = {
            "create": create_list,
            "update": update_list
        }
        res = self._request("POST", "/wp-json/wc/v3/products/batch", json=payload)
        return res is not None

    def update_product_meta(self, product_id, key, value):
        if self.dry_run:
            return
        payload = {"meta_data": [{"key": key, "value": value}]}
        self._request("PUT", f"/wp-json/wc/v3/products/{product_id}", json=payload)

    # -- Taxonomy ------------------------------------------------------------

    def term_exists(self, name, taxonomy="product_cat"):
        if taxonomy != "product_cat":
            self.log.error("Taxonomy %s not supported via this specific endpoint", taxonomy)
            return None
        res = self._request("GET", f"/wp-json/wc/v3/products/categories?search={name}")
        if res and isinstance(res, list):
            for term in res:
                if term.get("name") == name:
                    return term["id"]
        return None

    def create_term(self, name, taxonomy="product_cat", parent_id=0):
        if taxonomy != "product_cat":
            return None
        if self.dry_run:
            self.log.info("  [DRY RUN] Create category %s", name)
            return 888888
        payload = {"name": name}
        if parent_id:
            payload["parent"] = parent_id
        res = self._request("POST", "/wp-json/wc/v3/products/categories", json=payload)
        if res:
            if "id" in res:
                return res["id"]
            elif res.get("code") == "term_exists" and "data" in res and "resource_id" in res["data"]:
                return res["data"]["resource_id"]
        return None


# ---------------------------------------------------------------------------
# Category Resolver
# ---------------------------------------------------------------------------

class CategoryCache:
    """In-memory cache for WooCommerce product categories."""

    def __init__(self, wp, logger):
        self.wp = wp
        self.log = logger
        self._cache = {}

    def resolve(self, category_string):
        if not category_string or not category_string.strip():
            return None
        parts = re.split(r"\s*[>:]\s*", category_string.strip())
        parts = [p.strip() for p in parts if p.strip()]
        parent_id = 0
        resolved_id = None
        for part in parts:
            cache_key = f"{part.lower()}|{parent_id}"
            if cache_key in self._cache:
                resolved_id = self._cache[cache_key]
                parent_id = resolved_id
                continue
            term_id = self.wp.term_exists(part)
            if term_id:
                self._cache[cache_key] = term_id
                resolved_id = term_id
                parent_id = term_id
                self.log.debug("  Category exists: '%s' (ID %d)", part, term_id)
                continue
            term_id = self.wp.create_term(part, parent_id=parent_id)
            if term_id:
                self._cache[cache_key] = term_id
                resolved_id = term_id
                parent_id = term_id
                self.log.info("  Created category: '%s' (ID %d, parent %d)", part, term_id, parent_id)
            else:
                self.log.warning("  Failed to create category: '%s'", part)
                return resolved_id
        return resolved_id


# ---------------------------------------------------------------------------
# Attribute Registry
# ---------------------------------------------------------------------------

class AttributeRegistry:
    """In-memory cache and register for WooCommerce global attributes and terms."""

    def __init__(self, wp, logger):
        self.wp = wp
        self.log = logger
        self.attr_cache = {}  # name -> id
        self.term_cache = {}  # attr_id -> { term_name -> term_id }
        self._load_existing_attributes()

    def _fetch_all(self, endpoint):
        results = []
        page = 1
        seen_ids = set()
        while True:
            sep = "&" if "?" in endpoint else "?"
            url = f"{endpoint}{sep}per_page=100&page={page}"
            res = self.wp._request("GET", url)
            if not res or not isinstance(res, list):
                break
                
            added_new = False
            for item in res:
                item_id = item.get("id")
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    results.append(item)
                    added_new = True
                    
            if not added_new:
                # API ignored pagination and returned the exact same page,
                # or returned an empty page. We are done.
                break
                
            if len(res) < 100:
                break
            page += 1
        return results

    def _load_existing_attributes(self):
        res = self._fetch_all("/wp-json/wc/v3/products/attributes")
        for attr in res:
            self.attr_cache[attr["name"].lower()] = attr["id"]
            self.term_cache[attr["id"]] = {}

    def get_or_create_attribute(self, name):
        name_key = name.lower()
        if name_key in self.attr_cache:
            return self.attr_cache[name_key]
        
        if self.wp.dry_run:
            self.log.info("  [DRY RUN] [ATTRIBUTE OK] Created global attribute '%s'", name)
            dummy_id = hash(name_key) % 100000 + 100000
            self.attr_cache[name_key] = dummy_id
            self.term_cache[dummy_id] = {}
            return dummy_id

        payload = {
            "name": name,
            "type": "select",
            "has_archives": True
        }
        
        # Avoid reserved WordPress slugs
        if name.lower() in {"type", "attachment", "date", "action", "order", "name", "term", "status", "author", "year", "month", "day", "hour", "minute", "second"}:
            payload["slug"] = f"item_{name.lower()[:15]}"
            
        res = self.wp._request("POST", "/wp-json/wc/v3/products/attributes", json=payload)
        if res and "id" in res:
            attr_id = res["id"]
            self.attr_cache[name_key] = attr_id
            self.term_cache[attr_id] = {}
            self.log.info("  [ATTRIBUTE OK] Created global attribute '%s' (ID %d)", name, attr_id)
            return attr_id
        return None

    def get_or_create_term(self, attr_id, term_name):
        term_key = term_name.lower()
        if not self.term_cache.get(attr_id):
            self.term_cache[attr_id] = {}
            if not self.wp.dry_run:
                res = self._fetch_all(f"/wp-json/wc/v3/products/attributes/{attr_id}/terms")
                for term in res:
                    self.term_cache[attr_id][term["name"].lower()] = term["id"]
        
        if term_key in self.term_cache[attr_id]:
            return self.term_cache[attr_id][term_key]
            
        if self.wp.dry_run:
            self.log.info("  [DRY RUN] [ATTRIBUTE OK] Registered '%s' under attribute ID %d", term_name, attr_id)
            dummy_id = hash(term_key) % 100000 + 100000
            self.term_cache[attr_id][term_key] = dummy_id
            return dummy_id

        payload = {"name": term_name}
        res = self.wp._request("POST", f"/wp-json/wc/v3/products/attributes/{attr_id}/terms", json=payload)
        if res:
            if "id" in res:
                term_id = res["id"]
                self.term_cache[attr_id][term_key] = term_id
                self.log.info("  [ATTRIBUTE OK] Registered '%s' under attribute ID %d", term_name, attr_id)
                return term_id
            elif res.get("code") == "term_exists" and "data" in res and "resource_id" in res["data"]:
                term_id = res["data"]["resource_id"]
                self.term_cache[attr_id][term_key] = term_id
                self.log.info("  [ATTRIBUTE OK] Recovered existing term '%s' (ID %d) under attribute ID %d", term_name, term_id, attr_id)
                return term_id
        return None





# ---------------------------------------------------------------------------
# High-Performance XML Streaming & Bytes Wrapper
# ---------------------------------------------------------------------------

class ProgressFileWrapper:
    """Wrapper around a file object that tracks the total bytes read for precise progress tracking."""
    def __init__(self, file_obj):
        self.file_obj = file_obj
        self.bytes_read = 0

    def read(self, size=-1):
        data = self.file_obj.read(size)
        self.bytes_read += len(data)
        return data

    def seek(self, offset, whence=0):
        res = self.file_obj.seek(offset, whence)
        if whence == 0:
            self.bytes_read = offset
        elif whence == 1:
            self.bytes_read += offset
        return res

    def tell(self):
        return self.file_obj.tell()

    def close(self):
        self.file_obj.close()


def iter_xml_rows(file_path, stop_event=None):
    """
    Incremental streaming XML parser for large product XML files.
    Yields tuples of (row_dict, bytes_read, total_size).
    Guarantees sub-50 MB RAM usage via Element.clear() and root.clear().
    """
    total_size = os.path.getsize(file_path)
    
    with open(file_path, "rb") as raw_f:
        wrapped_f = ProgressFileWrapper(raw_f)
        context = ET.iterparse(wrapped_f, events=("start", "end"))
        
        # Get the root element
        event, root = next(context)
        
        for event, elem in context:
            if stop_event and stop_event.is_set():
                break
                
            if event == "end" and elem.tag == "Item":
                # Extract and yield all flat dictionaries for this Item
                title = elem.findtext("Title", "").strip()
                item_id = elem.findtext("ItemID", "").strip()
                use_variations = elem.findtext("UseVariations", "").strip().lower()
                
                category = ""
                description = ""
                ebay_elem = elem.find("eBay")
                if ebay_elem is not None:
                    category = ebay_elem.findtext("StoreCategory1Name", "").strip()
                    description = ebay_elem.findtext("Description", "").strip()
                
                paths = []
                pictures_elem = elem.find("Pictures")
                if pictures_elem is not None:
                    for pic_elem in pictures_elem.findall("Picture"):
                        p_path = pic_elem.findtext("Path", "").strip()
                        if p_path:
                            paths.append(p_path)
                pictures_path = ";".join(paths)
                
                is_attrs = {}
                specs_elem = elem.find("ItemSpecifics")
                if specs_elem is not None:
                    selected_vals = specs_elem.find("SelectedValues")
                    if selected_vals is not None:
                        for sv in selected_vals.findall("SelectedValue"):
                            name = sv.findtext("Name", "").strip()
                            value = sv.findtext("Value", "").strip()
                            if name and value:
                                is_attrs[f"IS_{name}"] = value
                                
                vars_elem = elem.find("Variations")
                if vars_elem is not None:
                    for var in vars_elem.findall("Variation"):
                        sku = var.findtext("SKU", "").strip()
                        fixed_price = var.findtext("FixedPriceeBay", "").strip()
                        stock_total = var.findtext("StockTotal", "").strip()
                        qty_to_list = var.findtext("QtyToList", "").strip()
                        
                        row = {
                            COL_TITLE: title,
                            COL_SKU: sku,
                            COL_ITEM_NUM: item_id,
                            COL_PRICE: fixed_price,
                            COL_QTY: stock_total,
                            COL_QTY_LIST: qty_to_list,
                            COL_CATEGORY: category,
                            COL_SHIPPING: "",  # default to free shipping
                            COL_VARIATIONS: "True" if use_variations == "true" else "False",
                            COL_PICTURES: pictures_path,
                            COL_DESC: description,
                        }
                        row.update(is_attrs)
                        
                        yield row, wrapped_f.bytes_read, total_size
                
                # Free memory to guarantee constant sub-50MB usage
                elem.clear()
                root.clear()


# ---------------------------------------------------------------------------
# CSV / XML Validation
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = [COL_TITLE, COL_PRICE, COL_QTY]
OPTIONAL_COLUMNS = [
    COL_SKU, COL_ITEM_NUM, COL_QTY_LIST, COL_CATEGORY,
    COL_SHIPPING, COL_VARIATIONS, COL_PICTURES, COL_DESC,
]


def validate_headers(csv_path, logger):
    """
    Validate that required columns are present.
    For XML files, bypasses CSV header checks and validates structure compatibility.
    Returns (headers, has_sku, has_item_num).
    """
    if csv_path.lower().endswith(".xml"):
        logger.info("=== XML Column Mapping (SixBit Schema) ===")
        logger.info("  %-45s -> name", "Title")
        logger.info("  %-45s -> sku (primary)", "Variations/Variation/SKU")
        logger.info("  %-45s -> sku (fallback as ITEM-{id})", "ItemID")
        logger.info("  %-45s -> regular_price (exact eBay price)", "Variations/Variation/FixedPriceeBay")
        logger.info("  %-45s -> stock_quantity (primary)", "Variations/Variation/StockTotal")
        logger.info("  %-45s -> product category", "eBay/StoreCategory1Name")
        logger.info("  %-45s -> image paths", "Pictures/Picture/Path")
        logger.info("  %-45s -> description", "eBay/Description")
        logger.info("  IS_ attribute tags detected under ItemSpecifics/SelectedValue")
        logger.info("===========================================")
        return [], True, True

    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
            headers = next(csv.reader(f))
    except StopIteration:
        logger.error("CSV file is empty: %s", csv_path)
        sys.exit(1)

    header_set = set(h.strip() for h in headers)
    missing = [c for c in REQUIRED_COLUMNS if c not in header_set]
    if missing:
        logger.error("CSV is missing required columns: %s", missing)
        logger.error("Found columns: %s", sorted(header_set))
        sys.exit(1)

    has_sku      = COL_SKU in header_set
    has_item_num = COL_ITEM_NUM in header_set

    if not has_sku and not has_item_num:
        logger.error(
            "CSV must have '%s' or '%s' for product identification.", COL_SKU, COL_ITEM_NUM
        )
        sys.exit(1)

    logger.info("=== Column Mapping ===")
    logger.info("  %-45s -> name", COL_TITLE)
    if has_sku:
        logger.info("  %-45s -> sku (primary)", COL_SKU)
    if has_item_num:
        logger.info("  %-45s -> sku (fallback as ITEM-{id})", COL_ITEM_NUM)
    logger.info("  %-45s -> regular_price (exact eBay price)", COL_PRICE)
    logger.info("  %-45s -> stock_quantity (primary)", COL_QTY)
    logger.info("  %-45s -> product category", COL_CATEGORY)
    logger.info("  %-45s -> image paths", COL_PICTURES)
    logger.info("  %-45s -> description", COL_DESC)
    is_cols = [h for h in headers if h.strip().startswith(IS_COLUMN_PREFIX)]
    logger.info("  IS_ attribute columns detected: %d", len(is_cols))
    logger.info("=====================")

    return headers, has_sku, has_item_num


# ---------------------------------------------------------------------------
# Business Rule Filters
# ---------------------------------------------------------------------------

def passes_price_filter(row, logger, sku):
    """
    Return True only if Fixed Price eBay is a positive, parseable number.
    Rows with 0.00, empty, or non-numeric values are skipped.
    """
    raw = row.get(COL_PRICE, "").strip()
    if not raw:
        logger.debug("  SKU '%s': empty Fixed Price eBay — skipping.", sku)
        return False
    cleaned = re.sub(r"[£$€,\s]", "", raw)
    try:
        price = float(cleaned)
    except ValueError:
        logger.debug("  SKU '%s': non-numeric Fixed Price eBay '%s' — skipping.", sku, raw)
        return False
    if price <= 0:
        logger.debug("  SKU '%s': Fixed Price eBay is 0.00 — skipping.", sku)
        return False
    return True


def passes_stock_filter(row, logger, sku):
    """
    Return True only if Qty Currently Listed is > 0.
    Also logs a debug message if Qty To List reports a higher available count.
    """
    raw = row.get(COL_QTY, "").strip()
    if not raw:
        logger.debug("  SKU '%s': empty Qty Currently Listed — skipping.", sku)
        return False
    try:
        qty = float(raw)
    except ValueError:
        logger.debug("  SKU '%s': non-numeric Qty Currently Listed '%s' — skipping.", sku, raw)
        return False
    if qty <= 0:
        logger.debug("  SKU '%s': Qty Currently Listed is 0 — skipping.", sku)
        return False

    return True


def passes_shipping_filter(row, logger, sku):
    """
    Return True if the shipping override is empty (standard/free), or
    explicitly contains '0.00' or 'free' (case-insensitive).
    Only fails for non-empty values that are neither free nor 0.00.
    """
    raw = row.get(COL_SHIPPING, "").strip()
    if not raw:
        # Empty = no override set; treat as standard/free shipping.
        return True
    lower = raw.lower()
    if "0.00" in lower or "free" in lower:
        return True
    logger.debug("  SKU '%s': shipping override '%s' not free — skipping.", sku, raw)
    return False


def is_variation(row):
    """Return True if Use Variations column is literally 'True'."""
    return row.get(COL_VARIATIONS, "").strip() == "True"


# ---------------------------------------------------------------------------
# IS_ Attribute Extractor
# ---------------------------------------------------------------------------

def extract_is_attributes(row, registry):
    """
    Scan all columns that start with the IS_ prefix and return a list of
    WooCommerce attribute dicts mapped to global attributes.
    """
    attributes = []
    for col, value in row.items():
        if not col.startswith(IS_COLUMN_PREFIX):
            continue
        value = value.strip() if value else ""
        if not value:
            continue
        options = split_attribute_values(value)
        if not options:
            continue
        attr_name = col[len(IS_COLUMN_PREFIX):].strip()
        
        attr_id = registry.get_or_create_attribute(attr_name)
        if attr_id:
            for opt in options:
                registry.get_or_create_term(attr_id, opt)
            attributes.append({
                "id":        attr_id,
                "name":      attr_name,
                "options":   options,
                "visible":   True,
                "variation": False,
            })
    return attributes


# ---------------------------------------------------------------------------
# SKU Resolution
# ---------------------------------------------------------------------------

def resolve_sku(row, has_sku, has_item_num):
    """
    Return (sku, is_fallback).
    Prefers SKU column; falls back to 'ITEM-{Item ID}'.
    """
    if has_sku:
        sku = row.get(COL_SKU, "").strip()
        if sku:
            return sku, False

    if has_item_num:
        item_id = row.get(COL_ITEM_NUM, "").strip()
        if item_id:
            return f"{ITEM_SKU_PREFIX}{item_id}", True

    return None, False


# ---------------------------------------------------------------------------
# Image Path Rebasing
# ---------------------------------------------------------------------------

def rebase_image_paths(row, images_dir, logger, sku):
    """
    Extract filenames from the Pictures Path column and look for them in
    the local images_dir. Returns a list of resolved absolute paths.

    The CSV may contain absolute paths like 'F:\\pics\\61189-001.jpg' or
    multiple paths separated by semicolons. We ignore the directory component
    and only use the basename.
    """
    raw = row.get(COL_PICTURES, "").strip()
    if not raw:
        return []

    resolved = []
    entries = [p.strip() for p in raw.split(";") if p.strip()]
    for entry in entries:
        # Extract just the filename, ignoring CSV drive/directory components
        # and tolerating mixed Windows/POSIX slash styles.
        filename = os.path.basename(entry.replace("\\", os.sep).replace("/", os.sep))
        if not filename:
            continue
        candidate = os.path.join(images_dir, filename)
        if os.path.isfile(candidate):
            resolved.append(candidate)
        else:
            logger.warning("[SKIP] Image file %s not found inside the selected directory", filename)

    return resolved


# ---------------------------------------------------------------------------
# Image Resolver — MD5 Deduplication
# ---------------------------------------------------------------------------

def compute_md5(file_path, chunk_size=8192):
    md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                md5.update(chunk)
        return md5.hexdigest()
    except (FileNotFoundError, IOError):
        return None


def resolve_image(image_path, wp, logger):
    """Deduplicate, compress to WebP, and import a single image. Returns attachment ID or None."""
    md5_hash = compute_md5(image_path)
    if not md5_hash:
        logger.warning("  Could not compute MD5: %s", image_path)
        return None
    
    # Priority 1: Deduplication
    existing_id = wp.find_attachment_by_hash(md5_hash)
    if existing_id:
        logger.info(
            "  [OK] Skipped (Existing Hash) ID %d: %s",
            existing_id, os.path.basename(image_path)
        )
        return existing_id

    # Priority 2: Compression
    import tempfile
    try:
        from PIL import Image
    except ImportError:
        logger.error("  Pillow is not installed. Run 'pip install Pillow'.")
        return None

    try:
        with Image.open(image_path) as img:
            # Convert to RGB if saving as WebP from RGBA/P
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGBA")
            else:
                img = img.convert("RGB")
            
            # Resize max width to 1200
            if img.width > 1200:
                ratio = 1200 / img.width
                new_height = int(img.height * ratio)
                img = img.resize((1200, new_height), Image.Resampling.LANCZOS)
            
            # Save to temporary file as WebP
            fd, temp_path = tempfile.mkstemp(suffix=".webp")
            os.close(fd)
            
            img.save(temp_path, format="webp", quality=70, method=4)
            
            # Prepare new filename
            original_basename = os.path.basename(image_path)
            name, _ = os.path.splitext(original_basename)
            webp_filename = f"{name}.webp"
            
            attachment_id = wp.media_import(temp_path, filename=webp_filename)
            
            # Cleanup temp file
            os.remove(temp_path)
            
    except Exception as e:
        logger.warning("  Failed to convert/upload image %s: %s", image_path, e)
        return None

    # Priority 3: Metadata
    if attachment_id:
        wp.set_attachment_meta(attachment_id, META_KEY_MD5, md5_hash)
        logger.info(
            "  [OK] Uploaded (New WebP) ID %d: %s",
            attachment_id, webp_filename
        )
        return attachment_id
    
    logger.warning("  Failed to import image: %s", image_path)
    return None


# ---------------------------------------------------------------------------
# Price Transformation
# ---------------------------------------------------------------------------

def transform_price(raw_price):
    """Strip currency symbols and return the exact eBay price as a 2dp string, or None.

    The PRICE_MULTIPLIER is currently set to 1.0 (no discount).
    To re-enable the 10% UK discount, change PRICE_MULTIPLIER to Decimal("0.9").

    All arithmetic uses Python's ``decimal.Decimal`` to avoid binary floating-point
    rounding errors. The result is quantised to exactly two decimal places with
    ``ROUND_HALF_UP`` (standard commercial rounding).
    """
    if not raw_price:
        return None
    cleaned = re.sub(r"[£$€,\s]", "", str(raw_price).strip())
    try:
        price = Decimal(cleaned)
    except InvalidOperation:
        return None
    if price <= 0:
        return None
    reduced = (price * PRICE_MULTIPLIER).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return str(reduced)


# ---------------------------------------------------------------------------
# Status File
# ---------------------------------------------------------------------------

def write_status(status_file, payload):
    """Atomically write JSON status to disk (temp-file + rename)."""
    if not status_file:
        return
    tmp = status_file + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, status_file)
    except OSError:
        pass


def notify_progress(progress_callback, payload):
    """Forward progress snapshots to GUI callers without affecting ingestion."""
    if not progress_callback:
        return
    try:
        progress_callback(payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Row Processor
# ---------------------------------------------------------------------------

def process_row(
    row, has_sku, has_item_num, images_dir,
    wp, cat_cache, attr_registry, logger, dry_run, variations_log_fh, debug_counter,
    existing_products, skip_updates=False,
    update_prices=True, update_stock=True, update_images=True, update_details=True, update_attributes=True
):
    """
    Process one CSV row. Returns a tuple: (status_string, payload_dict).
    cat_cache    -- CategoryCache instance for resolving/creating WC categories.
    debug_counter -- mutable list [n] tracking dry-run validation lines printed.
    existing_products -- local dict of SKU -> Product data
    """
    # --- SKU ---
    sku, is_fallback = resolve_sku(row, has_sku, has_item_num)
    if not sku:
        logger.warning("  Row has no SKU and no Item ID — skipping.")
        return "failed", None

    # --- Variations gate ---
    if is_variation(row):
        logger.info("  SKU '%s': Use Variations=True — logged to variations file.", sku)
        variations_log_fh.write(sku + "\n")
        variations_log_fh.flush()
        return "skipped", None

    # --- Price filter (Fixed Price eBay must be > 0) ---
    if not passes_price_filter(row, logger, sku):
        return "skipped", None

    # --- Stock filter (Stock Total must be > 0) ---
    if not passes_stock_filter(row, logger, sku):
        return "skipped", None

    # --- Free postage filter ---
    if not passes_shipping_filter(row, logger, sku):
        return "skipped", None

    # --- Title ---
    name = row.get(COL_TITLE, "").strip() or f"Product {sku}"

    # --- Price (Python-calculated: Fixed Price eBay × 1.0) ---
    raw_price = row.get(COL_PRICE, "").strip()
    price = transform_price(raw_price)
    if not price:
        logger.warning("  SKU '%s': could not transform price '%s' — skipping.", sku, raw_price)
        return "failed", None

    # --- Stock quantity (primary: Qty Currently Listed) ---
    stock_qty = None
    raw_qty = row.get(COL_QTY, "").strip()
    if raw_qty:
        try:
            stock_qty = int(Decimal(raw_qty))
        except (InvalidOperation, ValueError):
            stock_qty = None

    # --- Fast Unchanged / Skip updates check ---
    existing_product = existing_products.get(sku)
    existing_id = existing_product["id"] if existing_product else None
    
    if existing_id:
        if skip_updates:
            logger.debug("  SKU '%s' already exists — skipping (Insert Only Mode).", sku)
            return "skipped", None
            
        # Fast path skip: only run if complex fields (images, attributes) are NOT being updated
        if not update_images and not update_attributes:
            old_price = str(existing_product.get("regular_price") or "")
            new_price = str(price)
            old_stock = existing_product.get("stock_quantity")
            
            price_match = (not update_prices) or (old_price == new_price)
            stock_match = (not update_stock) or (old_stock == stock_qty)
            name_match = (not update_details) or (existing_product.get("name") == name)
            
            if price_match and stock_match and name_match:
                logger.debug("  SKU '%s' unchanged — skipping (Fast Path).", sku)
                return "skipped", None

    # --- Category ---
    category_ids = []
    category_str = row.get(COL_CATEGORY, "").strip()
    if category_str:
        cat_id = cat_cache.resolve(category_str)
        if cat_id:
            category_ids.append({"id": cat_id})

    # --- IS_ Dynamic Attributes ---
    attributes = extract_is_attributes(row, attr_registry)

    # --- Dry-run validation output (first 3 rows that pass all filters) ---
    if dry_run and debug_counter[0] < 3:
        debug_counter[0] += 1
        attr_names = [a["name"] for a in attributes]
        logger.info(
            "DEBUG [%s]: Category: [%s] | Attributes: [%s]",
            sku,
            category_str or "(none)",
            ", ".join(attr_names) if attr_names else "(none)"
        )

    # --- Images (rebased from Pictures Path column) ---
    image_paths = rebase_image_paths(row, images_dir, logger, sku)
    image_ids = []
    for img_path in image_paths:
        att_id = resolve_image(img_path, wp, logger)
        if att_id:
            image_ids.append(att_id)

    # --- Build WooCommerce payload ---
    fields = {
        "name":               name,
        "sku":                sku,
        "type":               "simple",
        "status":             "publish",
        "catalog_visibility": "visible",
        "regular_price":      price,
        "manage_stock":       True,
        "meta_data":          [{"key": META_KEY_BATCH, "value": IMPORT_BATCH}]
    }
    
    description = row.get(COL_DESC, "").strip()
    if description:
        fields["description"] = description
        
    if stock_qty is not None:
        fields["stock_quantity"] = stock_qty
        fields["in_stock"] = stock_qty > 0
    if image_ids:
        fields["images"] = [{"id": iid} for iid in image_ids]
    if category_ids:
        fields["categories"] = category_ids
    if attributes:
        fields["attributes"] = attributes

    fallback_note = " [fallback SKU]" if is_fallback else ""
    if existing_id:
        update_fields = {"id": existing_id}
        if update_details:
            update_fields["name"] = name
            if description:
                update_fields["description"] = description
            if category_ids:
                update_fields["categories"] = category_ids
        if update_prices:
            update_fields["regular_price"] = price
        if update_stock:
            if stock_qty is not None:
                update_fields["stock_quantity"] = stock_qty
                update_fields["in_stock"] = stock_qty > 0
                update_fields["manage_stock"] = True
        if update_images and image_ids:
            update_fields["images"] = [{"id": iid} for iid in image_ids]
        if update_attributes and attributes:
            update_fields["attributes"] = attributes

        logger.info(
            "  [QUEUE] Update product '%s' (SKU: %s%s) [batch: %s]",
            name[:50], sku, fallback_note, IMPORT_BATCH
        )
        return "queued_update", update_fields

    logger.info(
        "  [QUEUE] Create product '%s' (SKU: %s%s) @ %s [batch: %s]",
        name[:50], sku, fallback_note, price, IMPORT_BATCH
    )
    return "queued_create", fields


# ---------------------------------------------------------------------------
# Frontend Cache Invalidation
# ---------------------------------------------------------------------------

def bust_frontend_cache(logger):
    """
    POST to the Next.js /api/revalidate endpoint to flush the product cache.
    This makes newly uploaded products appear on the storefront immediately
    rather than waiting up to 1 hour for the cache to naturally expire.

    Requires REVALIDATE_URL and REVALIDATE_SECRET to be set in the environment
    (or .env.local beside the exe). These are read dynamically here so they
    are guaranteed to be loaded after load_runtime_environment() has run.
    """
    revalidate_url    = os.environ.get("REVALIDATE_URL", "").rstrip("/")
    revalidate_secret = os.environ.get("REVALIDATE_SECRET", "")

    if not revalidate_url or not revalidate_secret:
        logger.info(
            "  [Cache] REVALIDATE_URL or REVALIDATE_SECRET not set — skipping cache invalidation."
        )
        return

    endpoint = f"{revalidate_url}/api/revalidate"
    try:
        response = requests.post(
            endpoint,
            json={"tags": ["wc-products", "wc-categories"]},
            headers={
                "x-revalidate-secret": revalidate_secret,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        if response.status_code == 200:
            logger.info(
                "  [Cache] Frontend product cache invalidated successfully — new products are live!"
            )
        else:
            logger.warning(
                "  [Cache] Revalidate endpoint returned %d: %s",
                response.status_code,
                response.text[:200],
            )
    except Exception as exc:
        logger.warning("  [Cache] Could not reach revalidate endpoint: %s", exc)


# ---------------------------------------------------------------------------
# Main Ingestion Loop
# ---------------------------------------------------------------------------

def run_ingestion(args):
    logger = setup_logging(
        args.log_file,
        extra_handlers=getattr(args, "extra_handlers", None),
        console=getattr(args, "console_log", True),
    )
    progress_callback = getattr(args, "progress_callback", None)
    logger.info("WooCommerce Ingestion Engine v%s", VERSION)
    logger.info("=" * 60)

    # --- Resolve paths ---
    csv_path   = os.path.abspath(args.csv)
    images_dir = os.path.abspath(args.images_dir)

    is_xml = csv_path.lower().endswith(".xml")
    file_label = "XML file" if is_xml else "CSV file"
    logger.info("%s:         %s", file_label, csv_path)
    logger.info("Images directory: %s",
        images_dir if os.path.isdir(images_dir) else images_dir + "  <- NOT FOUND"
    )
    logger.info("Import batch:     %s", IMPORT_BATCH)
    logger.info("Dry run:          %s", args.dry_run)
    upload_limit = max(0, int(getattr(args, "upload_limit", 0) or 0))
    if upload_limit:
        logger.info("Upload limit:     %d successful product operations", upload_limit)

    if not os.path.isfile(csv_path):
        logger.error("%s not found: %s", file_label, csv_path)
        sys.exit(1)

    if not os.path.isdir(images_dir):
        logger.warning("Images directory does not exist — no images will be attached.")

    # --- Validate headers ---
    _, has_sku, has_item_num = validate_headers(csv_path, logger)

    # --- Confirmation ---
    if not args.auto and not args.dry_run:
        response = input("\nProceed with import? [Y/n]: ").strip().lower()
        if response and response != "y":
            logger.info("Aborted by user.")
            sys.exit(0)

    # --- Count rows or init XML progress method ---
    total_rows = 0
    target_skus = []
    if not is_xml:
        with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_rows += 1
                sku, _ = resolve_sku(row, has_sku, has_item_num)
                if sku:
                    target_skus.append(sku)
        logger.info("Total rows in CSV: %d", total_rows)
    else:
        logger.info("XML Input detected. Dynamic size-based progress calculation active.")

    notify_progress(progress_callback, {
        "status":      "running",
        "total":       total_rows,
        "processed":   0,
        "imported":    0,
        "updated":     0,
        "skipped":     0,
        "failed":      0,
        "percent":     0.0,
        "recent_logs": [],
        "elapsed":     "00:00:00",
    })

    # --- Write PID file ---
    if args.pid_file:
        try:
            with open(args.pid_file, "w") as f:
                f.write(str(os.getpid()))
        except OSError:
            logger.warning("Could not write PID file: %s", args.pid_file)

    # --- Initialise API Client & Caches ---
    wp = WooCommerceAPI(dry_run=args.dry_run, logger=logger)
    cat_cache = CategoryCache(wp, logger)
    attr_registry = AttributeRegistry(wp, logger)

    if not is_xml and total_rows <= 150:
        existing_products = wp.fetch_products_by_skus(target_skus)
    else:
        existing_products = wp.fetch_all_products()

    # --- Stream and process ---
    counters = {"imported": 0, "updated": 0, "skipped": 0, "failed": 0}
    recent_logs: deque = deque(maxlen=STATUS_LOG_LIMIT)
    debug_counter = [0]   # mutable counter: tracks dry-run validation lines printed
    failed_log_fh = open(_SCRIPT_DIR / "failed_skus_to_review.log", "w", encoding="utf-8")
    start_time = time.time()
    processed_rows = 0
    stopped_by_limit = False

    batch_creates = []
    batch_updates = []
    
    def flush_batch():
        nonlocal batch_creates, batch_updates
        if not batch_creates and not batch_updates:
            return
        logger.info("Flushing batch: %d creates, %d updates...", len(batch_creates), len(batch_updates))
        success = wp.batch_operations(batch_creates, batch_updates)
        if success:
            counters["imported"] += len(batch_creates)
            counters["updated"] += len(batch_updates)
        else:
            logger.error("Batch flush failed!")
            counters["failed"] += len(batch_creates) + len(batch_updates)
            
        batch_creates.clear()
        batch_updates.clear()

    with open_variations_log() as variations_log_fh:
        last_ui_update_time = 0.0

        def get_rows_generator():
            nonlocal total_rows
            if is_xml:
                xml_gen = iter_xml_rows(csv_path, stop_event=getattr(args, "stop_event", None))
                for idx, (row, bytes_read, total_size) in enumerate(xml_gen, start=1):
                    pct = (bytes_read / total_size * 100) if total_size else 0.0
                    est_total = int((total_size / bytes_read) * idx) if bytes_read > 0 else idx
                    yield idx, row, pct, est_total
            else:
                with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
                    reader = csv.DictReader(f)
                    for idx, row in enumerate(reader, start=1):
                        pct = (idx / total_rows * 100) if total_rows else 0.0
                        yield idx, row, pct, total_rows

        row_gen = get_rows_generator()
        for i, row, pct, current_total in row_gen:
            processed_rows = i
            total_rows = current_total

            if args.debug_limit and i > args.debug_limit:
                logger.info("DEBUG_LIMIT of %d reached. Exiting gracefully.", args.debug_limit)
                break
                
            if getattr(args, "stop_event", None) and args.stop_event.is_set():
                logger.info("Stop requested by user. Exiting gracefully. You can resume later by running again.")
                break

            sku, is_fallback = resolve_sku(row, has_sku, has_item_num)
            
            # Variations Gate: Skip parent products with variations (Use Variations is True)
            if is_variation(row):
                logger.info("  SKU '%s': Use Variations=True — logged to variations file.", sku or "Unknown")
                variations_log_fh.write((sku or "Unknown") + "\n")
                variations_log_fh.flush()
                counters["skipped"] += 1
                continue

            # Variations Gate: Skip child variation rows (empty Item ID)
            item_id = row.get(COL_ITEM_NUM, "").strip()
            if not item_id:
                logger.info("  SKU '%s': Empty Item ID (child variation row) — skipping.", sku or "Unknown")
                counters["skipped"] += 1
                continue

            if not sku:
                counters["failed"] += 1
                continue
                
            # Check fast-path skip (Insert-only mode OR unchanged in Standard mode)
            existing_product = existing_products.get(sku)
            existing_id = existing_product["id"] if existing_product else None
            
            is_skipped = False
            if existing_id:
                skip_updates_active = getattr(args, "skip_updates", False)
                if skip_updates_active:
                    is_skipped = True
                else:
                    update_prices = getattr(args, "update_prices", True)
                    update_stock = getattr(args, "update_stock", True)
                    update_images = getattr(args, "update_images", True)
                    update_details = getattr(args, "update_details", True)
                    update_attributes = getattr(args, "update_attributes", True)
                    
                    if not update_images and not update_attributes:
                        name = row.get(COL_TITLE, "").strip() or f"Product {sku}"
                        raw_price = row.get(COL_PRICE, "").strip()
                        price = transform_price(raw_price)
                        
                        raw_qty = row.get(COL_QTY, "").strip()
                        stock_qty = None
                        if raw_qty:
                            try:
                                stock_qty = int(Decimal(raw_qty))
                            except (InvalidOperation, ValueError):
                                stock_qty = None
                                
                        old_price = str(existing_product.get("regular_price") or "")
                        new_price = str(price)
                        old_stock = existing_product.get("stock_quantity")
                        
                        price_match = (not update_prices) or (old_price == new_price)
                        stock_match = (not update_stock) or (old_stock == stock_qty)
                        name_match = (not update_details) or (existing_product.get("name") == name)
                        
                        if price_match and stock_match and name_match:
                            is_skipped = True
            
            if is_skipped:
                counters["skipped"] += 1
                
                # Periodically update the progress/UI so it doesn't freeze but remains extremely fast
                current_time = time.time()
                if i % 1000 == 0 or i == total_rows or (current_time - last_ui_update_time) > 1.0:
                    last_ui_update_time = current_time
                    
                    progress_msg = f"[{i}/{total_rows}] {pct:.1f}% - Fast-skipping already uploaded products..."
                    logger.info(progress_msg)
                    recent_logs.append(progress_msg)
                    
                    elapsed = current_time - start_time
                    progress_payload = {
                        "status":      "running",
                        "total":       total_rows,
                        "processed":   i,
                        "imported":    counters["imported"] + len(batch_creates),
                        "updated":     counters["updated"] + len(batch_updates),
                        "skipped":     counters["skipped"],
                        "failed":      counters["failed"],
                        "percent":     round(pct, 1),
                        "recent_logs": list(recent_logs),
                        "elapsed":     "{:02d}:{:02d}:{:02d}".format(
                            int(elapsed) // 3600,
                            (int(elapsed) % 3600) // 60,
                            int(elapsed) % 60,
                        ),
                    }
                    write_status(args.status_file, progress_payload)
                    notify_progress(progress_callback, progress_payload)
                continue

            progress_msg = f"[{i}/{total_rows}] {pct:.1f}% - Processing SKU: {sku}"
            logger.info(progress_msg)
            recent_logs.append(progress_msg)

            try:
                result, payload = process_row(
                    row, has_sku, has_item_num, images_dir,
                    wp, cat_cache, attr_registry, logger, args.dry_run, variations_log_fh, debug_counter,
                    existing_products, 
                    skip_updates=getattr(args, "skip_updates", False),
                    update_prices=getattr(args, "update_prices", True),
                    update_stock=getattr(args, "update_stock", True),
                    update_images=getattr(args, "update_images", True),
                    update_details=getattr(args, "update_details", True),
                    update_attributes=getattr(args, "update_attributes", True)
                )
                
                if result == "queued_create":
                    batch_creates.append(payload)
                elif result == "queued_update":
                    batch_updates.append(payload)
                elif result == "skipped":
                    counters["skipped"] += 1
                elif result == "failed":
                    counters["failed"] += 1
                    failed_log_fh.write(f"SKU: {sku}\n")
                    failed_log_fh.flush()
                    
                if len(batch_creates) + len(batch_updates) >= 100:
                    flush_batch()
                    
            except Exception as exc:
                err_msg = f"  Unhandled error on row {i}: {exc}"
                logger.error(err_msg)
                recent_logs.append(err_msg)
                counters["failed"] += 1
                failed_log_fh.write(f"SKU: {sku} - Error: {exc}\n")
                failed_log_fh.flush()

            current_time = time.time()
            last_ui_update_time = current_time
            elapsed = current_time - start_time
            progress_payload = {
                "status":      "running",
                "total":       total_rows,
                "processed":   i,
                "imported":    counters["imported"] + len(batch_creates),
                "updated":     counters["updated"] + len(batch_updates),
                "skipped":     counters["skipped"],
                "failed":      counters["failed"],
                "percent":     round(pct, 1),
                "recent_logs": list(recent_logs),
                "elapsed":     "{:02d}:{:02d}:{:02d}".format(
                    int(elapsed) // 3600,
                    (int(elapsed) % 3600) // 60,
                    int(elapsed) % 60,
                ),
            }
            write_status(args.status_file, progress_payload)
            notify_progress(progress_callback, progress_payload)

            successful_products = counters["imported"] + len(batch_creates)
            if upload_limit and successful_products >= upload_limit:
                logger.info(
                    "Upload limit of %d new product imports reached. Exiting gracefully.",
                    upload_limit,
                )
                stopped_by_limit = True
                break
                
        # Flush any remaining items in the batch
        flush_batch()

    # --- Summary ---
    elapsed = time.time() - start_time
    hours, remainder = divmod(int(elapsed), 3600)
    minutes, seconds = divmod(remainder, 60)

    logger.info("")
    logger.info("=" * 60)
    logger.info("INGESTION COMPLETE")
    logger.info("=" * 60)
    logger.info("  [+] Imported:      %s", f"{counters['imported']:,}")
    logger.info("  [~] Updated:       %s", f"{counters['updated']:,}")
    logger.info("  [-] Skipped:       %s", f"{counters['skipped']:,}")
    logger.info("  [x] Failed:        %s", f"{counters['failed']:,}")
    logger.info("  [>] Rows scanned:  %s / %s", f"{processed_rows:,}", f"{total_rows:,}")
    if stopped_by_limit:
        logger.info("  [>] Stop reason:   upload limit reached")
    elif getattr(args, "stop_event", None) and args.stop_event.is_set():
        logger.info("  [>] Stop reason:   stopped by user")
    logger.info("  [*] Duration:      %02d:%02d:%02d", hours, minutes, seconds)

    logger.info("  [*] Batch tag:     %s", IMPORT_BATCH)
    logger.info("  [*] Variations:    %s", VARIATIONS_LOG)
    logger.info("=" * 60)

    final_percent = (processed_rows / total_rows * 100) if total_rows else 100.0
    progress_payload = {
        "status":      "complete",
        "total":       total_rows,
        "processed":   processed_rows,
        "imported":    counters["imported"],
        "updated":     counters["updated"],
        "skipped":     counters["skipped"],
        "failed":      counters["failed"],
        "percent":     round(final_percent, 1),
        "recent_logs": list(recent_logs),
        "elapsed":     "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds),
    }
    write_status(args.status_file, progress_payload)
    notify_progress(progress_callback, progress_payload)

    if args.pid_file and os.path.exists(args.pid_file):
        try:
            os.remove(args.pid_file)
        except OSError:
            pass

    # --- Bust the frontend product cache so new products appear immediately ---
    if counters["imported"] + counters["updated"] > 0:
        bust_frontend_cache(logger)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TkQueueLogHandler(logging.Handler):
    """Logging handler that forwards formatted records to the Tk event queue."""

    def __init__(self, event_queue):
        super().__init__(logging.DEBUG)
        self.event_queue = event_queue
        self.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    def emit(self, record):
        try:
            self.event_queue.put(("log", self.format(record)))
        except Exception:
            self.handleError(record)


class IngestionGui:
    """Small Tkinter control panel for non-technical desktop ingestion runs."""

    def __init__(self, root):
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.scrolledtext = scrolledtext
        self.events = queue.Queue()
        self.worker = None
        self.running = False

        default_file = ""
        if os.path.isfile(DEFAULT_CSV_PATH):
            default_file = DEFAULT_CSV_PATH
        elif os.path.isfile(DEFAULT_XML_PATH):
            default_file = DEFAULT_XML_PATH

        self.csv_path = tk.StringVar(value=default_file)
        self.images_dir = tk.StringVar(value=DEFAULT_IMAGES_DIR if os.path.isdir(DEFAULT_IMAGES_DIR) else "")
        self.upload_limit = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="Ready")

        root.title("WooCommerce Ingestion Pipeline")
        root.minsize(760, 560)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        frame = ttk.Frame(root, padding=18)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(8, weight=1)

        ttk.Label(frame, text="Supplier CSV/XML").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        ttk.Entry(frame, textvariable=self.csv_path).grid(row=0, column=1, sticky="ew", pady=(0, 8))
        ttk.Button(frame, text="Browse", command=self.browse_csv).grid(row=0, column=2, sticky="ew", padx=(10, 0), pady=(0, 8))

        ttk.Label(frame, text="Image Folder").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(0, 14))
        ttk.Entry(frame, textvariable=self.images_dir).grid(row=1, column=1, sticky="ew", pady=(0, 14))
        ttk.Button(frame, text="Browse", command=self.browse_images_dir).grid(row=1, column=2, sticky="ew", padx=(10, 0), pady=(0, 14))

        ttk.Label(frame, text="Max Uploads").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(0, 14))
        ttk.Spinbox(frame, from_=0, to=100000, textvariable=self.upload_limit, width=12).grid(row=2, column=1, sticky="w", pady=(0, 14))
        ttk.Label(frame, text="Leave blank or 0 to process the full CSV").grid(row=2, column=2, sticky="e", pady=(0, 14))

        ttk.Label(frame, text="Import Mode").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=(0, 14))
        self.import_mode_cb = ttk.Combobox(frame, values=["Update Existing Products", "Insert New Products Only"], state="readonly")
        self.import_mode_cb.set("Update Existing Products")
        self.import_mode_cb.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(0, 14))

        # Fields to Update Options Frame (row 4)
        self.update_opts_frame = ttk.LabelFrame(frame, text="Fields to Update (for Existing Products)", padding=8)
        self.update_opts_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 14))

        self.chk_prices = tk.BooleanVar(value=True)
        self.chk_stock = tk.BooleanVar(value=True)
        self.chk_images = tk.BooleanVar(value=True)
        self.chk_details = tk.BooleanVar(value=True)
        self.chk_attributes = tk.BooleanVar(value=True)

        self.cb_prices = ttk.Checkbutton(self.update_opts_frame, text="Prices", variable=self.chk_prices)
        self.cb_stock = ttk.Checkbutton(self.update_opts_frame, text="Stock", variable=self.chk_stock)
        self.cb_images = ttk.Checkbutton(self.update_opts_frame, text="Images", variable=self.chk_images)
        self.cb_details = ttk.Checkbutton(self.update_opts_frame, text="Details", variable=self.chk_details)
        self.cb_attributes = ttk.Checkbutton(self.update_opts_frame, text="Attributes", variable=self.chk_attributes)

        self.cb_prices.pack(side="left", padx=10)
        self.cb_stock.pack(side="left", padx=10)
        self.cb_images.pack(side="left", padx=10)
        self.cb_details.pack(side="left", padx=10)
        self.cb_attributes.pack(side="left", padx=10)

        def on_mode_change(event=None):
            state = "normal" if self.import_mode_cb.get() == "Update Existing Products" else "disabled"
            self.cb_prices.configure(state=state)
            self.cb_stock.configure(state=state)
            self.cb_images.configure(state=state)
            self.cb_details.configure(state=state)
            self.cb_attributes.configure(state=state)
            
        self.import_mode_cb.bind("<<ComboboxSelected>>", on_mode_change)

        self.run_button = ttk.Button(frame, text="Run Ingestion Pipeline", command=self.start_ingestion)
        self.run_button.grid(row=5, column=0, columnspan=2, sticky="ew", ipady=8, pady=(0, 14))
        
        self.stop_button = ttk.Button(frame, text="Stop", command=self.stop_ingestion, state="disabled")
        self.stop_button.grid(row=5, column=2, sticky="ew", ipady=8, padx=(10, 0), pady=(0, 14))

        ttk.Label(frame, textvariable=self.status_text).grid(row=6, column=0, columnspan=3, sticky="w")

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(6, 12))

        self.log_text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=18, state="disabled")
        self.log_text.grid(row=8, column=0, columnspan=3, sticky="nsew")

        self.copy_button = ttk.Button(frame, text="Copy Logs to Clipboard", command=self.copy_logs)
        self.copy_button.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.help_button = ttk.Button(frame, text="View Instructions", command=self.show_instructions)
        self.help_button.grid(row=9, column=2, sticky="ew", padx=(10, 0), pady=(8, 0))

        root.after(100, self.drain_events)

    def copy_logs(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get("1.0", self.tk.END))
        self.root.update()

    def show_instructions(self):
        import tkinter as tk
        from tkinter import scrolledtext

        help_win = tk.Toplevel(self.root)
        help_win.title("Ingestion Pipeline Instructions")
        help_win.geometry("640x520")
        help_win.minsize(500, 400)

        frame = self.ttk.Frame(help_win, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        title_label = self.ttk.Label(frame, text="WooCommerce Ingestion Pipeline Help", font=("Helvetica", 12, "bold"))
        title_label.pack(anchor="w", pady=(0, 10))

        text_area = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=("Consolas", 10))
        text_area.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        instructions = """============================================================
WooCommerce Ingestion Engine Instructions
============================================================

1. INPUT FILES (Supplier CSV/XML)
   - Select either the supplier's lightbulbs CSV or the XML product feed.
   - The engine automatically resolves titles, prices, SKU, stock quantity, 
     shipping overrides, categories, and dynamic attribute fields.

2. IMAGE FOLDER
   - Select the local folder containing the product images.
   - The engine matches filenames listed in the "Pictures Path" column.
   - Images are automatically resized (max width 1200px), converted to high-efficiency 
     WebP format, and deduplicated using MD5 hashes to prevent duplicate media uploads.

3. IMPORT MODES
   - UPDATE EXISTING PRODUCTS:
     Updates catalog items that already exist in WooCommerce. You can selectively choose what to update:
     * Prices: Updates the regular price.
     * Stock: Updates stock levels and availability.
     * Images: Re-processes, WebP-compresses, and attaches supplier images.
     * Details: Updates name, description, and store categories.
     * Attributes: Updates dynamic attributes (e.g. wattage, color, material).
     
     NOTE ON SPEEDS (Auto-Skipping): 
     If Images or Attributes are checked, the engine runs a full process 
     on existing items (since we don't cache these complex fields). 
     If they are unchecked, the engine will automatically skip products 
     where name, price, and stock match to optimize speed.
   - INSERT NEW PRODUCTS ONLY:
     Skips existing products. Useful for adding new stock additions quickly.

4. MAX UPLOADS (Testing / Smoke Run)
   - Leave blank or set to 0 to run the entire feed.
   - Set to a small number (e.g., 5 or 10) to import a sample batch and test connections.

5. BUSINESS RULES & FILTERS
   - Stock: Skips items with a Stock Total of 0 or blank.
   - Shipping: Only imports items with free/override shipping (contains '0.00' or 'Free').
   - Variations: Skip parent products with variations (these are written to the
     'variations_to_review.log' next to the script) and variation child rows.
"""
        text_area.insert("1.0", instructions)
        text_area.configure(state="disabled")

        close_btn = self.ttk.Button(frame, text="Close", command=help_win.destroy)
        close_btn.pack(anchor="e")

    def browse_csv(self):
        from tkinter import filedialog

        initial = os.path.dirname(self.csv_path.get()) if self.csv_path.get() else os.getcwd()
        selected = filedialog.askopenfilename(
            title="Select supplier CSV/XML",
            initialdir=initial if os.path.isdir(initial) else os.getcwd(),
            filetypes=(("CSV/XML files", "*.csv;*.xml"), ("CSV files", "*.csv"), ("XML files", "*.xml"), ("All files", "*.*")),
        )
        if selected:
            self.csv_path.set(selected)

    def browse_images_dir(self):
        from tkinter import filedialog

        initial = self.images_dir.get() if os.path.isdir(self.images_dir.get()) else os.getcwd()
        selected = filedialog.askdirectory(title="Select product image folder", initialdir=initial)
        if selected:
            self.images_dir.set(selected)

    def stop_ingestion(self):
        if self.running and self.worker:
            self.stop_event.set()
            self.status_text.set("Stopping gracefully (waiting for current batch to finish)...")
            self.stop_button.configure(state="disabled")

    def start_ingestion(self):

        from tkinter import messagebox

        csv_path = self.csv_path.get().strip()
        images_dir = self.images_dir.get().strip()
        if not os.path.isfile(csv_path):
            messagebox.showerror("File not found", "Please choose a valid supplier CSV or XML file.")
            return
        if not os.path.isdir(images_dir):
            messagebox.showerror("Image folder not found", "Please choose the folder containing the product images.")
            return
        upload_limit_text = self.upload_limit.get().strip()
        if upload_limit_text:
            try:
                upload_limit = int(upload_limit_text)
            except ValueError:
                messagebox.showerror("Invalid max uploads", "Max Uploads must be blank, 0, or a whole number.")
                return
            if upload_limit < 0:
                messagebox.showerror("Invalid max uploads", "Max Uploads cannot be negative.")
                return
        else:
            upload_limit = 0
        if self.running:
            return

        self.clear_log()
        self.progress["value"] = 0
        self.status_text.set("Starting ingestion...")
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.running = True
        self.stop_event = threading.Event()

        log_handler = TkQueueLogHandler(self.events)

        args = argparse.Namespace(
            csv=csv_path,
            images_dir=images_dir,
            debug_limit=0,
            dry_run=False,
            upload_limit=upload_limit,
            skip_updates=(self.import_mode_cb.get() == "Insert New Products Only"),
            update_prices=self.chk_prices.get(),
            update_stock=self.chk_stock.get(),
            update_images=self.chk_images.get(),
            update_details=self.chk_details.get(),
            update_attributes=self.chk_attributes.get(),
            auto=True,
            log_file=None,
            status_file=None,
            pid_file=None,
            extra_handlers=[log_handler],
            console_log=False,
            progress_callback=self.enqueue_progress,
            stop_event=self.stop_event,
        )
        self.worker = threading.Thread(target=self.run_worker, args=(args,), daemon=True)

        self.worker.start()

    def run_worker(self, args):
        import traceback

        try:
            run_ingestion(args)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if code == 0:
                self.events.put(("finished", True, "Finished"))
            else:
                self.events.put(("finished", False, f"Stopped with exit code {code}"))
        except Exception:
            self.events.put(("log", traceback.format_exc()))
            self.events.put(("finished", False, "Failed"))
        else:
            self.events.put(("finished", True, "Complete"))

    def enqueue_progress(self, payload):
        self.events.put(("progress", payload))

    def drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.append_log(event[1])
                elif kind == "progress":
                    self.update_progress(event[1])
                elif kind == "finished":
                    self.finish_run(event[1], event[2])
        except queue.Empty:
            pass
        self.root.after(100, self.drain_events)

    def append_log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def update_progress(self, payload):
        percent = float(payload.get("percent", 0.0) or 0.0)
        processed = payload.get("processed", 0)
        total = payload.get("total", 0)
        imported = payload.get("imported", 0)
        updated = payload.get("updated", 0)
        skipped = payload.get("skipped", 0)
        failed = payload.get("failed", 0)

        self.progress["value"] = max(0.0, min(100.0, percent))
        label = "items" if total > 0 and self.csv_path.get().lower().endswith(".xml") else "rows"
        self.status_text.set(
            f"{processed}/{total} {label} | {percent:.1f}% | "
            f"Imported {imported} | Updated {updated} | Skipped {skipped} | Failed {failed}"
        )

    def finish_run(self, success, message):
        self.running = False
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_text.set(message)

    def on_close(self):
        from tkinter import messagebox

        if self.running:
            messagebox.showwarning(
                "Ingestion is running",
                "The ingestion pipeline is still running. Please wait for it to finish before closing.",
            )
            return
        self.root.destroy()


def launch_gui():
    import tkinter as tk

    root = tk.Tk()
    IngestionGui(root)
    root.mainloop()


def main():
    default_csv = DEFAULT_CSV_PATH
    if not os.path.isfile(DEFAULT_CSV_PATH) and os.path.isfile(DEFAULT_XML_PATH):
        default_csv = DEFAULT_XML_PATH

    parser = argparse.ArgumentParser(
        description="WooCommerce Ingestion Engine — Import lightbulbs CSV or XML via REST API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Desktop app:\n"
            "  python3 engine.py\n"
            "  python3 engine.py --gui\n\n"
            "Examples:\n"
            "  python3 engine.py --csv /other/file.xml --auto\n"
            "  python3 engine.py --upload-limit 10 --auto\n"
            "  python3 engine.py --dry-run\n"
            "  python3 engine.py --debug-limit 5\n"
        ),
    )
    parser.add_argument(
        "--csv", default=default_csv,
        metavar="PATH",
        help=f"Path to the CSV/XML file. Default: ../data/website/lightbulbs.csv or all products.xml",
    )
    parser.add_argument(
        "--images-dir", default=DEFAULT_IMAGES_DIR,
        metavar="PATH",
        help="Directory containing product images. Default: ../data/pics",
    )
    parser.add_argument(
        "--debug-limit", type=int, default=0,
        metavar="N",
        help="Validation mode: Stop after processing N rows/items (smoke test).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Preview all actions without executing API calls.",
    )
    parser.add_argument(
        "--upload-limit", type=int, default=0,
        metavar="N",
        help="Stop after N successful product creates/updates. Default: 0, process the full file.",
    )
    parser.add_argument(
        "--no-update-prices", action="store_false", dest="update_prices", default=True,
        help="Do not update prices of existing products.",
    )
    parser.add_argument(
        "--no-update-stock", action="store_false", dest="update_stock", default=True,
        help="Do not update stock levels of existing products.",
    )
    parser.add_argument(
        "--no-update-images", action="store_false", dest="update_images", default=True,
        help="Do not update images of existing products.",
    )
    parser.add_argument(
        "--no-update-details", action="store_false", dest="update_details", default=True,
        help="Do not update details (names, descriptions, categories) of existing products.",
    )
    parser.add_argument(
        "--no-update-attributes", action="store_false", dest="update_attributes", default=True,
        help="Do not update custom attributes of existing products.",
    )
    parser.add_argument(
        "--auto", action="store_true", default=False,
        help="Skip interactive confirmation prompt.",
    )
    parser.add_argument(
        "--log-file", default=None,
        help="Optional path for detailed log file output.",
    )
    parser.add_argument(
        "--status-file", default=None, metavar="PATH",
        help="JSON file for live dashboard progress updates.",
    )
    parser.add_argument(
        "--pid-file", default=None, metavar="PATH",
        help="File where the engine writes its PID (used by the Stop button).",
    )
    parser.add_argument(
        "--gui", action="store_true", default=False,
        help="Launch the Tkinter desktop control panel.",
    )
    parser.add_argument(
        "--cli", action="store_true", default=False,
        help="Run the command-line importer with default paths when no other options are supplied.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {VERSION}",
    )

    args = parser.parse_args()
    if args.gui or (len(sys.argv) == 1 and not args.cli):
        launch_gui()
        return
    run_ingestion(args)


if __name__ == "__main__":
    main()
