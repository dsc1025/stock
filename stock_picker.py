"""Stock picker engine — purely local database, zero external API calls.

All indicator computation is done on-the-fly via indicators.py.
"""
from __future__ import annotations
from typing import Optional

from indicators import add_indicators
from repository import stock_repo


def pick_stocks(pool_ids: list[int] | None, config: dict) -> list[dict]:
    """Core stock picking engine. 100% local database.

    Pipeline:
    1. SQL prefilter (price, turnover, pct_change, amplitude) → narrow candidate set
    2. Batch load K-line history for candidates
    3. Compute indicators on-the-fly, apply complex filters in memory
    4. Return scored & sorted results

    Args:
        pool_ids: optional whitelist of stock_id to filter. None = all stocks.
        config: filter & scoring configuration dict.

    Returns:
        list of result dicts sorted by score descending.
    """
    filters = config.get("filters", {})

    # ── Step 1: SQL prefilter ──
    prefiltered = stock_repo.prefilter_by_latest(filters)
    if prefiltered is not None:
        id_set = set(prefiltered)
        if pool_ids:
            id_set &= set(pool_ids)
        stock_ids = [sid for sid in prefiltered if sid in id_set]
    else:
        stock_ids = pool_ids or [s["id"] for s in stock_repo.get_all_stock_ids()]

    if not stock_ids:
        return []

    # ── Load stock code metadata (id → {code, name}) ──
    all_stocks = stock_repo.get_all_stock_ids()
    stock_map = {s["id"]: s for s in all_stocks}

    # ── Step 2: Batch load history ──
    lookback = config.get("lookback_days", 120)
    need_history_filters = (
        filters.get("prior_rally", {}).get("enabled", False)
        or filters.get("volume_selloff", {}).get("enabled", False)
        or filters.get("consecutive_volume_surge", {}).get("enabled", False)
    )

    if need_history_filters:
        hist_days = max(
            filters.get("prior_rally", {}).get("lookback_days", lookback),
            filters.get("consecutive_volume_surge", {}).get("avg_window", 20)
            + filters.get("consecutive_volume_surge", {}).get("consecutive_days", 5)
            + 10,
            30,
        )
    else:
        hist_days = lookback

    history_map = stock_repo.load_history_batch(stock_ids, hist_days + 5)

    # ── Pre-fetch aggregates ──
    vol_avg_cache = stock_repo.get_avg_volume_batch(stock_ids, 5)
    avg_amp_to_cache = stock_repo.get_avg_amplitude_turnover_batch(stock_ids, lookback)

    # ── Step 3: Compute indicators & apply filters ──
    candidates = []
    _peak_info: dict[int, dict] = {}

    for sid in stock_ids:
        rows = history_map.get(sid)
        if not rows:
            continue

        # Compute indicators on-the-fly
        rows = add_indicators(rows)

        last = rows[-1]
        prev = rows[-2] if len(rows) >= 2 else last

        try:
            price      = float(last.get("close", 0) or 0)
            open_      = float(last.get("open", 0) or price)
            high_      = float(last.get("high", 0) or price)
            low_       = float(last.get("low", 0) or price)
            turnover   = float(last.get("turn", 0) or 0)
            pct_change = float(last.get("pct_chg", 0) or 0)
            # amplitude = (high-low)/preclose*100, computed on the fly
            preclose   = float(last.get("preclose", 0) or 0)
            amplitude  = (high_ - low_) / preclose * 100 if preclose > 0 else 0
            volume     = float(last.get("volume", 0) or 0)

            rsi = float(last.get("RSI14", 50) or 50)
            k   = float(last.get("K", 50) or 50)
            d   = float(last.get("D", 50) or 50)
            dif = float(last.get("DIF", 0) or 0)
            dea = float(last.get("DEA", 0) or 0)
            prev_k   = float(prev.get("K", 50) or 50)
            prev_d   = float(prev.get("D", 50) or 50)
            prev_dif = float(prev.get("DIF", 0) or 0)
            prev_dea = float(prev.get("DEA", 0) or 0)
            ma20  = float(last.get("MA20", price) or price)
            ma60  = float(last.get("MA60", price) or price)
            bb_up = float(last.get("BB_UP", price + 1) or price + 1)
            bb_lo = float(last.get("BB_LO", price - 1) or price - 1)
            atr   = float(last.get("ATR14", 0) or 0)
        except Exception:
            continue

        # ── Apply filters ──
        passed = True
        scores: dict[str, float] = {}

        def _chk(key: str) -> bool:
            return filters.get(key, {}).get("enabled", False)

        # 1. Turnover (SQL prefiltered, defensive check)
        if _chk("turnover"):
            lo = filters["turnover"]["min"]
            hi = filters["turnover"].get("max", 100)
            if not (lo <= turnover <= hi):
                passed = False

        # 2. Amplitude (SQL prefiltered)
        if _chk("amplitude"):
            if amplitude < filters["amplitude"]["min"]:
                passed = False
            if "max" in filters["amplitude"]:
                if amplitude > filters["amplitude"]["max"]:
                    passed = False

        # 3. Pct change (SQL prefiltered)
        if _chk("pct_change"):
            lo = filters["pct_change"]["min"]
            hi = filters["pct_change"]["max"]
            if not (lo <= pct_change <= hi):
                passed = False

        # 4. Price range (SQL prefiltered)
        if _chk("price_range"):
            lo = filters["price_range"]["min"]
            hi = filters["price_range"]["max"]
            if not (lo <= price <= hi):
                passed = False

        # 5. Volume rate
        if _chk("volume_rate"):
            cfg = filters["volume_rate"]
            min_r = cfg.get("min_vs_avg", 1.5)
            max_r = cfg.get("max_vs_avg", 999)
            avg_vol = vol_avg_cache.get(sid, 0)
            if avg_vol > 0:
                cur_r = volume / avg_vol
                if cur_r < min_r or cur_r > max_r:
                    passed = False
                scores["volume"] = min(cur_r, 2.0)

        # 6. RSI range
        if _chk("rsi"):
            if not (filters["rsi"]["min"] <= rsi <= filters["rsi"]["max"]):
                passed = False

        # 7. RSI oversold
        if _chk("rsi_oversold"):
            thr = filters["rsi_oversold"]["threshold"]
            if rsi >= thr:
                passed = False
            scores["rsi_oversold"] = 1.0 - rsi / thr

        # 8. RSI overbought
        if _chk("rsi_overbought"):
            thr = filters["rsi_overbought"]["threshold"]
            if rsi <= thr:
                passed = False
            scores["rsi_overbought"] = rsi / 100 - thr / 100

        # 9. MACD golden cross
        if _chk("macd_golden_cross"):
            is_cross = prev_dif < prev_dea and dif > dea
            if not is_cross:
                passed = False
            scores["macd_golden"] = 2.0 if is_cross else 0

        # 10. MACD death cross
        if _chk("macd_death_cross"):
            is_cross = prev_dif > prev_dea and dif < dea
            if not is_cross:
                passed = False
            scores["macd_death"] = 1.0 if is_cross else 0

        # 11. KDJ range
        if _chk("kdj"):
            if not (filters["kdj"]["min"] <= k <= filters["kdj"]["max"]):
                passed = False

        # 12. KDJ low golden cross
        if _chk("kdj_low_cross"):
            thr = filters["kdj_low_cross"]["threshold"]
            is_cross = prev_k < prev_d and k > d and k < thr
            if not is_cross:
                passed = False
            scores["kdj_cross"] = 1.2 if is_cross else 0

        # 13. Bollinger position
        if _chk("bb_position"):
            pos = filters["bb_position"]["position"]
            dist = (price - bb_lo) / (bb_up - bb_lo) if bb_up > bb_lo else 0.5
            if pos == "lower":
                if dist > 0.3:
                    passed = False
                scores["bb"] = 1.0 - dist
            elif pos == "upper":
                if dist < 0.7:
                    passed = False
                scores["bb"] = dist

        # 14. MA trend
        if _chk("ma_trend"):
            t = filters["ma_trend"]["type"]
            if t == "bullish" and not (price > ma20 > ma60):
                passed = False
            if t == "bearish" and not (price < ma20 < ma60):
                passed = False
            scores["ma_trend"] = 1.0

        # 15. Price vs MA20
        if _chk("price_vs_ma20"):
            rel = filters["price_vs_ma20"]["relation"]
            cfg = filters["price_vs_ma20"]
            pct_lo = cfg.get("pct_min", cfg.get("pct", 0))
            pct_hi = cfg.get("pct_max", 999)
            if rel == "above":
                if price < ma20 * (1 + pct_lo / 100):
                    passed = False
                if price > ma20 * (1 + pct_hi / 100):
                    passed = False
            elif rel == "below":
                if price > ma20 * (1 - pct_lo / 100):
                    passed = False
                if price < ma20 * (1 - pct_hi / 100):
                    passed = False

        # 16. Price vs MA60
        if _chk("price_vs_ma60"):
            rel = filters["price_vs_ma60"]["relation"]
            cfg = filters["price_vs_ma60"]
            pct_lo = cfg.get("pct_min", cfg.get("pct", 0))
            pct_hi = cfg.get("pct_max", 999)
            if rel == "above":
                if price < ma60 * (1 + pct_lo / 100):
                    passed = False
                if price > ma60 * (1 + pct_hi / 100):
                    passed = False
            elif rel == "below":
                if price > ma60 * (1 - pct_lo / 100):
                    passed = False
                if price < ma60 * (1 - pct_hi / 100):
                    passed = False

        # 17. ATR volatility
        if _chk("atr_ratio"):
            ratio = atr / price * 100 if price > 0 else 0
            cfg = filters["atr_ratio"]
            if ratio < cfg.get("min", 0):
                passed = False
            if "max" in cfg and ratio > cfg["max"]:
                passed = False
            scores["atr"] = min(ratio / 2, 1.0)

        # 18. High/low ratio
        if _chk("high_low_ratio"):
            hl = low_ / high_ if high_ > 0 else 0
            if hl < filters["high_low_ratio"]["min"]:
                passed = False
            scores["hl_ratio"] = hl

        # 19. 120-day avg amplitude & turnover
        avg_amp_120 = 0.0
        avg_turn_120 = 0.0
        entry = avg_amp_to_cache.get(sid)
        if entry:
            avg_amp_120 = entry["avg_amplitude"]
            avg_turn_120 = entry["avg_turnover"]

        if _chk("avg_amplitude_120"):
            if avg_amp_120 < filters["avg_amplitude_120"]["min"]:
                passed = False
            scores["avg_amp_120"] = avg_amp_120 / 7.0

        if _chk("avg_turnover_120"):
            if avg_turn_120 < filters["avg_turnover_120"]["min"]:
                passed = False
            scores["avg_turn_120"] = min(avg_turn_120 / 2.0, 2.0)

        # 20. Prior rally
        if _chk("prior_rally"):
            cfg = filters["prior_rally"]
            lb = cfg.get("lookback_days", lookback)
            min_gain = cfg.get("min_gain_pct", 20)
            exclude = cfg.get("exclude_recent_days", 10)
            if rows and len(rows) > exclude + 5:
                n = len(rows)
                search_end = n - exclude
                search_start = max(0, search_end - lb)
                if search_end > search_start:
                    peak_idx = search_start
                    peak_close = float(rows[search_start].get("close", 0) or 0)
                    for i in range(search_start + 1, search_end):
                        c = float(rows[i].get("close", 0) or 0)
                        if c > peak_close:
                            peak_close = c
                            peak_idx = i
                    start_close = float(rows[search_start].get("close", 0) or 0)
                    if start_close > 0:
                        rally_pct = (peak_close / start_close - 1) * 100
                        if rally_pct < min_gain:
                            passed = False
                        else:
                            scores["prior_rally"] = min(rally_pct / min_gain, 2.0)
                    else:
                        passed = False
                else:
                    passed = False
            else:
                passed = False
            if passed:
                _peak_info[sid] = {"idx": peak_idx, "close": peak_close}

        # 21. Volume selloff
        if _chk("volume_selloff"):
            cfg = filters["volume_selloff"]
            min_vr = cfg.get("min_volume_ratio", 1.2)
            min_dd = cfg.get("min_drawdown_pct", 5)
            if rows and len(rows) >= 2:
                n = len(rows)
                pi = _peak_info.get(sid, {})
                peak_idx = pi.get("idx", -1)
                peak_close = pi.get("close", 0.0)
                if peak_idx < 0:
                    peak_idx = 0
                    peak_close = float(rows[0].get("close", 0) or 0)
                    for i in range(1, n):
                        c = float(rows[i].get("close", 0) or 0)
                        if c > peak_close:
                            peak_close = c
                            peak_idx = i
                found_selloff = False
                if peak_idx < n - 1:
                    for i in range(peak_idx + 1, n):
                        day_vol = float(rows[i].get("volume", 0) or 0)
                        day_pct = float(rows[i].get("pct_chg", 0) or 0)
                        local_vols = []
                        for j in range(max(0, i - 5), i):
                            local_vols.append(float(rows[j].get("volume", 0) or 0))
                        local_avg = sum(local_vols) / len(local_vols) if local_vols else 1
                        if local_avg > 0 and day_vol > local_avg * min_vr and day_pct < 0:
                            found_selloff = True
                            break
                dd_pct = (peak_close - price) / peak_close * 100 if peak_close > 0 else 0
                if not found_selloff or dd_pct < min_dd:
                    passed = False
                else:
                    scores["volume_selloff"] = min(dd_pct / min_dd, 2.0)

        if sid in _peak_info:
            del _peak_info[sid]

        # 22. Consecutive volume surge
        if _chk("consecutive_volume_surge"):
            cfg = filters["consecutive_volume_surge"]
            n_days = cfg.get("consecutive_days", 5)
            min_ratio = cfg.get("min_volume_ratio", 1.5)
            avg_win = cfg.get("avg_window", 20)

            search_window = min(lookback + avg_win, len(rows))
            recent = rows[-search_window:] if search_window < len(rows) else rows
            if recent and len(recent) >= avg_win + n_days:
                found_surge = False
                for i in range(avg_win, len(recent) - n_days + 1):
                    pre_vols = [float(recent[j].get("volume", 0) or 0) for j in range(i - avg_win, i)]
                    avg_vol = sum(pre_vols) / avg_win if pre_vols else 0
                    if avg_vol <= 0:
                        continue
                    all_surge = True
                    for j in range(i, i + n_days):
                        day_vol = float(recent[j].get("volume", 0) or 0)
                        if day_vol <= avg_vol * min_ratio:
                            all_surge = False
                            break
                    if all_surge:
                        found_surge = True
                        break
                if not found_surge:
                    passed = False
                else:
                    scores["consecutive_vol"] = min_ratio
            else:
                passed = False

        if not passed:
            continue

        # ── Composite score ──
        score = turnover + amplitude + pct_change
        weights = config.get("signal_weights", {})
        for k_name, val in scores.items():
            score += val * weights.get(k_name, 1.0)

        avg_vol = vol_avg_cache.get(sid, 0)
        vol_ratio = volume / avg_vol if avg_vol > 0 else 0

        # Get name from stock_code table
        stock_info = stock_map.get(sid, {})
        stock_code = stock_info.get("code", str(sid))
        stock_name = stock_info.get("name", stock_code)

        candidates.append({
            "stock_id": sid,
            "code": stock_code,
            "name": stock_name,
            "price": price,
            "pct_change": pct_change,
            "vol_ratio": vol_ratio,
            "turnover": turnover,
            "amplitude": amplitude,
            "volume": volume,
            "rsi": rsi,
            "k": k,
            "macd_positive": dif > dea,
            "price_above_ma20": price > ma20,
            "price_above_ma60": price > ma60,
            "score": score,
            "avg_amplitude_120": avg_amp_120,
            "avg_turnover_120": avg_turn_120,
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates
