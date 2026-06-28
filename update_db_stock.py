#!/usr/bin/env python3
"""
WooCommerce Database Stock Alignment and Cleanup Script
======================================================
1. Reads 'free postage.csv' and 'free2_filtered.csv'.
2. Maps each product SKU to its 'Qty Currently Listed'.
3. Fetches all products from WooCommerce.
4. Updates stock level in WooCommerce to match 'Qty Currently Listed'.
5. Deletes all products where stock is 0 (or <= 0, or missing in CSV with 0 stock).
"""

import argparse
import csv
import logging
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Add the script directory to sys.path so we can import from engine.py
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.append(str(SCRIPT_DIR))

try:
    from engine import WooCommerceAPI, resolve_sku, COL_SKU, COL_ITEM_NUM
except ImportError:
    print("Error: Could not import WooCommerceAPI or helper functions from engine.py.")
    sys.exit(1)


# Setup basic logger
logger = logging.getLogger("db_cleanup")
logger.setLevel(logging.INFO)
ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(ch)


def parse_csv_files(postage_path, filtered_path):
    """
    Parse the two CSV files and build a mapping of SKU -> Qty Currently Listed.
    """
    sku_to_stock = {}
    csv_files = [postage_path, filtered_path]

    for path in csv_files:
        if not os.path.exists(path):
            logger.warning(f"CSV file not found: {path}")
            continue

        logger.info(f"Parsing CSV file: {path}")
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            has_sku = "SKU" in headers
            has_item_num = "Item ID" in headers

            # Check if Qty Currently Listed exists in headers
            qty_col = "Qty Currently Listed"
            if qty_col not in headers:
                logger.error(f"Required column '{qty_col}' not found in {path}")
                sys.exit(1)

            count = 0
            for row in reader:
                sku, _ = resolve_sku(row, has_sku, has_item_num)
                if not sku:
                    continue

                raw_qty = row.get(qty_col, "").strip()
                qty = 0
                if raw_qty:
                    try:
                        qty = int(float(raw_qty))
                    except ValueError:
                        qty = 0

                # If SKU is already mapped, take the maximum stock level
                if sku in sku_to_stock:
                    sku_to_stock[sku] = max(sku_to_stock[sku], qty)
                else:
                    sku_to_stock[sku] = qty
                count += 1
            logger.info(f"Successfully processed {count} rows. Map size: {len(sku_to_stock)}")

    return sku_to_stock


def fetch_all_db_products(wp):
    """
    Fetch all products from WooCommerce including metadata needed for updates.
    Returns a list of dicts.
    """
    logger.info("Fetching all products from WooCommerce...")
    all_products = []
    page = 1
    while True:
        url = f"/wp-json/wc/v3/products?per_page=100&page={page}&_fields=id,sku,name,stock_quantity,manage_stock"
        res = wp._request("GET", url)
        if not res or not isinstance(res, list):
            break
        all_products.extend(res)
        logger.info(f"  Fetched {len(all_products)} products so far...")
        if len(res) < 100:
            break
        page += 1

    logger.info(f"Total products fetched from WooCommerce database: {len(all_products)}")
    return all_products


def main():
    parser = argparse.ArgumentParser(description="WooCommerce Database Stock Cleanup & Alignment Script")
    parser.add_argument(
        "--postage-csv",
        default=r"c:\Development\DQP\data\free postage.csv",
        help="Path to free postage CSV file."
    )
    parser.add_argument(
        "--filtered-csv",
        default=r"c:\Development\DQP\data\free2_filtered.csv",
        help="Path to free2 filtered CSV file."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview operations without executing API calls."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Batch size for update and delete requests."
    )

    args = parser.parse_args()

    # 1. Parse CSV files
    sku_to_stock = parse_csv_files(args.postage_csv, args.filtered_csv)
    if not sku_to_stock:
        logger.error("No stock mappings generated. Exiting.")
        sys.exit(1)

    # 2. Init API Client
    wp = WooCommerceAPI(dry_run=args.dry_run, logger=logger)

    # 3. Fetch live DB products
    db_products = fetch_all_db_products(wp)
    if not db_products and not args.dry_run:
        logger.warning("No products found in WooCommerce database.")
        sys.exit(0)

    updates = []
    deletes = []

    for p in db_products:
        p_id = p.get("id")
        sku = p.get("sku")
        name = p.get("name", "Unknown Product")
        current_stock = p.get("stock_quantity")
        manage_stock = p.get("manage_stock", False)

        # Parse current stock
        try:
            current_stock_int = int(current_stock) if current_stock is not None else 0
        except (ValueError, TypeError):
            current_stock_int = 0

        target_stock = None
        if sku and sku in sku_to_stock:
            target_stock = sku_to_stock[sku]
        else:
            # Not in CSV -> If it currently has 0 stock in DB, we remove it.
            if current_stock_int == 0:
                target_stock = 0

        if target_stock is not None:
            if target_stock <= 0:
                deletes.append((p_id, sku, name, current_stock_int))
            elif current_stock_int != target_stock or not manage_stock:
                updates.append((p_id, sku, name, current_stock_int, target_stock))

    logger.info("========================================")
    logger.info(f"Found {len(updates)} products to update stock levels.")
    logger.info(f"Found {len(deletes)} products with zero stock to delete.")
    logger.info("========================================")

    # 4. Perform Updates
    if updates:
        logger.info(f"Starting stock updates (Batch size: {args.batch_size})...")
        for idx in range(0, len(updates), args.batch_size):
            batch = updates[idx:idx + args.batch_size]
            update_payload = []
            for p_id, sku, name, old_s, new_s in batch:
                logger.info(f"  [UPDATE] SKU '{sku}' (ID {p_id}): '{name}' | Stock {old_s} -> {new_s}")
                update_payload.append({
                    "id": p_id,
                    "manage_stock": True,
                    "stock_quantity": new_s,
                    "stock_status": "instock"
                })

            if not args.dry_run:
                success = wp.batch_operations([], update_payload)
                if success:
                    logger.info(f"  Batch update of {len(batch)} products succeeded.")
                else:
                    logger.error(f"  Batch update of {len(batch)} products failed.")

    # 5. Perform Deletions
    if deletes:
        logger.info(f"Starting product deletions (Batch size: {args.batch_size})...")
        for idx in range(0, len(deletes), args.batch_size):
            batch = deletes[idx:idx + args.batch_size]
            delete_ids = [p_id for p_id, _, _, _ in batch]

            for p_id, sku, name, old_s in batch:
                logger.info(f"  [DELETE] SKU '{sku}' (ID {p_id}): '{name}' | Stock was {old_s}")

            if not args.dry_run:
                # WooCommerce batch endpoint accepts delete: [id1, id2, ...]
                payload = {"delete": delete_ids}
                res = wp._request("POST", "/wp-json/wc/v3/products/batch", json=payload)
                if res:
                    logger.info(f"  Batch delete of {len(batch)} products succeeded.")
                else:
                    logger.error(f"  Batch delete of {len(batch)} products failed.")

    logger.info("Stock Level Alignment and Cleanup completed.")


if __name__ == "__main__":
    main()
