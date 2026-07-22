"""Data access layer: stock_code + stock_detail CRUD and batch queries.

All database access for the two core tables lives here.
"""
from __future__ import annotations
from collections import defaultdict

from db_config import get_connection


# ── stock_code ────────────────────────────────────────────────────────────

def get_all_stock_ids() -> list[dict]:
    """Return all stocks: [{id, code, name}, ...]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, code, name FROM stock_code ORDER BY code")
            return [{"id": r["id"], "code": r["code"], "name": r["name"]} for r in cur.fetchall()]


def get_stock_id_map() -> dict[str, int]:
    """Return {code: stock_id} mapping."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, code FROM stock_code")
            return {r["code"]: r["id"] for r in cur.fetchall()}


def get_cached_count() -> int:
    """Number of stocks with at least one daily record."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(DISTINCT stock_id) AS cnt FROM stock_detail")
            row = cur.fetchone()
            return row["cnt"] if row else 0


# ── stock_detail: single-stock queries ────────────────────────────────────

_DETAIL_SELECT = (
    "date, open, high, low, close, preclose, volume, amount, turn, pct_chg"
)
_DETAIL_NUM_COLS = [
    "open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pct_chg",
]


def _convert_numeric(row: dict):
    for col in _DETAIL_NUM_COLS:
        if col in row and row[col] is not None:
            try:
                row[col] = float(row[col]) if col != "volume" else int(row[col])
            except (ValueError, TypeError):
                pass


def load_history(stock_id: int) -> list[dict] | None:
    """Load all K-line rows for one stock, ordered by date."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_DETAIL_SELECT} FROM stock_detail "
                "WHERE stock_id = %s ORDER BY date",
                (stock_id,),
            )
            rows = cur.fetchall()
    if not rows:
        return None
    for r in rows:
        _convert_numeric(r)
    return rows


def load_history_tail(stock_id: int, n: int = 150) -> list[dict] | None:
    """Load the last N rows (chronological), fast via LIMIT."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_DETAIL_SELECT} FROM stock_detail "
                "WHERE stock_id = %s ORDER BY date DESC LIMIT %s",
                (stock_id, n),
            )
            rows = cur.fetchall()
    if not rows:
        return None
    rows.reverse()
    for r in rows:
        _convert_numeric(r)
    return rows


# ── stock_detail: batch queries (performance-critical) ────────────────────

def prefilter_by_latest(config_filters: dict) -> list[int] | None:
    """Push simple filters (turn, pct_chg, price) to SQL via subquery on latest date.

    Returns list of stock_id that pass, or None if no simple filters enabled.
    """
    conditions = []
    params = []

    for key, cfg in config_filters.items():
        if not cfg.get("enabled", False):
            continue
        if key == "turnover":
            conditions.append("sd.turn BETWEEN %s AND %s")
            params.extend([float(cfg["min"]), float(cfg.get("max", 100))])
        elif key == "amplitude":
            # amplitude is computed: (high-low)/preclose*100 — push as SQL expression
            conditions.append("(sd.high - sd.low) / NULLIF(sd.preclose, 0) * 100 >= %s")
            params.append(float(cfg["min"]))
            if "max" in cfg:
                conditions.append("(sd.high - sd.low) / NULLIF(sd.preclose, 0) * 100 <= %s")
                params.append(float(cfg["max"]))
        elif key == "pct_change":
            conditions.append("sd.pct_chg BETWEEN %s AND %s")
            params.extend([float(cfg["min"]), float(cfg["max"])])
        elif key == "price_range":
            conditions.append("sd.close BETWEEN %s AND %s")
            params.extend([float(cfg["min"]), float(cfg["max"])])

    if not conditions:
        return None

    sql = (
        "SELECT sd.stock_id FROM stock_detail sd "
        "INNER JOIN ("
        "  SELECT stock_id, MAX(date) AS max_date FROM stock_detail GROUP BY stock_id"
        ") latest ON sd.stock_id = latest.stock_id AND sd.date = latest.max_date "
        "WHERE " + " AND ".join(conditions) + " "
        "ORDER BY sd.stock_id"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [r["stock_id"] for r in cur.fetchall()]


def load_history_batch(stock_ids: list[int], days: int = 120) -> dict[int, list[dict]]:
    """Load recent K-line for many stocks in ONE query."""
    if not stock_ids:
        return {}

    placeholders = ",".join(["%s"] * len(stock_ids))
    sql = (
        f"SELECT stock_id, {_DETAIL_SELECT} FROM stock_detail "
        f"WHERE stock_id IN ({placeholders}) "
        "AND date >= DATE_SUB((SELECT MAX(date) FROM stock_detail), INTERVAL %s DAY) "
        "ORDER BY stock_id, date"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, stock_ids + [days + 10])
            rows = cur.fetchall()

    if not rows:
        return {}

    result: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        sid = r["stock_id"]
        del r["stock_id"]
        _convert_numeric(r)
        result[sid].append(r)

    # Keep only last `days` rows per stock
    for sid in result:
        if len(result[sid]) > days:
            result[sid] = result[sid][-days:]

    return dict(result)


def load_latest_indicators_batch(stock_ids: list[int]) -> dict[int, dict]:
    """Load latest 2 rows for each stock (for cross-detection: MACD golden cross, etc.).

    Returns {stock_id: {"last": {...}, "prev": {...}}}.
    """
    if not stock_ids:
        return {}

    placeholders = ",".join(["%s"] * len(stock_ids))
    sql = (
        f"SELECT stock_id, {_DETAIL_SELECT} FROM stock_detail "
        f"WHERE stock_id IN ({placeholders}) ORDER BY stock_id, date DESC"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, stock_ids)
            rows = cur.fetchall()

    result: dict[int, dict] = {}
    for r in rows:
        sid = r["stock_id"]
        del r["stock_id"]
        _convert_numeric(r)
        if sid not in result:
            result[sid] = {"last": r, "prev": None}
        elif result[sid]["prev"] is None:
            result[sid]["prev"] = r

    return result


def get_avg_volume_batch(stock_ids: list[int], days: int = 5) -> dict[int, float]:
    """Get N-day average volume per stock (for volume_rate filter)."""
    if not stock_ids:
        return {}

    placeholders = ",".join(["%s"] * len(stock_ids))
    sql = (
        "SELECT stock_id, volume FROM stock_detail "
        f"WHERE stock_id IN ({placeholders}) ORDER BY stock_id, date"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, stock_ids)
            rows = cur.fetchall()

    code_data: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        code_data[r["stock_id"]].append(int(r["volume"] or 0))

    result: dict[int, float] = {}
    for sid, vols in code_data.items():
        recent = vols[-days:] if len(vols) > days else vols
        result[sid] = sum(recent) / len(recent) if recent else 0.0
    return result


def get_avg_amplitude_turnover_batch(stock_ids: list[int], days: int = 120) -> dict[int, dict]:
    """Compute N-day avg amplitude (computed) and avg turnover (stored) per stock.

    Returns {stock_id: {"avg_amplitude": float, "avg_turnover": float}}.
    """
    if not stock_ids:
        return {}

    placeholders = ",".join(["%s"] * len(stock_ids))
    sql = (
        "SELECT stock_id, high, low, preclose, turn FROM stock_detail "
        f"WHERE stock_id IN ({placeholders}) ORDER BY stock_id, date"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, stock_ids)
            rows = cur.fetchall()

    code_data: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        code_data[r["stock_id"]].append({
            "high": float(r["high"] or 0),
            "low": float(r["low"] or 0),
            "preclose": float(r["preclose"] or 0),
            "turn": float(r["turn"] or 0),
        })

    min_required = max(days // 2, 20)
    result: dict[int, dict] = {}
    for sid, data in code_data.items():
        if len(data) < min_required:
            continue
        recent = data[-days:] if len(data) > days else data
        amps = []
        for d in recent:
            if d["preclose"] > 0 and d["high"] is not None and d["low"] is not None:
                amps.append((d["high"] - d["low"]) / d["preclose"] * 100)
        turns = [d["turn"] for d in recent]
        result[sid] = {
            "avg_amplitude": sum(amps) / len(amps) if amps else 0.0,
            "avg_turnover": sum(turns) / len(turns) if turns else 0.0,
        }

    return result
