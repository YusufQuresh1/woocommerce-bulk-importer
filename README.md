# WooCommerce Bulk Importer

A production-grade, high-performance integration engine designed to import large product catalogs (25,000+ items) from eBay CSV or XML exports into WooCommerce. 

It provides a desktop control panel GUI, a robust CLI runner, and a companion WordPress plugin to execute imports in the background on remote servers.

---

## Background & Origin

This utility was originally built specifically for the [Discount Quality Products](https://github.com/YQ-Web-Studio/discount-quality-products) online storefront to handle complex, large-scale SixBit / eBay CSV & XML exports. 

For portfolio demonstration, this repository showcases a full-stack, distributed architecture:
1. **Local Desktop Client (Python + Tkinter)**: A desktop GUI app (compiled to a standalone Windows `.exe`) allowing administrators to load products locally and push data remotely via the secure WooCommerce REST API.
2. **Server-Side Background Importer (PHP + WordPress Plugin)**: A companion WordPress plugin allowing server-side background execution (ideal for bypassing web server execution timeouts on shared hosting).

While tailored for the specific data shape of Discount Quality Products, the engine can be configured or adapted for any WooCommerce store importing similar structured CSV or XML catalogs.

---

## Key Features

- **Dual Formats**: Parses both eBay CSV exports and XML catalogs.
- **REST API Integration**: Communicates directly with WooCommerce and WordPress REST APIs.
- **Tkinter Desktop GUI**: Cross-platform control panel to run, stop, and monitor imports in real-time.
- **Background Mode & WP Plugin**: Runs headlessly on your hosting server with a companion plugin that polls execution progress.
- **Image MD5 Deduplication**: Hashes local images; if they exist in the media library, reuses them to save disk space and bandwidth.
- **Auto-Mapping / Peek**: Reads input files, detects headers dynamically, and proposes mappings.
- **Attribute Extraction**: Automatic extraction of custom product attributes (`IS_` prefixed columns).
- **10% UK Discount Option**: Integrated price multipliers via high-precision `Decimal` arithmetic.
- **Stock & Price Updates Only**: Run the uploader or the dedicated `update_db_stock.py` script to refresh inventory quickly.

---

## Directory Structure

```
woocommerce-bulk-importer/
├── engine.py                  # Core import engine & Tkinter GUI
├── update_db_stock.py         # Fast stock updater utility
├── WooCommerceBulkImporter.spec # PyInstaller build specification
├── requirements.txt           # Python package dependencies
├── wp-plugin/
│   └── bulk-import.php        # WordPress dashboard companion plugin
└── README.md                  # Documentation
```

---

## Installation & Setup

### 1. Requirements
- Python 3.8+
- Active WooCommerce store with REST API access

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configuration
Create a `.env.local` file next to `engine.py` (or beside the compiled `.exe`):

```ini
# Store URLs
WOOCOMMERCE_URL=https://yourstore.com

# Authentication: WordPress Application Passwords (Preferred)
WORDPRESS_USER=admin
WORDPRESS_APP_PASSWORD=abcd efgh ijkl mnop qrst

# Authentication: WooCommerce REST API Keys (Alternative)
WOOCOMMERCE_CONSUMER_KEY=ck_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
WOOCOMMERCE_CONSUMER_SECRET=cs_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

# Next.js Cache Invalidation (Optional)
REVALIDATE_URL=https://yourstore.com/api/revalidate
REVALIDATE_SECRET=your_secret_token
```

---

## Usage Guide

### A. Desktop GUI Mode
Simply run the script with no arguments to launch the Tkinter GUI:
```bash
python engine.py
```
This opens the control panel where you can browse for files, select folders, and start/stop imports while seeing live logs.

### B. CLI Runner
Run with the `--cli` flag or options to run headless:
```bash
# Print help options
python engine.py --help

# Run ingestion headlessly with auto-approval
python engine.py --csv ./data.csv --images-dir ./images --auto

# Dry-run validation (does not write database changes)
python engine.py --csv ./data.xml --dry-run
```

### C. WordPress Companion Plugin
To run the uploader in the background on your WooCommerce host:
1. Upload the `wp-plugin/` directory to `/wp-content/plugins/` on your server.
2. Edit `/wp-content/plugins/wp-plugin/bulk-import.php` to customize your server paths (Python executable, CSV location, engine script location).
3. Activate the **Bulk Product Importer** plugin under WP Admin → Plugins.
4. Go to **Bulk Import** in your WooCommerce dashboard menu to start and monitor uploads.

---

## Creating the Executable (.exe)

You can compile this utility into a single-file executable for Windows users using PyInstaller:

```bash
# Compile using the provided spec file
pyinstaller --clean WooCommerceBulkImporter.spec
```

The compiled binary will be generated in the `dist/` directory as `WooCommerceBulkImporter.exe`. Make sure to place your `.env.local` file in the same directory as the executable.

---

## License

This project is licensed under the MIT License.
