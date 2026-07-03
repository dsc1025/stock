"""
Data engine: real-time quotes via Sina Finance API + historical K-line via baostock.
Real-time quotes use a single batch HTTP request (one call for all watchlist stocks).
Historical OHLCV data (for indicator/signal analysis) still uses baostock.
"""
from __future__ import annotations
import baostock as bs
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
import urllib.request
import re

logging.getLogger("baostock").setLevel(logging.ERROR)


def login():
    bs.login()


def logout():
    bs.logout()


def get_stock_history(code: str, days: int = 60) -> pd.DataFrame:
    """Fetch OHLCV daily data for the past `days` days."""
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=days + 30)).strftime("%Y-%m-%d")

    rs = bs.query_history_k_data_plus(
        code,
        "date,open,high,low,close,volume,amount,turn,pctChg",
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag="3",
    )
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=rs.fields)
    numeric_cols = ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"]).tail(days).reset_index(drop=True)
    return df


def get_stock_basic(code: str) -> dict:
    """Get basic stock info."""
    rs = bs.query_stock_basic(code=code)
    if rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        return dict(zip(rs.fields, row))
    return {}


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add MA, EMA, MACD, RSI, Bollinger Bands, KDJ to dataframe."""
    if df.empty or len(df) < 5:
        return df

    close = df["close"]
    high = df["high"]
    low = df["low"]

    # Moving Averages
    for n in [5, 10, 20, 60]:
        df[f"MA{n}"] = close.rolling(n).mean()

    # EMA
    df["EMA12"] = close.ewm(span=12, adjust=False).mean()
    df["EMA26"] = close.ewm(span=26, adjust=False).mean()

    # MACD
    df["DIF"] = df["EMA12"] - df["EMA26"]
    df["DEA"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD"] = (df["DIF"] - df["DEA"]) * 2

    # RSI (14)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI14"] = 100 - 100 / (1 + rs)

    # Bollinger Bands (20, 2)
    df["BB_MID"] = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    df["BB_UP"] = df["BB_MID"] + 2 * std20
    df["BB_LO"] = df["BB_MID"] - 2 * std20

    # KDJ (9, 3, 3)
    low9 = low.rolling(9).min()
    high9 = high.rolling(9).max()
    rsv = (close - low9) / (high9 - low9).replace(0, np.nan) * 100
    df["K"] = rsv.ewm(com=2, adjust=False).mean()
    df["D"] = df["K"].ewm(com=2, adjust=False).mean()
    df["J"] = 3 * df["K"] - 2 * df["D"]

    # ATR (14)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["ATR14"] = tr.rolling(14).mean()

    return df


def generate_signals(df: pd.DataFrame) -> list[str]:
    """Generate simple buy/sell signals from indicators."""
    if df.empty or len(df) < 2:
        return []
    last = df.iloc[-1]
    prev = df.iloc[-2]
    signals = []

    # MACD golden/death cross
    if prev["DIF"] < prev["DEA"] and last["DIF"] > last["DEA"]:
        signals.append("MACD 金叉 — 看多信号")
    elif prev["DIF"] > prev["DEA"] and last["DIF"] < last["DEA"]:
        signals.append("MACD 死叉 — 看空信号")

    # RSI overbought/oversold
    if last["RSI14"] > 70:
        signals.append(f"RSI={last['RSI14']:.1f} 超买区间 — 注意回调风险")
    elif last["RSI14"] < 30:
        signals.append(f"RSI={last['RSI14']:.1f} 超卖区间 — 可能反弹")

    # Bollinger breakout
    if last["close"] > last["BB_UP"]:
        signals.append("价格突破布林上轨 — 强势但超买")
    elif last["close"] < last["BB_LO"]:
        signals.append("价格跌破布林下轨 — 弱势但超卖")

    # MA trend
    if last["close"] > last.get("MA20", float("nan")) > last.get("MA60", float("nan")):
        signals.append("价格在MA20/MA60上方 — 多头趋势")
    elif last["close"] < last.get("MA20", float("nan")) < last.get("MA60", float("nan")):
        signals.append("价格在MA20/MA60下方 — 空头趋势")

    # KDJ
    if prev["K"] < prev["D"] and last["K"] > last["D"] and last["K"] < 30:
        signals.append("KDJ 低位金叉 — 看多信号")
    elif prev["K"] > prev["D"] and last["K"] < last["D"] and last["K"] > 70:
        signals.append("KDJ 高位死叉 — 看空信号")

    return signals


# ── Sina Finance real-time API ────────────────────────────────────────────

def _to_sina_code(code: str) -> str:
    """Convert baostock code to Sina Finance format: 'sh.600519' → 'sh600519'."""
    return code.replace(".", "")


def get_realtime_quotes(codes: list[str]) -> list[dict]:
    """Fetch real-time quotes for multiple stocks in ONE HTTP request (Sina Finance).

    During trading hours returns live price; outside hours returns last close.
    Returns list of dicts with keys:
      code, name, open, high, low, close, prev_close, volume, amount, pctChg, time
    Stocks that fail to parse are silently skipped.
    """
    if not codes:
        return []

    sina_codes = ",".join(_to_sina_code(c) for c in codes)
    url = f"http://hq.sinajs.cn/list={sina_codes}"
    req = urllib.request.Request(
        url,
        headers={"Referer": "http://finance.sina.com.cn"},
    )
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
            volume     = float(fields[8])  if fields[8]  else 0.0  # shares
            amount     = float(fields[9])  if fields[9]  else 0.0  # yuan
            pct_chg    = (current - prev_close) / prev_close * 100 if prev_close else 0.0
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
            "time":       time_str,
        })
    return result


def get_market_snapshot(codes: list[str]) -> list[dict]:
    """Batch real-time quote fetch. Delegates to get_realtime_quotes()."""
    return get_realtime_quotes(codes)
