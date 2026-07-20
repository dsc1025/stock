"""
Stock quantitative analysis terminal.
Uses baostock for historical/end-of-day data; refreshes on demand.

Controls:
  [f] stock picker      [c] cache manager
  [q] quit
"""
from __future__ import annotations
from typing import Optional, List, Dict

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.prompt import Prompt, Confirm
from rich import box
from rich.progress import (
    Progress, SpinnerColumn, BarColumn,
    TextColumn, TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn,
)

from data_engine import (
    login, logout,
    get_realtime_quotes,
    get_all_stock_codes, get_cached_stock_codes, refresh_hist_cache,
    refresh_hist_incremental,
)
import db_manager

console = Console()


# ─────────────────────────────────────────────────────────────────────────
# 核心选股函数
# ─────────────────────────────────────────────────────────────────────────

def pick_stocks(pool: list[str] | None, config: dict) -> list[dict]:
    """
    核心选股引擎 - 使用数据库批量加载 + SQL预筛选，性能大幅优化。

    处理流程：
    1. SQL预筛选：将价格/换手率/涨跌幅/振幅等简单条件推入SQL，快速缩小候选范围
    2. 批量加载：一次SQL查询拉取所有候选股的历史K线
    3. 内存计算：对候选股逐只计算技术指标并应用复杂筛选条件
    4. 仅对通过筛选的少量候选股批量拉取实时行情（取名称+实时价）
    """
    # ── Step 1: SQL 预筛选（价格、换手率、涨跌幅、振幅） ──
    prefiltered = db_manager.prefilter_codes_by_latest(config["filters"])

    if prefiltered is not None:
        # SQL 预筛选生效
        code_set = set(prefiltered)
        if pool:
            code_set &= set(pool)
        codes = [c for c in prefiltered if c in code_set]  # preserve order
    else:
        # 没有启用简单筛选条件，使用全部
        codes = pool if pool else get_cached_stock_codes()

    if not codes:
        return []

    # ── Step 2: 批量加载预计算指标（一次SQL，取最后2行，无pandas） ──
    ind_map = db_manager.load_latest_indicators_batch(codes)

    # ── Step 2.5: Pre-fetch avg volume (always, for 量比 display + volume_rate filter) ──
    _vol_avg_cache: dict[str, float] = db_manager.get_avg_volume_batch(codes, 5)

    # ── Step 2.6: Pre-fetch rolling avg amplitude & avg turnover ──
    _avg_amp_to_cache: dict[str, dict] = {}
    lookback = config.get("lookback_days", 120)
    _avg_amp_to_cache = db_manager.get_avg_amplitude_turnover_batch(codes, lookback)
    need_avg_120 = True

    # ── Step 2.7: Pre-fetch multi-day history (for prior_rally / volume_selloff filters) ──
    _history_cache: dict[str, list[dict]] = {}
    _need_history = (
        config["filters"].get("prior_rally", {}).get("enabled", False)
        or config["filters"].get("volume_selloff", {}).get("enabled", False)
        or config["filters"].get("consecutive_volume_surge", {}).get("enabled", False)
    )
    if _need_history:
        hist_days = max(
            config["filters"].get("prior_rally", {}).get("lookback_days",
                config.get("lookback_days", 60)),
            config["filters"].get("consecutive_volume_surge", {}).get("avg_window", 20)
                + config["filters"].get("consecutive_volume_surge", {}).get("consecutive_days", 5)
                + 10,
            30,
        )
        _history_cache = db_manager.load_stock_history_batch(codes, hist_days + 5)

    # ── Step 3: 直接使用预计算指标应用筛选 ──
    candidates = []
    _peak_info: dict[str, dict] = {}  # shared between prior_rally & volume_selloff

    for code, rows in ind_map.items():
        last = rows["last"]
        prev = rows["prev"]
        if last is None:
            continue
        # If only 1 row exists, prev = last (no historical comparison)
        if prev is None:
            prev = last

        try:
            price      = float(last["close"] or 0)
            open_      = float(last["open"] or price)
            high_      = float(last["high"] or price)
            low_       = float(last["low"] or price)
            turnover   = float(last["turn"] or 0)
            pct_change = float(last["pctChg"] or 0)
            amplitude  = float(last.get("amplitude") or 0)
            volume     = float(last["volume"] or 0)

            rsi = float(last["RSI14"] or 50)
            k   = float(last["K"] or 50)
            d   = float(last["D"] or 50)
            dif = float(last["DIF"] or 0)
            dea = float(last["DEA"] or 0)
            prev_k   = float(prev["K"] or 50)
            prev_d   = float(prev["D"] or 50)
            prev_dif = float(prev["DIF"] or 0)
            prev_dea = float(prev["DEA"] or 0)
            ma20  = float(last["MA20"] or price)
            ma60  = float(last["MA60"] or price)
            bb_up = float(last["BB_UP"] or price + 1)
            bb_lo = float(last["BB_LO"] or price - 1)
            atr   = float(last["ATR14"] or 0)
        except Exception:
            continue

        # ══ 执行筛选条件 ══
        passed = True
        scores: dict[str, float] = {}

        def _chk(key: str) -> bool:
            return config["filters"].get(key, {}).get("enabled", False)

        # 1 换手率 — SQL 已预筛选，但仍需 double-check（防御性）
        if _chk("turnover"):
            lo = config["filters"]["turnover"]["min"]
            hi = config["filters"]["turnover"].get("max", 100)
            if not (lo <= turnover <= hi):
                passed = False
        # 2 振幅 — SQL 已预筛选
        if _chk("amplitude"):
            if amplitude < config["filters"]["amplitude"]["min"]:
                passed = False
            if "max" in config["filters"]["amplitude"]:
                if amplitude > config["filters"]["amplitude"]["max"]:
                    passed = False
        # 3 涨跌幅 — SQL 已预筛选
        if _chk("pct_change"):
            lo = config["filters"]["pct_change"]["min"]
            hi = config["filters"]["pct_change"]["max"]
            if not (lo <= pct_change <= hi):
                passed = False
        # 4 股价范围 — SQL 已预筛选
        if _chk("price_range"):
            lo = config["filters"]["price_range"]["min"]
            hi = config["filters"]["price_range"]["max"]
            if not (lo <= price <= hi):
                passed = False
        # 5 成交量突增/缩量 — 预取批量volume数据
        if _chk("volume_rate"):
            days_n = config["filters"]["volume_rate"].get("days", 5)
            min_r = config["filters"]["volume_rate"].get("min_vs_avg", 1.5)
            max_r = config["filters"]["volume_rate"].get("max_vs_avg", 999)
            # Use pre-fetched avg volume dict
            avg_vol = _vol_avg_cache.get(code, 0)
            if avg_vol > 0:
                cur_r = volume / avg_vol
                if cur_r < min_r:
                    passed = False
                if cur_r > max_r:
                    passed = False
                scores["volume"] = min(cur_r, 2.0)
        # 6 RSI 范围
        if _chk("rsi"):
            if not (config["filters"]["rsi"]["min"] <= rsi <= config["filters"]["rsi"]["max"]):
                passed = False
        # 7 RSI 超卖
        if _chk("rsi_oversold"):
            thr = config["filters"]["rsi_oversold"]["threshold"]
            if rsi >= thr:
                passed = False
            scores["rsi_oversold"] = 1.0 - rsi / thr
        # 8 RSI 超买
        if _chk("rsi_overbought"):
            thr = config["filters"]["rsi_overbought"]["threshold"]
            if rsi <= thr:
                passed = False
            scores["rsi_overbought"] = rsi / 100 - thr / 100
        # 9 MACD 金叉
        if _chk("macd_golden_cross"):
            is_cross = prev_dif < prev_dea and dif > dea
            if not is_cross:
                passed = False
            scores["macd_golden"] = 2.0 if is_cross else 0
        # 10 MACD 死叉
        if _chk("macd_death_cross"):
            is_cross = prev_dif > prev_dea and dif < dea
            if not is_cross:
                passed = False
            scores["macd_death"] = 1.0 if is_cross else 0
        # 11 KDJ 范围
        if _chk("kdj"):
            if not (config["filters"]["kdj"]["min"] <= k <= config["filters"]["kdj"]["max"]):
                passed = False
        # 12 KDJ 低位金叉
        if _chk("kdj_low_cross"):
            thr = config["filters"]["kdj_low_cross"]["threshold"]
            is_cross = prev_k < prev_d and k > d and k < thr
            if not is_cross:
                passed = False
            scores["kdj_cross"] = 1.2 if is_cross else 0
        # 13 布林带位置
        if _chk("bb_position"):
            pos = config["filters"]["bb_position"]["position"]
            dist = (price - bb_lo) / (bb_up - bb_lo) if bb_up > bb_lo else 0.5
            if pos == "lower":
                if dist > 0.3:
                    passed = False
                scores["bb"] = 1.0 - dist
            elif pos == "upper":
                if dist < 0.7:
                    passed = False
                scores["bb"] = dist
        # 14 均线趋势
        if _chk("ma_trend"):
            t = config["filters"]["ma_trend"]["type"]
            if t == "bullish" and not (price > ma20 > ma60):
                passed = False
            if t == "bearish" and not (price < ma20 < ma60):
                passed = False
            scores["ma_trend"] = 1.0
        # 15 价格 vs MA20
        if _chk("price_vs_ma20"):
            rel = config["filters"]["price_vs_ma20"]["relation"]
            cfg = config["filters"]["price_vs_ma20"]
            # 兼容旧格式 "pct" 和新格式 "pct_min"/"pct_max"
            pct_lo = cfg.get("pct_min", cfg.get("pct", 0))
            pct_hi = cfg.get("pct_max", 999)
            if rel == "above":
                if price < ma20 * (1 + pct_lo / 100):
                    passed = False
                if price > ma20 * (1 + pct_hi / 100):
                    passed = False
            if rel == "below":
                if price > ma20 * (1 - pct_lo / 100):
                    passed = False
                if price < ma20 * (1 - pct_hi / 100):
                    passed = False
        # 16 价格 vs MA60
        if _chk("price_vs_ma60"):
            rel = config["filters"]["price_vs_ma60"]["relation"]
            cfg = config["filters"]["price_vs_ma60"]
            pct_lo = cfg.get("pct_min", cfg.get("pct", 0))
            pct_hi = cfg.get("pct_max", 999)
            if rel == "above":
                if price < ma60 * (1 + pct_lo / 100):
                    passed = False
                if price > ma60 * (1 + pct_hi / 100):
                    passed = False
            if rel == "below":
                if price > ma60 * (1 - pct_lo / 100):
                    passed = False
                if price < ma60 * (1 - pct_hi / 100):
                    passed = False
        # 17 ATR 波动性
        if _chk("atr_ratio"):
            ratio = atr / price * 100 if price > 0 else 0
            cfg = config["filters"]["atr_ratio"]
            if ratio < cfg.get("min", 0):
                passed = False
            if "max" in cfg and ratio > cfg["max"]:
                passed = False
            scores["atr"] = min(ratio / 2, 1.0)
        # 18 最低价/最高价比值
        if _chk("high_low_ratio"):
            hl = low_ / high_ if high_ > 0 else 0
            if hl < config["filters"]["high_low_ratio"]["min"]:
                passed = False
            scores["hl_ratio"] = hl
        # 19 120日均振幅
        avg_amp_120 = 0.0
        avg_turn_120 = 0.0
        if need_avg_120:
            entry = _avg_amp_to_cache.get(code)
            if entry:
                avg_amp_120 = entry["avg_amplitude"]
                avg_turn_120 = entry["avg_turnover"]
        if _chk("avg_amplitude_120"):
            if avg_amp_120 < config["filters"]["avg_amplitude_120"]["min"]:
                passed = False
            scores["avg_amp_120"] = avg_amp_120 / 7.0  # normalize to ~1.0 at 7%
        # 20 120日均换手率
        if _chk("avg_turnover_120"):
            if avg_turn_120 < config["filters"]["avg_turnover_120"]["min"]:
                passed = False
            scores["avg_turn_120"] = min(avg_turn_120 / 2.0, 2.0)
        # 21 前期大涨检测 — 回看N日内是否有显著涨幅
        if _chk("prior_rally"):
            cfg = config["filters"]["prior_rally"]
            lb = cfg.get("lookback_days", config.get("lookback_days", 60))
            min_gain = cfg.get("min_gain_pct", 20)
            exclude = cfg.get("exclude_recent_days", 10)
            hist = _history_cache.get(code, [])
            if hist and len(hist) > exclude + 5:
                n = len(hist)
                search_end = n - exclude  # exclude recent days from peak search
                search_start = max(0, search_end - lb)
                if search_end > search_start:
                    # find highest close in [search_start, search_end)
                    peak_idx = search_start
                    peak_close = float(hist[search_start].get("close", 0) or 0)
                    for i in range(search_start + 1, search_end):
                        c = float(hist[i].get("close", 0) or 0)
                        if c > peak_close:
                            peak_close = c
                            peak_idx = i
                    start_close = float(hist[search_start].get("close", 0) or 0)
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
                passed = False  # insufficient history
            # store peak info for volume_selloff to reuse
            if passed and hist:
                _peak_info[code] = {"idx": peak_idx, "close": peak_close}
        # 22 放量下跌检测 — 峰值后出现放量阴线, 且回撤达标
        if _chk("volume_selloff"):
            cfg = config["filters"]["volume_selloff"]
            min_vr = cfg.get("min_volume_ratio", 1.2)
            min_dd = cfg.get("min_drawdown_pct", 5)
            hist = _history_cache.get(code, [])
            if hist and len(hist) >= 2:
                n = len(hist)
                # find peak position (reuse from prior_rally if available, or scan all)
                peak_idx = _peak_info.get(code, {}).get("idx", -1)
                peak_close = _peak_info.get(code, {}).get("close", 0.0)
                if peak_idx < 0:
                    # scan all history for peak
                    peak_idx = 0
                    peak_close = float(hist[0].get("close", 0) or 0)
                    for i in range(1, n):
                        c = float(hist[i].get("close", 0) or 0)
                        if c > peak_close:
                            peak_close = c
                            peak_idx = i
                # check for volume sell-off after peak
                found_selloff = False
                if peak_idx < n - 1:
                    for i in range(peak_idx + 1, n):
                        day_vol = float(hist[i].get("volume", 0) or 0)
                        day_pct = float(hist[i].get("pctChg", 0) or 0)
                        # compute local avg volume (5-day before this day)
                        local_vols = []
                        for j in range(max(0, i - 5), i):
                            local_vols.append(float(hist[j].get("volume", 0) or 0))
                        local_avg = sum(local_vols) / len(local_vols) if local_vols else 1
                        if local_avg > 0 and day_vol > local_avg * min_vr and day_pct < 0:
                            found_selloff = True
                            break
                # check drawdown from peak
                dd_pct = (peak_close - price) / peak_close * 100 if peak_close > 0 else 0
                if not found_selloff or dd_pct < min_dd:
                    passed = False
                else:
                    scores["volume_selloff"] = min(dd_pct / min_dd, 2.0)
            # if no history, skip (don't fail — insufficient data)
        if code in _peak_info:
            del _peak_info[code]
        # 23 连续放量检测 — 在回溯期内是否存在连续N日放量
        if _chk("consecutive_volume_surge"):
            cfg = config["filters"]["consecutive_volume_surge"]
            n_days = cfg.get("consecutive_days", 5)
            min_ratio = cfg.get("min_volume_ratio", 1.5)
            avg_win = cfg.get("avg_window", 20)
            lookback_days = config.get("lookback_days", 60)

            hist = _history_cache.get(code, [])
            # only examine the most recent `lookback_days` (plus avg_win for baseline)
            search_window = min(lookback_days + avg_win, len(hist))
            recent = hist[-search_window:] if search_window < len(hist) else hist
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
                passed = False  # insufficient history

        if not passed:
            continue

        # 计算综合评分
        score = turnover + amplitude + pct_change
        weights = config.get("signal_weights", {})
        for k_name, val in scores.items():
            score += val * weights.get(k_name, 1.0)

        # 量比 = 当日成交量 / 5日均量
        avg_vol = _vol_avg_cache.get(code, 0)
        vol_ratio = volume / avg_vol if avg_vol > 0 else 0

        candidates.append({
            "code": code,
            "name": code,
            "price": price,
            "pct_change": pct_change,
            "vol_ratio": vol_ratio,
            "turnover": turnover,
            "amplitude": amplitude,
            "volume": float(last.get("volume", 0) or 0),
            "rsi": rsi,
            "k": k,
            "macd_positive": dif > dea,
            "price_above_ma20": price > ma20,
            "price_above_ma60": price > ma60,
            "score": score,
            "avg_amplitude_120": avg_amp_120,
            "avg_turnover_120": avg_turn_120,
        })

    # 仅对通过筛选的少量候选股，批量获取实时行情（取名称+实时价）
    if candidates:
        rt_map = {q["code"]: q for q in get_realtime_quotes([c["code"] for c in candidates])}
        for c in candidates:
            rt = rt_map.get(c["code"])
            if rt:
                c["name"] = rt.get("name", c["code"])
                if rt.get("close", 0) > 0:
                    c["price"] = rt["close"]
                    c["pct_change"] = rt.get("pctChg", c["pct_change"])
                if rt.get("amplitude", 0) > 0:
                    c["amplitude"] = rt["amplitude"]

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


# ─────────────────────────────────────────────────────────────────────────
# 选股结果展示
# ─────────────────────────────────────────────────────────────────────────

def make_picker_table(candidates: list[dict]) -> Table:
    """将选股结果显示为富文本表格"""
    t = Table(
        title="[bold cyan]选股结果[/]",
        box=box.SIMPLE_HEAVY,
        header_style="bold magenta",
        show_lines=False,
    )
    
    t.add_column("代码", style="cyan", width=8)
    t.add_column("名称", width=14)
    t.add_column("价格", justify="right", width=8)
    t.add_column("涨幅", justify="right", width=7)
    t.add_column("成交量", justify="right", width=10)
    t.add_column("换手", justify="right", width=7)
    t.add_column("振幅", justify="right", width=7)
    t.add_column("RSI", justify="right", width=5)
    t.add_column("K值", justify="right", width=5)
    t.add_column("MACD", justify="center", width=5)
    t.add_column("MA20", justify="center", width=5)
    t.add_column("MA60", justify="center", width=5)
    t.add_column("均振幅", justify="right", width=10)
    t.add_column("均换手", justify="right", width=10)
    t.add_column("评分", justify="right", width=7)
    
    for c in candidates:
        macd_mark = "✓" if c["macd_positive"] else "✗"
        ma20_mark = "↑" if c["price_above_ma20"] else "↓"
        ma60_mark = "↑" if c["price_above_ma60"] else "↓"
        
        t.add_row(
            _strip_prefix(c["code"]),
            c["name"],
            f"¥{c['price']:.2f}",
            Text.from_markup(color_pct(c["pct_change"])),
            f"{int(c.get('volume', 0) / 100):,}",
            f"{c['turnover']:.1f}%",
            f"{c['amplitude']:.1f}%",
            f"{c['rsi']:.0f}",
            f"{c['k']:.0f}",
            macd_mark,
            ma20_mark,
            ma60_mark,
            f"{c.get('avg_amplitude_120', 0):.1f}%",
            f"{c.get('avg_turnover_120', 0):.1f}%",
            f"{c['score']:.1f}",
        )
    
    return t


def menu_stock_picker():
    """选股工具 — 放量后缩量回调"""
    console.print("\n[bold cyan]=== 选股工具: 放量后缩量回调 ===[/]")

    cached_count = len(get_cached_stock_codes())
    if cached_count == 0:
        console.print("[red]本地无缓存数据！请先按 [bold]c[/bold] 进入缓存管理下载历史数据。[/]")
        Prompt.ask("\n按 Enter 返回")
        return

    console.print(f"[dim]股票池: 全部缓存A股 ({cached_count} 只)[/]")
    console.print("[dim]策略: 阶段放量(连续5日>1.5×20日均量) → 当前缩量(≤5日均量)[/]")

    lookback = Prompt.ask(
        "\n[dim]回溯交易日数[/]",
        default="60",
        show_default=True,
    )
    try:
        lookback = int(lookback)
        lookback = max(lookback, 10)
    except ValueError:
        lookback = 60

    # 构建核心筛选配置
    config = {
        "filters": {
            "turnover": {"min": 0.5, "max": 30.0, "enabled": True},
            "amplitude": {"min": 0.5, "max": 15.0, "enabled": True},
            "price_range": {"min": 5, "max": 500, "enabled": True},
            "volume_rate": {
                "enabled": True,
                "min_vs_avg": 0.1,
                "max_vs_avg": 1.0,
                "days": 5,
            },
            "consecutive_volume_surge": {
                "enabled": True,
                "consecutive_days": 5,
                "min_volume_ratio": 1.5,
                "avg_window": 20,
            },
        },
        "signal_weights": {
            "consecutive_vol": 2.0,
            "volume": 0.5,
        },
        "lookback_days": lookback,
    }

    console.print(f"\n[dim]正在筛选 {cached_count} 只股票 ({lookback}日回溯)...[/]")
    candidates = pick_stocks(None, config)

    if not candidates:
        console.print("[yellow]未找到符合条件的股票，可以尝试增加回溯天数[/]")
    else:
        console.print(make_picker_table(candidates))
        console.print(f"\n[green]共找到 {len(candidates)} 只符合条件的股票[/]")

    Prompt.ask("\n按 Enter 返回")


def menu_cache_manager():
    """缓存管理菜单 - 下载/刷新所有A股历史数据到数据库"""
    console.print("\n[bold cyan]=== 股票数据缓存管理 ===[/]")

    cached = get_cached_stock_codes()
    console.print(f"[dim]数据库已缓存: [bold]{len(cached)}[/] 只股票[/]")

    console.print("\n  [1] 补全缺失 (只下载还没有缓存的股票)")
    console.print("  [2] 增量更新 (仅更新昨日至今的新数据)")
    console.print("  [3] 刷新全部 (重新下载所有A股数据)")
    console.print("  [q] 返回")

    choice = Prompt.ask("\n选择", choices=["1", "2", "3", "q"], default="q", show_choices=False)
    if choice == "q":
        return

    console.print("\n[dim]正在获取A股股票列表...[/]")
    all_codes = get_all_stock_codes()
    if not all_codes:
        console.print("[red]获取股票列表失败，请检查网络连接[/]")
        Prompt.ask("\n按 Enter 返回")
        return

    console.print(f"[dim]共获取 {len(all_codes)} 只 A股[/]")

    if choice == "1":
        cached_set = set(cached)
        to_fetch = [c for c in all_codes if c not in cached_set]
        action = f"补全缺失 ({len(to_fetch)} 只)"
    elif choice == "2":
        # 增量更新：只更新已缓存的股票
        to_fetch = [c for c in all_codes if c in set(cached)]
        action = f"增量更新 ({len(to_fetch)} 只已缓存股票)"
    else:
        to_fetch = all_codes
        action = f"刷新全部 ({len(to_fetch)} 只)"

    if not to_fetch:
        console.print("[green]无需操作[/]")
        Prompt.ask("\n按 Enter 返回")
        return

    # 增量更新的提示信息
    if choice == "2":
        console.print(f"\n[cyan]{action}[/]")
        console.print("[dim]增量更新仅拉取每只股票上次缓存日期之后的新数据，速度快、资源省[/]")
    else:
        console.print(f"\n[yellow]{action}，每只约0.5秒，请耐心等待...[/]")

    if not Confirm.ask("确认开始?"):
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description:<12}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("[dim]{task.completed}/{task.total}[/]"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("准备中...", total=len(to_fetch))

        def on_prog(code: str, done: int, total: int):
            progress.update(task, completed=done, description=f"[cyan]{_strip_prefix(code)}[/]")

        if choice == "2":
            success, errors = refresh_hist_incremental(to_fetch, on_progress=on_prog)
        else:
            success, errors = refresh_hist_cache(to_fetch, days=730, on_progress=on_prog)

    console.print(f"\n[green]完成！成功 {success} 只，失败 {errors} 只[/]")
    Prompt.ask("\n按 Enter 返回")


def color_pct(pct: float) -> str:
    if pct > 0:
        return f"[bold red]+{pct:.2f}%[/]"
    elif pct < 0:
        return f"[bold green]{pct:.2f}%[/]"
    return f"{pct:.2f}%"


def _strip_prefix(code: str) -> str:
    """Strip exchange prefix for display: 'sh.600519' → '600519'."""
    for pfx in ("sh.", "sz.", "bj."):
        if code.startswith(pfx):
            return code[len(pfx):]
    return code


def _auto_prefix(num: str) -> str:
    """Infer exchange prefix from numeric code: '600519' → 'sh.600519'."""
    num = num.strip()
    return f"sh.{num}" if num[:1] in ("6", "9", "5") else f"sz.{num}"


# ── 个股详情 ────────────────────────────────────────────────────

def menu_stock_detail():
    """个股详情 — 输入代码查看历史K线和技术指标。"""
    console.print("\n[bold cyan]=== 个股详情 ===[/]")

    while True:
        raw = Prompt.ask("输入股票代码").strip()
        if not raw:
            Prompt.ask("\n按 Enter 返回")
            return
        code = _auto_prefix(raw) if raw.isdigit() else raw

        # 验证股票代码
        console.print("[dim]正在查询...[/]")
        rt = get_realtime_quotes([code])
        if not rt or not rt[0].get("name"):
            console.print(f"[red]未找到 {raw}，请确认代码[/]\n")
            continue

        name = rt[0]["name"]

        # 获取历史数据
        rows = db_manager.load_stock_history(code)
        if not rows:
            console.print(f"[yellow]{name} ({_strip_prefix(code)}) 暂无本地缓存数据[/]\n")
            continue

        total_days = len(rows)
        days = Prompt.ask(
            f"查看最近多少个交易日 (默认120, 最多{total_days})",
            default="120",
            show_default=True,
        )
        try:
            days = int(days) if days else 120
            days = max(min(days, total_days), 5)
        except ValueError:
            days = 120

        rows = rows[-days:][::-1]

        console.print(f"\n[bold]{name}[/] [dim]({_strip_prefix(code)}) 最近 {len(rows)} 个交易日[/]\n")

        t = Table(
            box=box.SIMPLE_HEAVY,
            header_style="bold magenta",
            show_lines=False,
        )
        t.add_column("日期", style="cyan", width=10)
        t.add_column("开盘", justify="right", width=8)
        t.add_column("最高", justify="right", width=8)
        t.add_column("最低", justify="right", width=8)
        t.add_column("收盘", justify="right", width=8)
        t.add_column("涨幅", justify="right", width=7)
        t.add_column("成交量", justify="right", width=10)
        t.add_column("成交额", justify="right", width=10)
        t.add_column("换手", justify="right", width=7)
        t.add_column("振幅", justify="right", width=7)

        for r in rows:
            pct = r.get("pctChg", 0) or 0
            vol = r.get("volume", 0) or 0
            amt = r.get("amount", 0) or 0
            t.add_row(
                str(r.get("date", "")),
                f"¥{r.get('open', 0) or 0:.2f}",
                f"¥{r.get('high', 0) or 0:.2f}",
                f"¥{r.get('low', 0) or 0:.2f}",
                f"¥{r.get('close', 0) or 0:.2f}",
                Text.from_markup(color_pct(pct)),
                f"{int(vol / 100):,}",
                f"{amt / 100000000:.2f}亿",
                f"{r.get('turn', 0) or 0:.1f}%",
                f"{r.get('amplitude', 0) or 0:.1f}%",
            )

        console.print(t)
        console.print()


# ── Main loop ────────────────────────────────────────────────────

def main():
    # Initialize database tables
    try:
        db_manager.init_database()
        # Backfill amplitude for existing rows that lack it
        n = db_manager.backfill_amplitude()
        if n > 0:
            console.print(f"[dim]已回填 {n} 条历史振幅数据[/]")
    except Exception as e:
        console.print(f"[bold red]数据库连接失败: {e}[/]")
        console.print("[yellow]请确保MySQL已启动，并检查 db_config.py 中的连接配置（主机/端口/用户名/密码/库名）[/]")
        return

    login()

    while True:
        console.clear()
        console.print("\n[bold cyan]股票量化分析终端[/]")
        console.print()
        console.print("  [f] 选股工具")
        console.print("  [g] 个股详情")
        console.print("  [c] 缓存管理 (全量/补全/增量)")
        console.print("  [q] 退出")
        console.print()

        key = Prompt.ask(
            "[dim]f选股 g个股 c缓存 q退出[/]",
            choices=["f", "c", "g", "q"],
            show_choices=False,
        )

        if key == "q":
            break
        elif key == "f":
            console.clear()
            menu_stock_picker()
        elif key == "g":
            console.clear()
            menu_stock_detail()
        elif key == "c":
            console.clear()
            menu_cache_manager()

    logout()


if __name__ == "__main__":
    main()
