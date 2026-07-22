"""Utility data access: portfolio, watchlist, configs.

Core stock data (stock_code + stock_detail) has moved to:
  db_schema.py       → table creation
  repository/stock_repo.py → CRUD & queries
"""
from __future__ import annotations
import json
from db_config import get_connection


# ── Portfolio ───────────────────────────────────────────────────────────

def save_portfolio(data: dict):
    """Save full portfolio state (config + positions + orders)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM portfolio")
            cur.execute(
                "INSERT INTO portfolio (type, code, data) VALUES ('config', NULL, %s)",
                (json.dumps({
                    "cash": data["cash"],
                    "initial_cash": data["initial_cash"],
                    "order_seq": data["order_seq"],
                }, ensure_ascii=False),),
            )
            for code, pos in data.get("positions", {}).items():
                cur.execute(
                    "INSERT INTO portfolio (type, code, data) VALUES ('position', %s, %s)",
                    (code, json.dumps(pos, ensure_ascii=False)),
                )
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

    result = {
        "cash": 1_000_000.0, "initial_cash": 1_000_000.0,
        "order_seq": 1, "positions": {}, "orders": [],
    }
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
    """Load watchlist codes."""
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
                "ON DUPLICATE KEY UPDATE config_value=VALUES(config_value), description=VALUES(description)",
                (key, json.dumps(value, ensure_ascii=False), description),
            )


def load_config(key: str) -> dict | None:
    """Load a config entry."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT config_value FROM configs WHERE config_key = %s", (key,))
            row = cur.fetchone()
    if not row:
        return None
    v = row["config_value"]
    return json.loads(v) if isinstance(v, str) else v
