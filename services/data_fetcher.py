"""Baostock + Sina Finance API wrapper — pure data fetching, no caching.

All functions return raw data, no side effects.
"""
from __future__ import annotations
import logging
import re
import urllib.request
from datetime import datetime, timedelta

import baostock as bs

logging.getLogger("baostock").setLevel(logging.ERROR)


# ── Session management ────────────────────────────────────────────────────

def _bs_login():
    lg = bs.login()
    if lg.error_code != "0":
        print(f"[baostock] initial login failed: code={lg.error_code}, msg={lg.error_msg}")


def _bs_force_login() -> bool:
    """Re-login to baostock. Returns True on success."""
    import time as _time
    _time.sleep(0.5)
    lg = bs.login()
    if lg.error_code != "0":
        print(f"[baostock] login failed: code={lg.error_code}, msg={lg.error_msg}")
    return lg.error_code == "0"


def login():
    _bs_login()


def logout():
    bs.logout()


# ── Stock list ────────────────────────────────────────────────────────────

def fetch_all_stock_codes() -> list[str]:
    """Get all A-share stock codes from baostock (excluding indices/ETFs).

    Filters: sh.6*, sh.9*, sz.0*, sz.3*
    Retries up to 3 times; walks back up to 7 days to find a valid trading day.
    """
    import time as _time
    for attempt in range(3):
        print(f"[baostock] login attempt {attempt + 1}/3...")
        if not _bs_force_login():
            print(f"[baostock] login attempt {attempt + 1} failed")
            if attempt < 2:
                _time.sleep(2)
                continue
            return []
        for delta in range(7):
            day = (datetime.today() - timedelta(days=delta)).strftime("%Y-%m-%d")
            print(f"[baostock] query_all_stock day={day}...")
            try:
                rs = bs.query_all_stock(day=day)
            except Exception as ex:
                print(f"[baostock] query_all_stock exception: {ex}")
                break
            print(f"[baostock] error_code={rs.error_code}, error_msg={rs.error_msg}")
            if rs.error_code != "0":
                continue
            codes = []
            while rs.next():
                code = rs.get_row_data()[0]
                if code.startswith("sh.6") or code.startswith("sz.0") or code.startswith("sz.3"):
                    codes.append(code)
            print(f"[baostock] day={day} found {len(codes)} stocks")
            if codes:
                return codes
        if attempt < 2:
            _time.sleep(2)
    return []


# ── Stock basic info ──────────────────────────────────────────────────────

def fetch_stock_basic(code: str) -> dict | None:
    """Get basic info for a single stock: code_name, ipoDate, outDate, type, status.

    Returns None if baostock returns no data.
    """
    rs = bs.query_stock_basic(code=code)
    if rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        return dict(zip(rs.fields, row))
    return None


# ── K-line data ───────────────────────────────────────────────────────────

BAOSTOCK_KLINE_FIELDS = "date,open,high,low,close,preclose,volume,amount,turn,pctChg"


def _parse_kline_rows(rs) -> list[dict]:
    """Parse baostock query_history_k_data_plus result into list[dict]."""
    rows: list[dict] = []
    while rs.error_code == "0" and rs.next():
        r = rs.get_row_data()
        try:
            row = {
                "date":     r[0],
                "open":     float(r[1]) if r[1] else None,
                "high":     float(r[2]) if r[2] else None,
                "low":      float(r[3]) if r[3] else None,
                "close":    float(r[4]) if r[4] else None,
                "preclose": float(r[5]) if r[5] else None,
                "volume":   int(float(r[6])) if r[6] else 0,
                "amount":   float(r[7]) if r[7] else 0.0,
                "turn":     float(r[8]) if r[8] else 0.0,
                "pct_chg":  float(r[9]) if r[9] else 0.0,
            }
        except (ValueError, IndexError):
            continue
        if row["close"] is not None:
            rows.append(row)
    return rows


def fetch_kline_range(code: str, start_date: str, end_date: str) -> list[dict]:
    """Fetch K-line data between explicit start/end dates (inclusive)."""
    rs = bs.query_history_k_data_plus(
        code,
        BAOSTOCK_KLINE_FIELDS,
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="3",
    )
    return _parse_kline_rows(rs)


def fetch_kline(code: str, days: int = 730) -> list[dict]:
    """Fetch recent N trading days of K-line data."""
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
    rows = fetch_kline_range(code, start, end)
    return rows[-days:] if len(rows) > days else rows


# ── Sina Finance real-time quotes ─────────────────────────────────────────

def _to_sina_code(code: str) -> str:
    return code.replace(".", "")


def fetch_realtime_quotes(codes: list[str]) -> list[dict]:
    """Fetch real-time quotes via Sina Finance in one batch HTTP request.

    Returns list of dicts: code, name, open, high, low, close, prev_close,
    volume, amount, pctChg, amplitude, time.
    """
    if not codes:
        return []

    sina_codes = ",".join(_to_sina_code(c) for c in codes)
    url = f"http://hq.sinajs.cn/list={sina_codes}"
    req = urllib.request.Request(url, headers={"Referer": "http://finance.sina.com.cn"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            content = resp.read().decode("gbk", errors="replace")
    except Exception:
        return []

    result = []
    for code in codes:
        sina_code = _to_sina_code(code)
        m = re.search(rf'hq_str_{re.escape(sina_code)}="([^"]*)"', content)
        if not m or not m.group(1).strip(","):
            continue
        fields = m.group(1).split(",")
        if len(fields) < 32:
            continue
        try:
            name       = fields[0]
            open_      = float(fields[1])  if fields[1]  else 0.0
            prev_close = float(fields[2])  if fields[2]  else 0.0
            current    = float(fields[3])  if fields[3]  else 0.0
            high       = float(fields[4])  if fields[4]  else 0.0
            low        = float(fields[5])  if fields[5]  else 0.0
            volume     = float(fields[8])  if fields[8]  else 0.0
            amount     = float(fields[9])  if fields[9]  else 0.0
            pct_chg    = (current - prev_close) / prev_close * 100 if prev_close else 0.0
            amplitude  = (high - low) / prev_close * 100 if prev_close else 0.0
            time_str   = fields[31] if len(fields) > 31 else ""
        except (ValueError, IndexError):
            continue

        result.append({
            "code":       code,
            "name":       name,
            "open":       open_,
            "high":       high,
            "low":        low,
            "close":      current,
            "prev_close": prev_close,
            "volume":     volume,
            "amount":     amount,
            "pctChg":     pct_chg,
            "amplitude":  amplitude,
            "time":       time_str,
        })
    return result
