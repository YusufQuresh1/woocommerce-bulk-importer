import io
import logging
import sys
import unittest
from pathlib import Path

# Add tools/ingestion to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import engine
from engine import (
    COL_SKU,
    COL_ITEM_NUM,
    COL_PRICE,
    COL_QTY,
    COL_SHIPPING,
    COL_VARIATIONS,
    COL_TITLE,
    process_row,
)


class DummyCache:
    def resolve(self, cat_str):
        return None


class DummyAttrRegistry:
    def get_or_create_attribute(self, name):
        return 1
    def get_or_create_term(self, attr_id, term_name):
        return 1


class DummyWP:
    dry_run = True
    def _request(self, method, url, data=None):
        return []


class TestIngestionModes(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test_logger")
        self.logger.setLevel(logging.DEBUG)
        self.variations_fh = io.StringIO()
        self.debug_counter = [0]
        self.wp = DummyWP()
        self.cat_cache = DummyCache()
        self.attr_registry = DummyAttrRegistry()
        self.images_dir = str(SCRIPT_DIR)

    def _sample_row(self, sku="SKU-001", price="19.99", qty="10"):
        return {
            COL_SKU: sku,
            COL_ITEM_NUM: "12345678",
            COL_PRICE: price,
            COL_QTY: qty,
            COL_SHIPPING: "",
            COL_VARIATIONS: "",
            COL_TITLE: f"Product {sku}",
        }

    def test_process_row_skips_non_existing_product_when_skip_inserts_is_true(self):
        """Update Existing Products Only mode: non-existing SKU must be skipped."""
        row = self._sample_row(sku="NEW-SKU-99")
        existing_products = {}

        status, payload = process_row(
            row=row,
            has_sku=True,
            has_item_num=True,
            images_dir=self.images_dir,
            wp=self.wp,
            cat_cache=self.cat_cache,
            attr_registry=self.attr_registry,
            logger=self.logger,
            dry_run=True,
            variations_log_fh=self.variations_fh,
            debug_counter=self.debug_counter,
            existing_products=existing_products,
            skip_updates=False,
            skip_inserts=True,
        )

        self.assertEqual(status, "skipped")
        self.assertIsNone(payload)

    def test_process_row_creates_product_when_skip_inserts_is_false(self):
        """Insert New & Update Existing mode: non-existing SKU must be queued for creation."""
        row = self._sample_row(sku="NEW-SKU-99")
        existing_products = {}

        status, payload = process_row(
            row=row,
            has_sku=True,
            has_item_num=True,
            images_dir=self.images_dir,
            wp=self.wp,
            cat_cache=self.cat_cache,
            attr_registry=self.attr_registry,
            logger=self.logger,
            dry_run=True,
            variations_log_fh=self.variations_fh,
            debug_counter=self.debug_counter,
            existing_products=existing_products,
            skip_updates=False,
            skip_inserts=False,
        )

        self.assertEqual(status, "queued_create")
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("sku"), "NEW-SKU-99")

    def test_process_row_updates_existing_product_in_update_only_mode(self):
        """Update Existing Products Only mode: existing SKU is queued for update."""
        row = self._sample_row(sku="EXIST-SKU-1", price="25.00", qty="8")
        existing_products = {
            "EXIST-SKU-1": {
                "id": 555,
                "name": "Old Product Title",
                "regular_price": "20.00",
                "stock_quantity": 2,
            }
        }

        status, payload = process_row(
            row=row,
            has_sku=True,
            has_item_num=True,
            images_dir=self.images_dir,
            wp=self.wp,
            cat_cache=self.cat_cache,
            attr_registry=self.attr_registry,
            logger=self.logger,
            dry_run=True,
            variations_log_fh=self.variations_fh,
            debug_counter=self.debug_counter,
            existing_products=existing_products,
            skip_updates=False,
            skip_inserts=True,
            update_prices=True,
            update_stock=True,
        )

        self.assertEqual(status, "queued_update")
        self.assertIsNotNone(payload)
        self.assertEqual(payload.get("id"), 555)

    def test_process_row_skips_existing_product_in_insert_only_mode(self):
        """Insert New Products Only mode: existing SKU must be skipped."""
        row = self._sample_row(sku="EXIST-SKU-1")
        existing_products = {
            "EXIST-SKU-1": {
                "id": 555,
                "name": "Product Title",
                "regular_price": "19.99",
                "stock_quantity": 10,
            }
        }

        status, payload = process_row(
            row=row,
            has_sku=True,
            has_item_num=True,
            images_dir=self.images_dir,
            wp=self.wp,
            cat_cache=self.cat_cache,
            attr_registry=self.attr_registry,
            logger=self.logger,
            dry_run=True,
            variations_log_fh=self.variations_fh,
            debug_counter=self.debug_counter,
            existing_products=existing_products,
            skip_updates=True,
            skip_inserts=False,
        )

    def test_process_row_update_stock_only_updates_only_stock(self):
        """When only update_stock is True, regular_price/name/images are not in update payload."""
        row = self._sample_row(sku="EXIST-SKU-1", price="30.00", qty="15")
        existing_products = {
            "EXIST-SKU-1": {
                "id": 555,
                "name": "Old Product Title",
                "regular_price": "20.00",
                "stock_quantity": 2,
            }
        }

        status, payload = process_row(
            row=row,
            has_sku=True,
            has_item_num=True,
            images_dir=self.images_dir,
            wp=self.wp,
            cat_cache=self.cat_cache,
            attr_registry=self.attr_registry,
            logger=self.logger,
            dry_run=True,
            variations_log_fh=self.variations_fh,
            debug_counter=self.debug_counter,
            existing_products=existing_products,
            skip_updates=False,
            skip_inserts=True,
            update_prices=False,
            update_stock=True,
            update_images=False,
            update_details=False,
            update_attributes=False,
        )

        self.assertEqual(status, "queued_update")
        self.assertEqual(payload.get("id"), 555)
        self.assertEqual(payload.get("stock_quantity"), 15)
        self.assertTrue(payload.get("in_stock"))
        self.assertNotIn("regular_price", payload)
        self.assertNotIn("name", payload)

    def test_process_row_zero_stock_updates_existing_product(self):
        """Zero-stock items that already exist in WC must be updated to 0 stock."""
        row = self._sample_row(sku="EXIST-SKU-1", price="20.00", qty="0")
        existing_products = {
            "EXIST-SKU-1": {
                "id": 555,
                "name": "Existing Product",
                "regular_price": "20.00",
                "stock_quantity": 5,
            }
        }

        status, payload = process_row(
            row=row,
            has_sku=True,
            has_item_num=True,
            images_dir=self.images_dir,
            wp=self.wp,
            cat_cache=self.cat_cache,
            attr_registry=self.attr_registry,
            logger=self.logger,
            dry_run=True,
            variations_log_fh=self.variations_fh,
            debug_counter=self.debug_counter,
            existing_products=existing_products,
            skip_updates=False,
            skip_inserts=True,
            update_stock=True,
        )

        self.assertEqual(status, "queued_update")
        self.assertEqual(payload.get("id"), 555)
        self.assertEqual(payload.get("stock_quantity"), 0)
        self.assertFalse(payload.get("in_stock"))

    def test_process_row_zero_stock_skips_new_product(self):
        """Zero-stock items not in WC must be skipped even if skip_inserts is False."""
        row = self._sample_row(sku="NEW-SKU-0", price="20.00", qty="0")
        existing_products = {}

        status, payload = process_row(
            row=row,
            has_sku=True,
            has_item_num=True,
            images_dir=self.images_dir,
            wp=self.wp,
            cat_cache=self.cat_cache,
            attr_registry=self.attr_registry,
            logger=self.logger,
            dry_run=True,
            variations_log_fh=self.variations_fh,
            debug_counter=self.debug_counter,
            existing_products=existing_products,
            skip_updates=False,
            skip_inserts=False,
        )

        self.assertEqual(status, "skipped")
        self.assertIsNone(payload)

    def test_cli_mode_parsing(self):
        """Test that CLI --mode resolves correctly to skip_inserts and skip_updates."""
        parser = engine.build_arg_parser()

        args_update = parser.parse_args(["--mode", "update-only"])
        self.assertTrue(args_update.skip_inserts)
        self.assertFalse(args_update.skip_updates)

        args_insert = parser.parse_args(["--mode", "insert-only"])
        self.assertFalse(args_insert.skip_inserts)
        self.assertTrue(args_insert.skip_updates)

        args_both = parser.parse_args(["--mode", "insert-and-update"])
        self.assertFalse(args_both.skip_inserts)
        self.assertFalse(args_both.skip_updates)


if __name__ == "__main__":
    unittest.main()
