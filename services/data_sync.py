"""Data sync orchestrator: downloads stock data from baostock → local DB.

Handles: full sync, incremental update, single-stock add, missing-stock fill.
"""
from __future__ import annotations
import time as _time

from db_config import get_connection
from services.data_fetcher import (
    login, logout, _bs_force_login,
    fetch_all_stock_codes, fetch_stock_basic, fetch_kline_range,
)
from datetime import datetime, timedelta


# ── Stock code management ─────────────────────────────────────────────────

def upsert_stock_code(code: str, stock_id: int | None = None) -> int:
    """Insert or update a stock code row. Returns stock_id.

    If stock_id is provided, updates that row; otherwise inserts.
    Always tries to fetch basic info from baostock for name/ipo_date/etc.
    """
    basic = fetch_stock_basic(code)
    name = basic.get("code_name", "") if basic else ""
    ipo_date = _safe_date(basic.get("ipoDate")) if basic else None
    out_date = _safe_date(basic.get("outDate")) if basic else None
    _type = basic.get("type", "") if basic else ""
    status = basic.get("status", "") if basic else ""

    with get_connection() as conn:
        with conn.cursor() as cur:
            if stock_id:
                cur.execute(
                    "UPDATE stock_code SET name=%s, ipo_date=%s, out_date=%s, type=%s, status=%s WHERE id=%s",
                    (name, ipo_date, out_date, _type, status, stock_id),
                )
                return stock_id
            else:
                cur.execute(
                    "INSERT INTO stock_code (code, name, ipo_date, out_date, type, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE name=VALUES(name), ipo_date=VALUES(ipo_date), "
                    "out_date=VALUES(out_date), type=VALUES(type), status=VALUES(status)",
                    (code, name, ipo_date, out_date, _type, status),
                )
                cur.execute("SELECT id FROM stock_code WHERE code = %s", (code,))
                return cur.fetchone()["id"]


def get_stock_id_map() -> dict[str, int]:
    """Get {code: stock_id} for all stocks in stock_code table."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, code FROM stock_code")
            return {r["code"]: r["id"] for r in cur.fetchall()}


def get_latest_dates_batch(stock_ids: list[int]) -> dict[int, str]:
    """Get latest trading date per stock_id. Returns {stock_id: 'YYYY-MM-DD'}."""
    if not stock_ids:
        return {}
    placeholders = ",".join(["%s"] * len(stock_ids))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT stock_id, MAX(date) AS max_date FROM stock_detail "
                f"WHERE stock_id IN ({placeholders}) GROUP BY stock_id",
                stock_ids,
            )
            return {r["stock_id"]: str(r["max_date"]) for r in cur.fetchall()}


# ── K-line ingestion ──────────────────────────────────────────────────────

_KLINE_COLS = [
    "stock_id", "date", "open", "high", "low", "close", "preclose",
    "volume", "amount", "turn", "pct_chg",
]


def save_kline_rows(stock_id: int, rows: list[dict]):
    """Batch INSERT ... ON DUPLICATE KEY UPDATE for K-line rows.

    Only writes raw baostock fields — no indicator computation.
    """
    if not rows:
        return

    placeholders = ",".join(["%s"] * len(_KLINE_COLS))
    update_cols = [c for c in _KLINE_COLS if c not in ("stock_id", "date")]
    update_clause = ",".join(f"{c}=VALUES({c})" for c in update_cols)
    sql = (
        f"INSERT INTO stock_detail ({','.join(_KLINE_COLS)}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {update_clause}"
    )

    data_rows = []
    for r in rows:
        data_rows.append([
            stock_id,
            str(r["date"])[:10],
            r.get("open"), r.get("high"), r.get("low"),
            r.get("close"), r.get("preclose"),
            r.get("volume", 0), r.get("amount", 0),
            r.get("turn", 0), r.get("pct_chg", 0),
        ])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, data_rows)


# ── Sync operations ───────────────────────────────────────────────────────

def sync_all_stock_codes(limit: int | None = None) -> int:
    """Pull all A-share codes from baostock and populate stock_code table.

    Returns number of stocks written.
    """
    print("[sync] 正在获取全量A股代码列表...")
    all_codes = fetch_all_stock_codes()
    if not all_codes:
        print("[sync] 获取股票列表失败")
        return 0

    if limit:
        all_codes = all_codes[:limit]

    print(f"[sync] 共 {len(all_codes)} 只A股，正在写入基本信息...")
    count = 0
    for i, code in enumerate(all_codes, 1):
        if i % 100 == 0:
            print(f"  ... {i}/{len(all_codes)}")
        upsert_stock_code(code)
        count += 1

    print(f"[sync] stock_code 表写入完成: {count} 只")
    return count


def sync_kline_full(stock_ids: list[int] | None = None, days: int = 500,
                    on_progress=None) -> tuple[int, int]:
    """Full K-line download for given stock_ids (or all in DB).

    Returns (success_count, error_count).
    """
    _bs_force_login()

    if stock_ids is None:
        code_map = get_stock_id_map()
        stock_ids = list(code_map.values())
        id_to_code = {v: k for k, v in code_map.items()}
    else:
        with get_connection() as conn:
            with conn.cursor() as cur:
                placeholders = ",".join(["%s"] * len(stock_ids))
                cur.execute(f"SELECT id, code FROM stock_code WHERE id IN ({placeholders})", stock_ids)
                id_to_code = {r["id"]: r["code"] for r in cur.fetchall()}

    success, errors = 0, 0
    total = len(stock_ids)

    for i, sid in enumerate(stock_ids, 1):
        code = id_to_code.get(sid)
        if not code:
            errors += 1
            continue

        if i % 100 == 0:
            _time.sleep(2)
            _bs_force_login()

        for attempt in range(3):
            try:
                rows = fetch_kline_range(
                    code,
                    (datetime.today() - timedelta(days=days + 30)).strftime("%Y-%m-%d"),
                    datetime.today().strftime("%Y-%m-%d"),
                )
                if rows:
                    rows = rows[-days:] if len(rows) > days else rows
                    save_kline_rows(sid, rows)
                    success += 1
                else:
                    errors += 1
                break
            except Exception:
                if attempt < 2:
                    _time.sleep(3)
                    _bs_force_login()
                else:
                    errors += 1

        if on_progress:
            on_progress(code, i, total)

    return success, errors


def sync_kline_incremental(stock_ids: list[int] | None = None,
                           on_progress=None) -> tuple[int, int]:
    """Incremental update: only fetch new trading days since last cached date.

    Returns (updated_count, error_count). Already-up-to-date stocks are not counted.
    """
    _bs_force_login()

    if stock_ids is None:
        code_map = get_stock_id_map()
        stock_ids = list(code_map.values())
        id_to_code = {v: k for k, v in code_map.items()}
    else:
        with get_connection() as conn:
            with conn.cursor() as cur:
                placeholders = ",".join(["%s"] * len(stock_ids))
                cur.execute(f"SELECT id, code FROM stock_code WHERE id IN ({placeholders})", stock_ids)
                id_to_code = {r["id"]: r["code"] for r in cur.fetchall()}

    latest_dates = get_latest_dates_batch(stock_ids)
    today_str = datetime.today().strftime("%Y-%m-%d")

    success, errors = 0, 0
    total = len(stock_ids)

    for i, sid in enumerate(stock_ids, 1):
        code = id_to_code.get(sid)
        if not code:
            errors += 1
            if on_progress:
                on_progress(code or "?", i, total)
            continue

        latest = latest_dates.get(sid)
        if not latest or latest >= today_str:
            if on_progress:
                on_progress(code, i, total)
            continue

        if i % 100 == 0:
            _time.sleep(2)
            _bs_force_login()

        start_dt = datetime.strptime(latest[:10], "%Y-%m-%d") + timedelta(days=1)
        start = start_dt.strftime("%Y-%m-%d")

        for attempt in range(3):
            try:
                new_rows = fetch_kline_range(code, start, today_str)
                if new_rows:
                    save_kline_rows(sid, new_rows)
                    success += 1
                break
            except Exception:
                if attempt < 2:
                    _time.sleep(3)
                    _bs_force_login()
                else:
                    errors += 1

        if on_progress:
            on_progress(code, i, total)

    return success, errors


def sync_missing() -> int:
    """Fetch codes that are in stock_code but have no K-line data yet.

    Returns number of stocks synced.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sc.id, sc.code FROM stock_code sc
                WHERE sc.id NOT IN (SELECT DISTINCT stock_id FROM stock_detail)
            """)
            missing = [(r["id"], r["code"]) for r in cur.fetchall()]

    if not missing:
        print("[sync] 所有股票已缓存数据")
        return 0

    print(f"[sync] 补全 {len(missing)} 只缺失的股票...")
    _bs_force_login()
    success = 0
    for i, (sid, code) in enumerate(missing, 1):
        if i % 100 == 0:
            _time.sleep(2)
            _bs_force_login()
        try:
            rows = fetch_kline_range(
                code,
                (datetime.today() - timedelta(days=530)).strftime("%Y-%m-%d"),
                datetime.today().strftime("%Y-%m-%d"),
            )
            if rows:
                rows = rows[-500:] if len(rows) > 500 else rows
                save_kline_rows(sid, rows)
                success += 1
        except Exception:
            pass
        if i % 50 == 0 or i == len(missing):
            print(f"  ... {i}/{len(missing)}")

    print(f"[sync] 补全完成: {success}/{len(missing)}")
    return success


# ── Helpers ───────────────────────────────────────────────────────────────

def _safe_date(val) -> str | None:
    """Convert baostock date string ('2020-01-15') to date-safe string, or None."""
    if not val or val == "":
        return None
    return str(val)[:10]
