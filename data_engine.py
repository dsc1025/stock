"""
Data engine: real-time quotes via Sina Finance API + historical K-line via baostock.
Real-time quotes use a single batch HTTP request (one call for all watchlist stocks).
Historical OHLCV data (for indicator/signal analysis) still uses baostock.
"""
from __future__ import annotations
import baostock as bs
from datetime import datetime, timedelta
import logging
import contextlib
import io
import urllib.request
import re

import db_manager

logging.getLogger("baostock").setLevel(logging.ERROR)

# 内存缓存：同一次运行中避免重复读数据库
# 数据结构：list[dict]，每个 dict 代表一行 K 线（含指标列）
_hist_cache: dict[str, list[dict]] = {}


def _bs_login():
    """静默登录 baostock，抑制 'login success!' 输出。"""
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()


def _bs_force_login() -> bool:
    """强制重连 baostock：先 logout 清理残留状态，再 login。返回是否成功。"""
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            bs.logout()
        except Exception:
            pass
        lg = bs.login()
    return lg.error_code == "0"


def login():
    _bs_login()


def logout():
    bs.logout()


def clear_hist_cache():
    """清空内存缓存（数据库不受影响）"""
    _hist_cache.clear()


def update_hist_cache(data: dict[str, list[dict]]):
    """批量更新内存缓存（避免后续重复查询数据库）。"""
    _hist_cache.update(data)


def get_cached_stock_codes() -> list[str]:
    """返回数据库中已缓存历史数据的股票代码列表。"""
    return db_manager.get_cached_stock_codes()


def get_all_stock_codes() -> list[str]:
    """
    从 baostock 获取全量A股股票代码（没有指数、ETF、债券）。
    包含：沪市主板/科创板(sh.6xxxxx)、深市主板/中小板(sz.0xxxxx)、创业板(sz.3xxxxx)
    自动处理节假日：往前最多回溯7天查找有效交易日。
    连接异常时自动重试最多 3 次。
    """
    import time as _time
    for attempt in range(3):
        if not _bs_force_login():
            if attempt < 2:
                _time.sleep(2)
                continue
            return []
        for delta in range(7):
            day = (datetime.today() - timedelta(days=delta)).strftime("%Y-%m-%d")
            try:
                rs = bs.query_all_stock(day=day)
            except Exception:
                break  # 连接异常，退到外层重试
            codes = []
            while rs.error_code == "0" and rs.next():
                code = rs.get_row_data()[0]
                if code.startswith("sh.6") or code.startswith("sz.0") or code.startswith("sz.3"):
                    codes.append(code)
            if codes:
                return codes
        # 7 天都没数据，重试
        if attempt < 2:
            _time.sleep(2)
    return []


def _fetch_history_from_api(code: str, days: int = 120) -> list[dict]:
    """直接调用 baostock API 获取历史K线，不经过任何缓存。返回 list[dict]。"""
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
    rs = bs.query_history_k_data_plus(
        code,
        "date,open,high,low,close,preclose,volume,amount,turn,pctChg",
        start_date=start,
        end_date=end,
        frequency="d",
        adjustflag="3",
    )
    rows: list[dict] = []
    while rs.error_code == "0" and rs.next():
        r = rs.get_row_data()
        try:
            preclose = float(r[5]) if r[5] else None
            high_v   = float(r[2]) if r[2] else None
            low_v    = float(r[3]) if r[3] else None
            # 振幅 = (最高-最低)/前收×100
            amplitude = ((high_v - low_v) / preclose * 100) if (preclose and high_v is not None and low_v is not None) else None
            row = {
                "date": r[0],
                "open": float(r[1]) if r[1] else None,
                "high": high_v,
                "low": low_v,
                "close": float(r[4]) if r[4] else None,
                "preclose": preclose,
                "volume": float(r[6]) if r[6] else 0.0,
                "amount": float(r[7]) if r[7] else 0.0,
                "turn": float(r[8]) if r[8] else 0.0,
                "pctChg": float(r[9]) if r[9] else 0.0,
                "amplitude": amplitude,
            }
        except (ValueError, IndexError):
            continue
        if row["close"] is not None:
            rows.append(row)
    return rows[-days:] if len(rows) > days else rows


def get_stock_history(code: str, days: int = 120) -> list[dict]:
    """
    获取历史K线数据。优先级：内存缓存 → 数据库 → baostock API。
    """
    # 1. 内存缓存
    if code in _hist_cache:
        return _hist_cache[code]

    # 2. 数据库
    db_rows = db_manager.load_stock_history(code)
    if db_rows:
        _hist_cache[code] = db_rows
        return db_rows

    # 3. 从 API 获取并双写缓存
    rows = _fetch_history_from_api(code, days)
    if rows:
        db_manager.save_stock_history(code, rows)
        _hist_cache[code] = rows
    return rows


def refresh_hist_cache(codes: list[str], on_progress=None) -> tuple[int, int]:
    """
    批量下载并保存股票历史K线到数据库。

    Args:
        codes: 需要缓存的股票代码列表
        on_progress: 进度回调 (current_code: str, done: int, total: int)

    Returns:
        (成功数量, 失败数量)
    """
    import time as _time
    _bs_force_login()
    success, errors = 0, 0
    total = len(codes)
    for i, code in enumerate(codes, 1):
        # 每100只强制重连 + 休息，避免连接断开
        if i % 100 == 0:
            _time.sleep(2)
            _bs_force_login()

        for attempt in range(3):
            try:
                rows = _fetch_history_from_api(code, days=120)
                if rows:
                    db_manager.save_stock_history(code, rows)
                    _hist_cache[code] = rows
                    success += 1
                else:
                    errors += 1
                break  # success, exit retry loop
            except Exception:
                if attempt < 2:
                    _time.sleep(3)
                    _bs_force_login()
                else:
                    errors += 1

        if on_progress:
            on_progress(code, i, total)
    return success, errors


def get_stock_basic(code: str) -> dict:
    """Get basic stock info."""
    rs = bs.query_stock_basic(code=code)
    if rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        return dict(zip(rs.fields, row))
    return {}


# ── 纯 Python 滑动窗口工具函数（替代 pandas 向量化） ─────────────────────

def _sma(values: list[float], n: int) -> list[float | None]:
    """简单移动平均。前 n-1 个为 None。"""
    result: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        result[i] = sum(values[i - n + 1:i + 1]) / n
    return result


def _ewm(values: list[float], span: int | None = None,
         alpha: float | None = None, com: int | None = None) -> list[float]:
    """指数加权平均（adjust=False，与 pandas ewm 一致）。
    无 None 值时使用；种子 y[0]=x[0]，之后 y[i]=(1-a)*y[i-1]+a*x[i]。"""
    if not values:
        return []
    if span is not None:
        a = 2 / (span + 1)
    elif alpha is not None:
        a = alpha
    elif com is not None:
        a = 1 / (com + 1)
    else:
        raise ValueError("need span/alpha/com")
    result = [0.0] * len(values)
    result[0] = values[0]
    for i in range(1, len(values)):
        result[i] = (1 - a) * result[i - 1] + a * values[i]
    return result


def _ewm_skip_none(values: list[float | None], span: int | None = None,
                   alpha: float | None = None, com: int | None = None) -> list[float | None]:
    """带 None 跳过的 EWM（adjust=False）。None 位置输出 None，
    从首个非 None 值播种，后续 None 不影响累积（carry forward prev）。"""
    if not values:
        return []
    if span is not None:
        a = 2 / (span + 1)
    elif alpha is not None:
        a = alpha
    elif com is not None:
        a = 1 / (com + 1)
    else:
        raise ValueError("need span/alpha/com")
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


def _rolling_std(values: list[float], n: int) -> list[float | None]:
    """滚动标准差（ddof=1，与 pandas 一致）。前 n-1 个为 None。"""
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


def add_indicators(df: list[dict]) -> list[dict]:
    """Add MA, EMA, MACD, RSI, Bollinger Bands, KDJ, ATR to list of row dicts (in-place)."""
    if not df or len(df) < 5:
        return df

    close = [r["close"] for r in df]
    high = [r["high"] for r in df]
    low = [r["low"] for r in df]
    n = len(df)

    # Moving Averages
    ma_maps = {nn: _sma(close, nn) for nn in (5, 10, 20, 60)}
    for i, r in enumerate(df):
        for nn in (5, 10, 20, 60):
            r[f"MA{nn}"] = ma_maps[nn][i]

    # EMA / MACD
    ema12 = _ewm(close, span=12)
    ema26 = _ewm(close, span=26)
    dif = [ema12[i] - ema26[i] for i in range(n)]
    dea = _ewm(dif, span=9)
    for i, r in enumerate(df):
        r["EMA12"] = ema12[i]
        r["EMA26"] = ema26[i]
        r["DIF"] = dif[i]
        r["DEA"] = dea[i]
        r["MACD"] = (dif[i] - dea[i]) * 2

    # RSI (14) — Wilder 平滑（与通达信口径一致）
    # delta[0] = None（无前值），gain/loss[0] = None
    delta: list[float | None] = [None] * n
    for i in range(1, n):
        delta[i] = close[i] - close[i - 1]
    gain = [max(d, 0.0) if d is not None else None for d in delta]
    loss = [max(-d, 0.0) if d is not None else None for d in delta]
    gain_ewm = _ewm_skip_none(gain, alpha=1 / 14)
    loss_ewm = _ewm_skip_none(loss, alpha=1 / 14)
    for i, r in enumerate(df):
        g, l = gain_ewm[i], loss_ewm[i]
        if g is None or l is None or l == 0:
            r["RSI14"] = None  # 与原 pandas: loss.replace(0, nan) 行为一致
        else:
            rs = g / l
            r["RSI14"] = 100 - 100 / (1 + rs)

    # Bollinger Bands (20, 2)
    bb_mid = _sma(close, 20)
    std20 = _rolling_std(close, 20)
    for i, r in enumerate(df):
        r["BB_MID"] = bb_mid[i]
        if bb_mid[i] is not None and std20[i] is not None:
            r["BB_UP"] = bb_mid[i] + 2 * std20[i]
            r["BB_LO"] = bb_mid[i] - 2 * std20[i]
        else:
            r["BB_UP"] = None
            r["BB_LO"] = None

    # KDJ (9, 3, 3)
    low9 = _rolling_min(low, 9)
    high9 = _rolling_max(high, 9)
    rsv: list[float | None] = [None] * n
    for i in range(n):
        if high9[i] is not None and low9[i] is not None and high9[i] != low9[i]:
            rsv[i] = (close[i] - low9[i]) / (high9[i] - low9[i]) * 100
    k_vals = _ewm_skip_none(rsv, com=2)
    d_vals = _ewm_skip_none(k_vals, com=2)
    for i, r in enumerate(df):
        r["K"] = k_vals[i]
        r["D"] = d_vals[i]
        if k_vals[i] is not None and d_vals[i] is not None:
            r["J"] = 3 * k_vals[i] - 2 * d_vals[i]
        else:
            r["J"] = None

    # ATR (14)
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
    atr14 = _sma(tr, 14)  # rolling mean of TR
    for i, r in enumerate(df):
        r["ATR14"] = atr14[i]

    return df


def generate_signals(df: list[dict]) -> list[str]:
    """Generate simple buy/sell signals from indicators."""
    if not df or len(df) < 2:
        return []
    last = df[-1]
    prev = df[-2]
    signals = []

    # MACD golden/death cross
    if prev["DIF"] < prev["DEA"] and last["DIF"] > last["DEA"]:
        signals.append("MACD 金叉 — 看多信号")
    elif prev["DIF"] > prev["DEA"] and last["DIF"] < last["DEA"]:
        signals.append("MACD 死叉 — 看空信号")

    # RSI overbought/oversold
    rsi = last.get("RSI14")
    if rsi is not None:
        if rsi > 70:
            signals.append(f"RSI={rsi:.1f} 超买区间 — 注意回调风险")
        elif rsi < 30:
            signals.append(f"RSI={rsi:.1f} 超卖区间 — 可能反弹")

    # Bollinger breakout
    if last.get("BB_UP") is not None and last["close"] > last["BB_UP"]:
        signals.append("价格突破布林上轨 — 强势但超买")
    elif last.get("BB_LO") is not None and last["close"] < last["BB_LO"]:
        signals.append("价格跌破布林下轨 — 弱势但超卖")

    # MA trend
    ma20 = last.get("MA20")
    ma60 = last.get("MA60")
    if ma20 is not None and ma60 is not None:
        if last["close"] > ma20 > ma60:
            signals.append("价格在MA20/MA60上方 — 多头趋势")
        elif last["close"] < ma20 < ma60:
            signals.append("价格在MA20/MA60下方 — 空头趋势")

    # KDJ
    if (prev.get("K") is not None and prev.get("D") is not None
            and last.get("K") is not None and last.get("D") is not None):
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
      code, name, open, high, low, close, prev_close, volume, amount,
      pctChg, amplitude, time
    amplitude = (high - low) / prev_close * 100 — 日内振幅，T+0 核心参数
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


def get_market_snapshot(codes: list[str]) -> list[dict]:
    """Batch real-time quote fetch. Delegates to get_realtime_quotes()."""
    return get_realtime_quotes(codes)
