"""Migration script: import all file-based data into MySQL."""
from __future__ import annotations
import os
import sys
import json
import pandas as pd
from datetime import datetime

import db_config
import db_manager


def migrate_stock_history():
    """Import all CSV files from cache/hist/ into stock_history table."""
    cache_dir = "cache/hist"
    if not os.path.exists(cache_dir):
        print("  [SKIP] cache/hist/ directory not found")
        return 0

    csv_files = [f for f in os.listdir(cache_dir) if f.endswith(".csv")]
    total = len(csv_files)
    if total == 0:
        print("  [SKIP] No CSV files found")
        return 0

    print(f"  Found {total} CSV files to import...")
    success, errors = 0, 0

    for i, fname in enumerate(csv_files, 1):
        code = fname[:-4].replace("_", ".", 1)  # sh_600519.csv -> sh.600519
        path = os.path.join(cache_dir, fname)
        try:
            df = pd.read_csv(path, parse_dates=["date"])
            if not df.empty:
                db_manager.save_stock_history(code, df)
                success += 1
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  [ERROR] {code}: {e}")

        if i % 500 == 0 or i == total:
            print(f"  Progress: {i}/{total} (success={success}, errors={errors})")

    return success


def migrate_portfolio():
    """Import portfolio.json into portfolio table."""
    path = "portfolio.json"
    if not os.path.exists(path):
        print("  [SKIP] portfolio.json not found")
        return

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    db_manager.save_portfolio(data)
    print(f"  Imported: cash={data.get('cash', 0):,.2f}, "
          f"positions={len(data.get('positions', {}))}, "
          f"orders={len(data.get('orders', []))}")


def migrate_watchlist():
    """Import watchlist.txt into watchlist table."""
    path = "watchlist.txt"
    if not os.path.exists(path):
        print("  [SKIP] watchlist.txt not found")
        return

    with open(path) as f:
        codes = [line.strip() for line in f if line.strip()]
    if codes:
        db_manager.save_watchlist(codes)
        print(f"  Imported {len(codes)} stocks")
    else:
        print("  [SKIP] watchlist.txt is empty")


def migrate_configs():
    """Import stock_picker_config.json and filters/*.json into configs table."""
    # Main picker config
    path = "stock_picker_config.json"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        db_manager.save_config("stock_picker", cfg, "选股配置")
        print(f"  Imported stock_picker_config.json")
    else:
        print("  [SKIP] stock_picker_config.json not found")

    # Filter presets
    filters_dir = "filters"
    if os.path.exists(filters_dir):
        count = 0
        for fname in os.listdir(filters_dir):
            if fname.endswith(".json"):
                fpath = os.path.join(filters_dir, fname)
                with open(fpath, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                key = f"filter_{fname[:-5]}"
                db_manager.save_config(key, cfg, f"策略预设: {fname}")
                count += 1
        print(f"  Imported {count} filter presets")
    else:
        print("  [SKIP] filters/ directory not found")


def verify_migration():
    """Verify data integrity after migration."""
    print("\n=== Verification ===")

    codes = db_manager.get_cached_stock_codes()
    print(f"  stock_history: {len(codes)} stocks in database")

    if codes:
        sample = codes[0]
        df = db_manager.load_stock_history(sample)
        rows = len(df) if df is not None else 0
        print(f"  Sample [{sample}]: {rows} rows")

    portfolio = db_manager.load_portfolio()
    if portfolio:
        print(f"  portfolio: cash={portfolio['cash']:,.2f}, "
              f"positions={len(portfolio['positions'])}, "
              f"orders={len(portfolio['orders'])}")
    else:
        print("  portfolio: empty")

    wl = db_manager.load_watchlist()
    print(f"  watchlist: {len(wl)} stocks")

    for key in ["stock_picker"]:
        cfg = db_manager.load_config(key)
        print(f"  config[{key}]: {'loaded' if cfg else 'not found'}")


def main():
    print("=" * 50)
    print("  Stock Data Migration to MySQL")
    print("=" * 50)

    # Test connection
    print("\nTesting database connection...")
    try:
        db_manager.init_database()
        print("  Connected successfully, tables created/verified.")
    except Exception as e:
        print(f"  [FATAL] Cannot connect to MySQL: {e}")
        print("  Make sure MySQL is running and the password is correct.")
        sys.exit(1)

    # Run migrations
    print("\n=== Migrating Data ===\n")

    print("[1/4] Stock history (CSV -> DB):")
    count = migrate_stock_history()
    print(f"  Done: {count} stocks imported\n")

    print("[2/4] Portfolio:")
    migrate_portfolio()
    print()

    print("[3/4] Watchlist:")
    migrate_watchlist()
    print()

    print("[4/4] Configs:")
    migrate_configs()

    # Verify
    verify_migration()

    print("\n" + "=" * 50)
    print("  Migration complete!")
    print("=" * 50)


if __name__ == "__main__":
    main()
