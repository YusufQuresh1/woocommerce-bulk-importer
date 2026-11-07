"""
End-to-end verification script for DQP-2 ingestion modes.
Demonstrates and proves that:
1. In "Update Existing Products Only" mode:
   - Non-existing SKUs are skipped and logged.
   - Zero creates are queued or sent to WooCommerce.
   - Existing SKUs are updated according to checked fields.
2. In "Insert New Products Only" mode:
   - Existing SKUs are skipped.
   - Non-existing SKUs are queued for creation.
3. In "Insert New & Update Existing" mode:
   - Existing SKUs are updated.
   - Non-existing SKUs are inserted.
"""

import argparse
import csv
import io
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add script directory to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
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
    COL_DESC,
    COL_CATEGORY,
    COL_PICTURES,
    run_ingestion,
)


def create_test_csv(file_path):
    """Create a sample CSV with 1 existing SKU and 2 new SKUs."""
    fieldnames = [
        COL_ITEM_NUM,
        COL_SKU,
        COL_TITLE,
        COL_PRICE,
        COL_QTY,
        COL_SHIPPING,
        COL_VARIATIONS,
        COL_DESC,
        COL_CATEGORY,
        COL_PICTURES,
    ]
    rows = [
        # Row 1: Existing SKU in WooCommerce
        {
            COL_ITEM_NUM: "61189",
            COL_SKU: "SB-61189",
            COL_TITLE: "2x 25W White Coloured Incandescent Dimmable GLS Light Bulbs",
            COL_PRICE: "8.99",
            COL_QTY: "2",
            COL_SHIPPING: "",
            COL_VARIATIONS: "False",
            COL_DESC: "Sample description",
            COL_CATEGORY: "Light Bulbs",
            COL_PICTURES: "",
        },
        # Row 2: Non-existing SKU (new product)
        {
            COL_ITEM_NUM: "61122",
            COL_SKU: "SB-61122",
            COL_TITLE: "2x 15W Opal Dimmable Candle Incandescent Light Bulbs",
            COL_PRICE: "8.99",
            COL_QTY: "5",
            COL_SHIPPING: "",
            COL_VARIATIONS: "False",
            COL_DESC: "Sample candle description",
            COL_CATEGORY: "Light Bulbs",
            COL_PICTURES: "",
        },
        # Row 3: Another non-existing SKU (new product)
        {
            COL_ITEM_NUM: "60879",
            COL_SKU: "SB-60879",
            COL_TITLE: "2x 40W Osram Opal Candle Incandescent Filament Light Bulbs",
            COL_PRICE: "9.99",
            COL_QTY: "10",
            COL_SHIPPING: "",
            COL_VARIATIONS: "False",
            COL_DESC: "Sample Osram description",
            COL_CATEGORY: "Light Bulbs",
            COL_PICTURES: "",
        },
    ]

    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class MockWooCommerceAPI:
    """Mock WooCommerce API tracking all batch operations."""

    def __init__(self, dry_run=False, logger=None):
        self.dry_run = dry_run
        self.log = logger or logging.getLogger("MockWP")
        self.captured_creates = []
        self.captured_updates = []

    def fetch_all_products(self):
        # SB-61189 exists in WooCommerce with ID 1001
        return {
            "SB-61189": {
                "id": 1001,
                "name": "Old Title for SB-61189",
                "regular_price": "7.50",
                "stock_quantity": 1,
            }
        }

    def fetch_products_by_skus(self, skus):
        all_prods = self.fetch_all_products()
        return {sku: all_prods[sku] for sku in skus if sku in all_prods}

    def batch_operations(self, create_list, update_list):
        self.captured_creates.extend(list(create_list))
        self.captured_updates.extend(list(update_list))
        self.log.info("Mock API: Received %d creates, %d updates", len(create_list), len(update_list))
        return True

    def _request(self, method, url, data=None):
        return []


def run_test_scenario(scenario_name, mode_arg, update_stock_only=False):
    print("=" * 70)
    print(f"RUNNING SCENARIO: {scenario_name}")
    print(f"Parameters: mode='{mode_arg}', update_stock_only={update_stock_only}")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = os.path.join(tmpdir, "test_input.csv")
        create_test_csv(csv_path)

        parser = engine.build_arg_parser()
        cmd_args = [
            "--csv", csv_path,
            "--images-dir", tmpdir,
            "--auto",
            "--mode", mode_arg,
        ]

        if update_stock_only:
            cmd_args.extend([
                "--no-update-prices",
                "--no-update-images",
                "--no-update-details",
                "--no-update-attributes",
            ])

        args = parser.parse_args(cmd_args)

        mock_wp_instance = MockWooCommerceAPI(logger=logging.getLogger("test"))

        with patch("engine.WooCommerceAPI", return_value=mock_wp_instance):
            with patch("engine.CategoryCache"):
                with patch("engine.AttributeRegistry"):
                    run_ingestion(args)

        print("\n--- RESULTS FOR SCENARIO:", scenario_name)
        print(f"Queued Creates ({len(mock_wp_instance.captured_creates)}):")
        for c in mock_wp_instance.captured_creates:
            print(f"  + CREATE: SKU={c.get('sku')}, Name={c.get('name')[:40]}..., Price={c.get('regular_price')}")

        print(f"Queued Updates ({len(mock_wp_instance.captured_updates)}):")
        for u in mock_wp_instance.captured_updates:
            print(f"  * UPDATE: ID={u.get('id')}, Fields={list(u.keys())}")
            if "stock_quantity" in u:
                print(f"    -> new stock_quantity={u.get('stock_quantity')}")
            if "regular_price" in u:
                print(f"    -> new regular_price={u.get('regular_price')}")

        return mock_wp_instance.captured_creates, mock_wp_instance.captured_updates


def main():
    print("STARTING DQP-2 END-TO-END VERIFICATION")
    print("=" * 70)

    # -----------------------------------------------------------------------
    # SCENARIO 1: Update Existing Products Only (The Core Ticket DQP-2 Requirement)
    # -----------------------------------------------------------------------
    creates, updates = run_test_scenario(
        "SCENARIO 1: Update Existing Products Only",
        mode_arg="update-only",
        update_stock_only=False,
    )
    assert len(creates) == 0, f"FAILURE: Expected 0 creates in update-only mode, got {len(creates)}"
    assert len(updates) == 1, f"FAILURE: Expected 1 update in update-only mode, got {len(updates)}"
    assert updates[0]["id"] == 1001, f"FAILURE: Expected update for ID 1001, got {updates[0]['id']}"
    print(">>> VERIFICATION PASSED: ZERO new products created. Only existing product updated.")

    # -----------------------------------------------------------------------
    # SCENARIO 2: Update Existing Products Only with Stock Only Checked
    # -----------------------------------------------------------------------
    creates, updates = run_test_scenario(
        "SCENARIO 2: Update Existing Products Only (Stock Only Checked)",
        mode_arg="update-only",
        update_stock_only=True,
    )
    assert len(creates) == 0, f"FAILURE: Expected 0 creates, got {len(creates)}"
    assert len(updates) == 1, f"FAILURE: Expected 1 update, got {len(updates)}"
    assert "stock_quantity" in updates[0], "FAILURE: stock_quantity should be updated"
    assert "regular_price" not in updates[0], "FAILURE: regular_price must NOT be updated when stock-only is checked"
    assert "name" not in updates[0], "FAILURE: name must NOT be updated when stock-only is checked"
    print(">>> VERIFICATION PASSED: Only stock fields updated for existing product. Prices & names untouched.")

    # -----------------------------------------------------------------------
    # SCENARIO 3: Insert New Products Only
    # -----------------------------------------------------------------------
    creates, updates = run_test_scenario(
        "SCENARIO 3: Insert New Products Only",
        mode_arg="insert-only",
        update_stock_only=False,
    )
    assert len(creates) == 2, f"FAILURE: Expected 2 creates in insert-only mode, got {len(creates)}"
    assert len(updates) == 0, f"FAILURE: Expected 0 updates in insert-only mode, got {len(updates)}"
    created_skus = {c["sku"] for c in creates}
    assert created_skus == {"SB-61122", "SB-60879"}, f"FAILURE: Unexpected created SKUs: {created_skus}"
    print(">>> VERIFICATION PASSED: Existing product skipped. Exactly 2 new products queued for insertion.")

    # -----------------------------------------------------------------------
    # SCENARIO 4: Insert New & Update Existing
    # -----------------------------------------------------------------------
    creates, updates = run_test_scenario(
        "SCENARIO 4: Insert New & Update Existing",
        mode_arg="insert-and-update",
        update_stock_only=False,
    )
    assert len(creates) == 2, f"FAILURE: Expected 2 creates in insert-and-update mode, got {len(creates)}"
    assert len(updates) == 1, f"FAILURE: Expected 1 update in insert-and-update mode, got {len(updates)}"
    assert updates[0]["id"] == 1001
    created_skus = {c["sku"] for c in creates}
    assert created_skus == {"SB-61122", "SB-60879"}
    print(">>> VERIFICATION PASSED: Both creates and updates successfully processed.")

    print("\n" + "=" * 70)
    print("ALL 4 SCENARIOS VERIFIED SUCCESSFULLY! DQP-2 IS PROVEN WORKING.")
    print("=" * 70)


if __name__ == "__main__":
    main()
