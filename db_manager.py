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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_date (code, date),
    INDEX idx_code (code),
    INDEX idx_date (date)
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
    """Create all tables if they don't exist."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for stmt in _CREATE_TABLES.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)


# ── Stock History ───────────────────────────────────────────────────────

def save_stock_history(code: str, df: pd.DataFrame):
    """Upsert K-line data for one stock. Replaces all rows for that code."""
    if df.empty:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Delete existing rows for this code, then bulk insert
            cur.execute("DELETE FROM stock_history WHERE code = %s", (code,))
            sql = (
                "INSERT INTO stock_history (code, date, open, high, low, close, volume, amount, turn, pctChg) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            )
            rows = []
            for _, r in df.iterrows():
                rows.append((
                    code,
                    str(r["date"])[:10],
                    float(r["open"]) if pd.notna(r["open"]) else None,
                    float(r["high"]) if pd.notna(r["high"]) else None,
                    float(r["low"]) if pd.notna(r["low"]) else None,
                    float(r["close"]) if pd.notna(r["close"]) else None,
                    int(r["volume"]) if pd.notna(r["volume"]) else None,
                    float(r["amount"]) if pd.notna(r["amount"]) else None,
                    float(r["turn"]) if pd.notna(r["turn"]) else None,
                    float(r["pctChg"]) if pd.notna(r["pctChg"]) else None,
                ))
            cur.executemany(sql, rows)


def load_stock_history(code: str) -> pd.DataFrame | None:
    """Load K-line data for one stock. Returns None if not found."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT date, open, high, low, close, volume, amount, turn, pctChg "
                "FROM stock_history WHERE code = %s ORDER BY date",
                (code,),
            )
            rows = cur.fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_cached_stock_codes() -> list[str]:
    """Return all stock codes that have history data in DB."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT code FROM stock_history ORDER BY code")
            return [r["code"] for r in cur.fetchall()]


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
