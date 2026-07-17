"""Database manager: CRUD operations for stock data in MySQL."""
from __future__ import annotations
import json
from db_config import get_connection


# ── Schema ──────────────────────────────────────────────────────────────

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS stock_history (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    code VARCHAR(20) NOT NULL,
    date DATE NOT NULL,
    open DECIMAL(10,2),
    high DECIMAL(10,2),
    low DECIMAL(10,2),
    close DECIMAL(10,2),
    preclose DECIMAL(10,2),
    volume BIGINT,
    amount DECIMAL(18,2),
    turn DECIMAL(8,4),
    pctChg DECIMAL(8,4),
    amplitude DECIMAL(8,4),
    MA5 DECIMAL(10,2),
    MA10 DECIMAL(10,2),
    MA20 DECIMAL(10,2),
    MA60 DECIMAL(10,2),
    DIF DECIMAL(10,6),
    DEA DECIMAL(10,6),
    MACD DECIMAL(10,6),
    RSI14 DECIMAL(8,4),
    BB_UP DECIMAL(10,2),
    BB_MID DECIMAL(10,2),
    BB_LO DECIMAL(10,2),
    K DECIMAL(8,4),
    D DECIMAL(8,4),
    ATR14 DECIMAL(10,4),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_date (code, date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS portfolio (
    id INT AUTO_INCREMENT PRIMARY KEY,
    type ENUM('config','position','order') NOT NULL,
    code VARCHAR(20),
    data JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_type (type),
    INDEX idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS watchlist (
    id INT AUTO_INCREMENT PRIMARY KEY,
    code VARCHAR(20) NOT NULL,
    name VARCHAR(50),
    added_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS configs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    config_key VARCHAR(50) NOT NULL,
    config_value JSON NOT NULL,
    description VARCHAR(200),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_key (config_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def init_database():
    """Create all tables, migrate schema, and clean up redundant indexes."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for stmt in _CREATE_TABLES.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            # Migrate: add indicator columns to existing tables
            _INDICATOR_COLS = [
                "MA5 DECIMAL(10,2)", "MA10 DECIMAL(10,2)", "MA20 DECIMAL(10,2)",
                "MA60 DECIMAL(10,2)", "DIF DECIMAL(10,6)", "DEA DECIMAL(10,6)",
                "MACD DECIMAL(10,6)", "RSI14 DECIMAL(8,4)",
                "BB_UP DECIMAL(10,2)", "BB_MID DECIMAL(10,2)", "BB_LO DECIMAL(10,2)",
                "K DECIMAL(8,4)", "D DECIMAL(8,4)", "ATR14 DECIMAL(10,4)",
            ]
            for col_def in _INDICATOR_COLS:
                try:
                    cur.execute(f"ALTER TABLE stock_history ADD COLUMN {col_def}")
                except Exception:
                    pass
            # Migrate: add preclose and amplitude columns
            for col_def in ["preclose DECIMAL(10,2)", "amplitude DECIMAL(8,4)"]:
                try:
                    cur.execute(f"ALTER TABLE stock_history ADD COLUMN {col_def}")
                except Exception:
                    pass
            # Drop redundant indexes
            for drop_idx in [
                "DROP INDEX idx_code ON stock_history",
                "DROP INDEX idx_date ON stock_history",
            ]:
                try:
                    cur.execute(drop_idx)
                except Exception:
                    pass


def backfill_amplitude():
    """Backfill amplitude column for existing rows that lack it.

    amplitude = (high - low) / prev_close * 100, where prev_close is the
    previous trading day's close (same as baostock's preclose semantics).
    Only updates rows where amplitude IS NULL.
    """
    codes = get_cached_stock_codes()
    if not codes:
        return 0

    updated = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for code in codes:
                # Load all rows for this stock, ordered by date
                cur.execute(
                    "SELECT date, high, low, close, amplitude "
                    "FROM stock_history WHERE code = %s ORDER BY date",
                    (code,),
                )
                rows = cur.fetchall()

                prev_close = None
                updates: list[tuple[float, str, str]] = []  # (amplitude, code, date)
                for r in rows:
                    if r["amplitude"] is not None:
                        prev_close = float(r["close"] or 0)
                        continue  # already has amplitude, just track prev_close

                    high_v = float(r["high"] or 0)
                    low_v = float(r["low"] or 0)
                    if prev_close is not None and prev_close > 0:
                        amp = (high_v - low_v) / prev_close * 100
                        updates.append((amp, code, str(r["date"])[:10]))
                    prev_close = float(r["close"] or 0)

                if updates:
                    cur.executemany(
                        "UPDATE stock_history SET amplitude = %s "
                        "WHERE code = %s AND date = %s",
                        updates,
                    )
                    updated += len(updates)

    return updated


# ── Stock History ───────────────────────────────────────────────────────

def save_stock_history(code: str, rows: list[dict]):
    """Save K-line data with pre-computed indicators for one stock."""
    if not rows:
        return

    # Ensure indicators are computed
    from data_engine import add_indicators
    rows = add_indicators(rows)

    _IND_COLS = [
        "MA5", "MA10", "MA20", "MA60", "DIF", "DEA", "MACD",
        "RSI14", "BB_UP", "BB_MID", "BB_LO", "K", "D", "ATR14",
    ]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cols = ["code", "date", "open", "high", "low", "close", "preclose",
                    "volume", "amount", "turn", "pctChg", "amplitude"] + _IND_COLS
            placeholders = ",".join(["%s"] * len(cols))
            # ON DUPLICATE KEY UPDATE：利用唯一键 uk_code_date(code,date) 去重，
            # 避免每次刷新都 DELETE 全量数据。UPDATE 子句需列出所有非键列。
            update_cols = [c for c in cols if c not in ("code", "date")]
            update_clause = ",".join(f"{c}=VALUES({c})" for c in update_cols)
            sql = (
                f"INSERT INTO stock_history ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_clause}"
            )

            data_rows = []
            for r in rows:
                row = [
                    code, str(r["date"])[:10],
                    r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                    r.get("preclose"),
                    r.get("volume", 0), r.get("amount", 0),
                    r.get("turn", 0), r.get("pctChg", 0),
                    r.get("amplitude"),
                ]
                for c in _IND_COLS:
                    row.append(r.get(c))
                data_rows.append(row)
            cur.executemany(sql, data_rows)


def load_stock_history(code: str) -> list[dict] | None:
    """Load K-line data with indicators for one stock. Returns list of row dicts."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT date, open, high, low, close, volume, amount, turn, pctChg, amplitude, "
                "MA5, MA10, MA20, MA60, DIF, DEA, MACD, RSI14, "
                "BB_UP, BB_MID, BB_LO, K, D, ATR14 "
                "FROM stock_history WHERE code = %s ORDER BY date",
                (code,),
            )
            rows = cur.fetchall()
    if not rows:
        return None
    # Convert numeric strings from DB to float/int
    _NUM_COLS = ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg", "amplitude",
                 "MA5", "MA10", "MA20", "MA60", "DIF", "DEA", "MACD", "RSI14",
                 "BB_UP", "BB_MID", "BB_LO", "K", "D", "ATR14"]
    for r in rows:
        for col in _NUM_COLS:
            if col in r and r[col] is not None:
                try:
                    r[col] = float(r[col]) if col != "volume" else int(r[col])
                except (ValueError, TypeError):
                    pass
    return rows


def get_cached_stock_codes() -> list[str]:
    """Return all stock codes that have history data in DB."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT code FROM stock_history ORDER BY code")
            return [r["code"] for r in cur.fetchall()]


def get_latest_dates_batch(codes: list[str]) -> dict[str, str]:
    """Get the latest trading date for each stock code in one query.

    Returns {code: "YYYY-MM-DD"}. Codes without data are omitted.
    """
    if not codes:
        return {}
    placeholders = ",".join(["%s"] * len(codes))
    sql = (
        "SELECT code, MAX(date) AS max_date FROM stock_history "
        f"WHERE code IN ({placeholders}) GROUP BY code"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, codes)
            return {r["code"]: str(r["max_date"]) for r in cur.fetchall()}


# ── Batch Operations (performance optimized) ────────────────────────────

def prefilter_codes_by_latest(config_filters: dict) -> list[str] | None:
    """Push simple filters (price, turnover, pct_change, amplitude) to SQL.

    Only examines the latest trading day per stock via a subquery join.
    Uses stored amplitude column — MySQL 5.7 compatible (no CTE/LAG).
    Returns a list of codes that pass all enabled simple filters, or None
    if no simple filters are enabled (meaning: caller should load all codes).
    """
    conditions = []
    params = []

    for key, cfg in config_filters.items():
        if not cfg.get("enabled", False):
            continue
        if key == "turnover":
            conditions.append("sh.turn BETWEEN %s AND %s")
            params.extend([float(cfg["min"]), float(cfg.get("max", 100))])
        elif key == "amplitude":
            conditions.append("sh.amplitude >= %s")
            params.append(float(cfg["min"]))
            if "max" in cfg:
                conditions.append("sh.amplitude <= %s")
                params.append(float(cfg["max"]))
        elif key == "pct_change":
            conditions.append("sh.pctChg BETWEEN %s AND %s")
            params.extend([float(cfg["min"]), float(cfg["max"])])
        elif key == "price_range":
            conditions.append("sh.close BETWEEN %s AND %s")
            params.extend([float(cfg["min"]), float(cfg["max"])])

    if not conditions:
        return None  # no simple filters → caller loads all codes

    sql = (
        "SELECT sh.code FROM stock_history sh "
        "INNER JOIN ("
        "  SELECT code, MAX(date) AS max_date FROM stock_history GROUP BY code"
        ") latest ON sh.code = latest.code AND sh.date = latest.max_date "
        "WHERE " + " AND ".join(conditions) + " "
        "ORDER BY sh.code"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [r["code"] for r in cur.fetchall()]


def load_stock_history_batch(codes: list[str], days: int = 120) -> dict[str, list[dict]]:
    """Load K-line data (with pre-computed indicators) for many stocks in ONE query."""
    if not codes:
        return {}

    placeholders = ",".join(["%s"] * len(codes))
    sql = (
        "SELECT code, date, open, high, low, close, volume, amount, turn, pctChg, amplitude, "
        "MA5, MA10, MA20, MA60, DIF, DEA, MACD, RSI14, "
        "BB_UP, BB_MID, BB_LO, K, D, ATR14 "
        "FROM stock_history "
        f"WHERE code IN ({placeholders}) "
        "ORDER BY code, date"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, codes)
            rows = cur.fetchall()

    if not rows:
        return {}

    _NUM_COLS = ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg", "amplitude",
                 "MA5", "MA10", "MA20", "MA60", "DIF", "DEA", "MACD", "RSI14",
                 "BB_UP", "BB_MID", "BB_LO", "K", "D", "ATR14"]

    result: dict[str, list[dict]] = {}
    for r in rows:
        code = r["code"]
        if code not in result:
            result[code] = []
        # Convert numeric fields
        for col in _NUM_COLS:
            if col in r and r[col] is not None:
                try:
                    r[col] = float(r[col]) if col != "volume" else int(r[col])
                except (ValueError, TypeError):
                    pass
        result[code].append(r)

    # Keep only the last `days` rows per code
    for code in result:
        if len(result[code]) > days:
            result[code] = result[code][-days:]

    return result


def load_latest_indicators_batch(codes: list[str]) -> dict[str, dict]:
    """Load the LATEST TWO rows (with pre-computed indicators) for multiple stocks.

    Returns dict[code] = {"last": {...}, "prev": {...}}
    MySQL 5.7 compatible — loads all rows and filters in Python (fast enough for
    typical usage; each stock contributes only 2 rows to the result).
    """
    if not codes:
        return {}

    placeholders = ",".join(["%s"] * len(codes))
    sql = (
        "SELECT code, date, open, high, low, close, volume, amount, turn, pctChg, amplitude, "
        "MA5, MA10, MA20, MA60, DIF, DEA, MACD, RSI14, "
        "BB_UP, BB_MID, BB_LO, K, D, ATR14 "
        "FROM stock_history "
        f"WHERE code IN ({placeholders}) "
        "ORDER BY code, date DESC"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, codes)
            rows = cur.fetchall()

    result: dict[str, dict] = {}
    for r in rows:
        code = r["code"]
        if code not in result:
            result[code] = {"last": r, "prev": None}
        elif result[code]["prev"] is None:
            result[code]["prev"] = r
        # else: already have 2 rows for this code, skip

    return result


def get_avg_volume_batch(codes: list[str], days: int = 5) -> dict[str, float]:
    """Get average volume of last N days for each code (used by volume_rate filter)."""
    if not codes:
        return {}
    placeholders = ",".join(["%s"] * len(codes))
    sql = (
        "SELECT code, volume FROM stock_history "
        f"WHERE code IN ({placeholders}) "
        "ORDER BY code, date"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, codes)
            rows = cur.fetchall()

    # Group by code, take last N days, compute average
    from collections import defaultdict
    code_data: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        code_data[r["code"]].append(int(r["volume"] or 0))

    result: dict[str, float] = {}
    for code, vols in code_data.items():
        recent = vols[-days:] if len(vols) > days else vols
        result[code] = sum(recent) / len(recent) if recent else 0.0
    return result


def get_avg_amplitude_turnover_batch(codes: list[str], days: int = 120) -> dict[str, dict]:
    """Compute N-day average amplitude and average turnover for each code.

    Uses stored amplitude column. MySQL 5.7 compatible.
    Returns {code: {"avg_amplitude": float, "avg_turnover": float}}.
    """
    if not codes:
        return {}

    placeholders = ",".join(["%s"] * len(codes))
    sql = (
        "SELECT code, amplitude, turn "
        "FROM stock_history "
        f"WHERE code IN ({placeholders}) "
        "ORDER BY code, date"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, codes)
            rows = cur.fetchall()

    # Group by code, take last N days, compute averages
    from collections import defaultdict
    code_data: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        code_data[r["code"]].append({
            "amplitude": float(r["amplitude"] or 0),
            "turn": float(r["turn"] or 0),
        })

    result: dict[str, dict] = {}
    min_required = max(days // 2, 20)  # at least half the period, floor 20 trading days
    for code, data in code_data.items():
        if len(data) < min_required:
            continue  # insufficient history, skip
        recent = data[-days:] if len(data) > days else data
        amps = [d["amplitude"] for d in recent if d["amplitude"] is not None and d["amplitude"] > 0]
        turns = [d["turn"] for d in recent]
        result[code] = {
            "avg_amplitude": sum(amps) / len(amps) if amps else 0.0,
            "avg_turnover": sum(turns) / len(turns) if turns else 0.0,
        }

    return result


# ── Portfolio ───────────────────────────────────────────────────────────

def save_portfolio(data: dict):
    """Save full portfolio state (config + positions + orders)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM portfolio")
            # Config row
            cur.execute(
                "INSERT INTO portfolio (type, code, data) VALUES ('config', NULL, %s)",
                (json.dumps({
                    "cash": data["cash"],
                    "initial_cash": data["initial_cash"],
                    "order_seq": data["order_seq"],
                }, ensure_ascii=False),),
            )
            # Position rows
            for code, pos in data.get("positions", {}).items():
                cur.execute(
                    "INSERT INTO portfolio (type, code, data) VALUES ('position', %s, %s)",
                    (code, json.dumps(pos, ensure_ascii=False)),
                )
            # Order rows
            for order in data.get("orders", []):
                cur.execute(
                    "INSERT INTO portfolio (type, code, data) VALUES ('order', %s, %s)",
                    (order.get("code"), json.dumps(order, ensure_ascii=False)),
                )


def load_portfolio() -> dict | None:
    """Load full portfolio state. Returns None if empty."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT type, code, data FROM portfolio")
            rows = cur.fetchall()
    if not rows:
        return None

    result = {"cash": 1_000_000.0, "initial_cash": 1_000_000.0, "order_seq": 1, "positions": {}, "orders": []}
    for row in rows:
        d = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        if row["type"] == "config":
            result["cash"] = d.get("cash", result["cash"])
            result["initial_cash"] = d.get("initial_cash", result["initial_cash"])
            result["order_seq"] = d.get("order_seq", result["order_seq"])
        elif row["type"] == "position":
            result["positions"][row["code"]] = d
        elif row["type"] == "order":
            result["orders"].append(d)
    return result


# ── Watchlist ───────────────────────────────────────────────────────────

def save_watchlist(codes: list[str]):
    """Replace watchlist with given codes."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watchlist")
            if codes:
                cur.executemany(
                    "INSERT INTO watchlist (code) VALUES (%s)",
                    [(c,) for c in codes],
                )


def load_watchlist() -> list[str]:
    """Load watchlist codes. Returns empty list if none."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM watchlist ORDER BY id")
            return [r["code"] for r in cur.fetchall()]


# ── Configs ─────────────────────────────────────────────────────────────

def save_config(key: str, value: dict, description: str = ""):
    """Upsert a config entry."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO configs (config_key, config_value, description) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE config_value = VALUES(config_value), description = VALUES(description)",
                (key, json.dumps(value, ensure_ascii=False), description),
            )


def load_config(key: str) -> dict | None:
    """Load a config entry. Returns None if not found."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT config_value FROM configs WHERE config_key = %s", (key,))
            row = cur.fetchone()
    if not row:
        return None
    v = row["config_value"]
    return json.loads(v) if isinstance(v, str) else v
