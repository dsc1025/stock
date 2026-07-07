"""Database manager: CRUD operations for stock data in MySQL."""
from __future__ import annotations
import json
import pandas as pd
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
    volume BIGINT,
    amount DECIMAL(18,2),
    turn DECIMAL(8,4),
    pctChg DECIMAL(8,4),
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
            # Drop redundant indexes
            for drop_idx in [
                "DROP INDEX idx_code ON stock_history",
                "DROP INDEX idx_date ON stock_history",
            ]:
                try:
                    cur.execute(drop_idx)
                except Exception:
                    pass


# ── Stock History ───────────────────────────────────────────────────────

def save_stock_history(code: str, df: pd.DataFrame):
    """Save K-line data with pre-computed indicators for one stock."""
    if df.empty:
        return

    # Ensure indicators are computed
    from data_engine import add_indicators
    df = add_indicators(df)

    _IND_COLS = [
        "MA5", "MA10", "MA20", "MA60", "DIF", "DEA", "MACD",
        "RSI14", "BB_UP", "BB_MID", "BB_LO", "K", "D", "ATR14",
    ]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM stock_history WHERE code = %s", (code,))
            cols = ["code", "date", "open", "high", "low", "close",
                    "volume", "amount", "turn", "pctChg"] + _IND_COLS
            placeholders = ",".join(["%s"] * len(cols))
            sql = f"INSERT INTO stock_history ({','.join(cols)}) VALUES ({placeholders})"

            rows = []
            for _, r in df.iterrows():
                row = [
                    code, str(r["date"])[:10],
                    _f(r, "open"), _f(r, "high"), _f(r, "low"), _f(r, "close"),
                    _i(r, "volume"), _f(r, "amount"), _f(r, "turn"), _f(r, "pctChg"),
                ]
                for c in _IND_COLS:
                    row.append(_f(r, c))
                rows.append(row)
            cur.executemany(sql, rows)


def _f(row, col, default=None):
    """Safe float from pandas row."""
    v = row.get(col)
    return float(v) if pd.notna(v) else default


def _i(row, col, default=None):
    """Safe int from pandas row."""
    v = row.get(col)
    return int(v) if pd.notna(v) else default


def load_stock_history(code: str) -> pd.DataFrame | None:
    """Load K-line data with indicators for one stock."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT date, open, high, low, close, volume, amount, turn, pctChg, "
                "MA5, MA10, MA20, MA60, DIF, DEA, MACD, RSI14, "
                "BB_UP, BB_MID, BB_LO, K, D, ATR14 "
                "FROM stock_history WHERE code = %s ORDER BY date",
                (code,),
            )
            rows = cur.fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    _NUM_COLS = ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg",
                 "MA5", "MA10", "MA20", "MA60", "DIF", "DEA", "MACD", "RSI14",
                 "BB_UP", "BB_MID", "BB_LO", "K", "D", "ATR14"]
    for col in _NUM_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_cached_stock_codes() -> list[str]:
    """Return all stock codes that have history data in DB."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT code FROM stock_history ORDER BY code")
            return [r["code"] for r in cur.fetchall()]


# ── Batch Operations (performance optimized) ────────────────────────────

def prefilter_codes_by_latest(config_filters: dict) -> list[str] | None:
    """Push simple filters (price, turnover, pct_change, amplitude) to SQL.

    Only examines the latest trading day per stock via a subquery join.
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
            conditions.append("(sh.high - sh.low) / NULLIF(sh.open, 0) * 100 >= %s")
            params.append(float(cfg["min"]))
            if "max" in cfg:
                conditions.append("(sh.high - sh.low) / NULLIF(sh.open, 0) * 100 <= %s")
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


def load_stock_history_batch(codes: list[str], days: int = 120) -> dict[str, pd.DataFrame]:
    """Load K-line data (with pre-computed indicators) for many stocks in ONE query."""
    if not codes:
        return {}

    placeholders = ",".join(["%s"] * len(codes))
    sql = (
        "SELECT code, date, open, high, low, close, volume, amount, turn, pctChg, "
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

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    _NUM_COLS = ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg",
                 "MA5", "MA10", "MA20", "MA60", "DIF", "DEA", "MACD", "RSI14",
                 "BB_UP", "BB_MID", "BB_LO", "K", "D", "ATR14"]
    for col in _NUM_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    result = {}
    for code, group in df.groupby("code"):
        group = group.sort_values("date").tail(days).reset_index(drop=True)
        result[code] = group

    return result


def load_latest_indicators_batch(codes: list[str]) -> dict[str, dict]:
    """Load the LATEST TWO rows (with pre-computed indicators) for multiple stocks.

    Returns dict[code] = {"last": {...}, "prev": {...}}
    Uses MySQL window function ROW_NUMBER() for efficiency.
    No pandas needed — returns plain dicts from the cursor.
    """
    if not codes:
        return {}

    placeholders = ",".join(["%s"] * len(codes))
    sql = (
        "SELECT code, date, open, high, low, close, volume, amount, turn, pctChg, "
        "MA5, MA10, MA20, MA60, DIF, DEA, MACD, RSI14, "
        "BB_UP, BB_MID, BB_LO, K, D, ATR14 "
        "FROM ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn "
        "  FROM stock_history "
        f" WHERE code IN ({placeholders})"
        ") t WHERE rn <= 2 "
        "ORDER BY code, date"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, codes)
            rows = cur.fetchall()

    result: dict[str, dict] = {}
    for r in rows:
        code = r["code"]
        if code not in result:
            result[code] = {"last": None, "prev": None}
        # rows are ordered by code, date — first row is prev, second is last
        if result[code]["last"] is None:
            # First encounter for this code = older row (prev)
            result[code]["prev"] = r
            result[code]["last"] = r  # will be overwritten by next row
        else:
            result[code]["prev"] = result[code]["last"]
            result[code]["last"] = r

    return result


def get_avg_volume_batch(codes: list[str], days: int = 5) -> dict[str, float]:
    """Get average volume of last N days for each code (used by volume_rate filter)."""
    if not codes:
        return {}
    placeholders = ",".join(["%s"] * len(codes))
    # Use window function to get last N rows per code efficiently
    sql = (
        "SELECT code, AVG(volume) AS avg_vol FROM ("
        "  SELECT code, volume, "
        "    ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn "
        "  FROM stock_history "
        f" WHERE code IN ({placeholders})"
        f") t WHERE rn <= {days} "
        "GROUP BY code"
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, codes)
            return {r["code"]: float(r["avg_vol"] or 0) for r in cur.fetchall()}


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
