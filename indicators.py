"""Pure technical indicator computation — no I/O, no side effects.

Extracted from data_engine.py's add_indicators() and generate_signals().
All functions operate on list[dict] in-place.
"""
from __future__ import annotations


# ── Sliding-window utilities ──────────────────────────────────────────────

def _sma(values: list[float], n: int) -> list[float | None]:
    """Simple moving average. First n-1 elements are None."""
    result: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        result[i] = sum(values[i - n + 1:i + 1]) / n
    return result


def _ewm(values: list[float], span: int | None = None,
         alpha: float | None = None, com: int | None = None) -> list[float]:
    """Exponential weighted moving average (adjust=False, matches pandas ewm).

    Seeds y[0] = x[0], then y[i] = (1-a)*y[i-1] + a*x[i].
    """
    if not values:
        return []
    a = _resolve_alpha(span, alpha, com)
    result = [0.0] * len(values)
    result[0] = values[0]
    for i in range(1, len(values)):
        result[i] = (1 - a) * result[i - 1] + a * values[i]
    return result


def _ewm_skip_none(values: list[float | None], span: int | None = None,
                   alpha: float | None = None, com: int | None = None) -> list[float | None]:
    """EWM with None skipping. None positions output None;
    seeds from first non-None value, carries forward across Nones.
    """
    if not values:
        return []
    a = _resolve_alpha(span, alpha, com)
    result: list[float | None] = [None] * len(values)
    prev: float | None = None
    for i, v in enumerate(values):
        if v is None:
            continue
        if prev is None:
            result[i] = v
        else:
            result[i] = (1 - a) * prev + a * v
        prev = result[i]
    return result


def _resolve_alpha(span, alpha, com):
    if span is not None:
        return 2 / (span + 1)
    if alpha is not None:
        return alpha
    if com is not None:
        return 1 / (com + 1)
    raise ValueError("need span, alpha, or com")


def _rolling_std(values: list[float], n: int) -> list[float | None]:
    """Rolling standard deviation (ddof=1). First n-1 are None."""
    result: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1:i + 1]
        m = sum(window) / n
        var = sum((x - m) ** 2 for x in window) / (n - 1)
        result[i] = var ** 0.5
    return result


def _rolling_min(values: list[float], n: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        result[i] = min(values[i - n + 1:i + 1])
    return result


def _rolling_max(values: list[float], n: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        result[i] = max(values[i - n + 1:i + 1])
    return result


# ── Main indicator computation ────────────────────────────────────────────

def add_indicators(rows: list[dict]) -> list[dict]:
    """Add MA, MACD, RSI, Bollinger Bands, KDJ, ATR to list[dict] in-place.

    Requires rows to have: 'close', 'high', 'low'.
    Returns the same list (mutated).
    """
    if not rows or len(rows) < 5:
        return rows

    close = [r["close"] for r in rows]
    high = [r["high"] for r in rows]
    low = [r["low"] for r in rows]
    n = len(rows)

    # ── Moving Averages ──
    for nn in (5, 10, 20, 60):
        ma = _sma(close, nn)
        for i, r in enumerate(rows):
            r[f"MA{nn}"] = ma[i]

    # ── MACD (12, 26, 9) ──
    ema12 = _ewm(close, span=12)
    ema26 = _ewm(close, span=26)
    dif = [ema12[i] - ema26[i] for i in range(n)]
    dea = _ewm(dif, span=9)
    for i, r in enumerate(rows):
        r["DIF"] = dif[i]
        r["DEA"] = dea[i]
        r["MACD"] = (dif[i] - dea[i]) * 2

    # ── RSI (14) — Wilder smoothing ──
    delta: list[float | None] = [None] * n
    for i in range(1, n):
        delta[i] = close[i] - close[i - 1]
    gain = [max(d, 0.0) if d is not None else None for d in delta]
    loss = [max(-d, 0.0) if d is not None else None for d in delta]
    gain_ewm = _ewm_skip_none(gain, alpha=1 / 14)
    loss_ewm = _ewm_skip_none(loss, alpha=1 / 14)
    for i, r in enumerate(rows):
        g, l = gain_ewm[i], loss_ewm[i]
        if g is None or l is None or l == 0:
            r["RSI14"] = None
        else:
            rs = g / l
            r["RSI14"] = 100 - 100 / (1 + rs)

    # ── Bollinger Bands (20, 2) ──
    bb_mid = _sma(close, 20)
    std20 = _rolling_std(close, 20)
    for i, r in enumerate(rows):
        r["BB_MID"] = bb_mid[i]
        if bb_mid[i] is not None and std20[i] is not None:
            r["BB_UP"] = bb_mid[i] + 2 * std20[i]
            r["BB_LO"] = bb_mid[i] - 2 * std20[i]
        else:
            r["BB_UP"] = None
            r["BB_LO"] = None

    # ── KDJ (9, 3, 3) ──
    low9 = _rolling_min(low, 9)
    high9 = _rolling_max(high, 9)
    rsv: list[float | None] = [None] * n
    for i in range(n):
        if high9[i] is not None and low9[i] is not None and high9[i] != low9[i]:
            rsv[i] = (close[i] - low9[i]) / (high9[i] - low9[i]) * 100
    k_vals = _ewm_skip_none(rsv, com=2)
    d_vals = _ewm_skip_none(k_vals, com=2)
    for i, r in enumerate(rows):
        r["K"] = k_vals[i]
        r["D"] = d_vals[i]
        if k_vals[i] is not None and d_vals[i] is not None:
            r["J"] = 3 * k_vals[i] - 2 * d_vals[i]
        else:
            r["J"] = None

    # ── ATR (14) ──
    tr: list[float | None] = [None] * n
    for i in range(n):
        if i == 0:
            tr[i] = high[i] - low[i]
        else:
            prev_close = close[i - 1]
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - prev_close),
                abs(low[i] - prev_close),
            )
    atr14 = _sma(tr, 14)
    for i, r in enumerate(rows):
        r["ATR14"] = atr14[i]

    return rows


# ── Signal generation ─────────────────────────────────────────────────────

def generate_signals(rows: list[dict]) -> list[str]:
    """Generate buy/sell signals from the last 2 rows of indicator data."""
    if not rows or len(rows) < 2:
        return []
    last = rows[-1]
    prev = rows[-2]
    signals: list[str] = []

    # MACD cross
    if prev.get("DIF") is not None and prev.get("DEA") is not None \
            and last.get("DIF") is not None and last.get("DEA") is not None:
        if prev["DIF"] < prev["DEA"] and last["DIF"] > last["DEA"]:
            signals.append("MACD 金叉 — 看多信号")
        elif prev["DIF"] > prev["DEA"] and last["DIF"] < last["DEA"]:
            signals.append("MACD 死叉 — 看空信号")

    # RSI
    rsi = last.get("RSI14")
    if rsi is not None:
        if rsi > 70:
            signals.append(f"RSI={rsi:.1f} 超买区间 — 注意回调风险")
        elif rsi < 30:
            signals.append(f"RSI={rsi:.1f} 超卖区间 — 可能反弹")

    # Bollinger breakout
    bb_up = last.get("BB_UP")
    bb_lo = last.get("BB_LO")
    if bb_up is not None and last["close"] > bb_up:
        signals.append("价格突破布林上轨 — 强势但超买")
    elif bb_lo is not None and last["close"] < bb_lo:
        signals.append("价格跌破布林下轨 — 弱势但超卖")

    # MA trend
    ma20 = last.get("MA20")
    ma60 = last.get("MA60")
    if ma20 is not None and ma60 is not None:
        if last["close"] > ma20 > ma60:
            signals.append("价格在MA20/MA60上方 — 多头趋势")
        elif last["close"] < ma20 < ma60:
            signals.append("价格在MA20/MA60下方 — 空头趋势")

    # KDJ cross
    prev_k, prev_d = prev.get("K"), prev.get("D")
    last_k, last_d = last.get("K"), last.get("D")
    if None not in (prev_k, prev_d, last_k, last_d):
        if prev_k < prev_d and last_k > last_d and last_k < 30:
            signals.append("KDJ 低位金叉 — 看多信号")
        elif prev_k > prev_d and last_k < last_d and last_k > 70:
            signals.append("KDJ 高位死叉 — 看空信号")

    return signals
