"""
Stock quantitative analysis terminal.
Uses baostock for historical/end-of-day data; refreshes on demand.

Controls:
  [f] stock picker      [c] cache manager
  [q] quit
"""
from __future__ import annotations
import os
import json
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
)
import db_manager

console = Console()


# ─────────────────────────────────────────────────────────────────────────
# 选股配置管理函数
# ─────────────────────────────────────────────────────────────────────────

# ── 选股配置：文件为主，DB 为运行时缓存 ──

_STOCK_PICKER_CONFIG_FILE = "stock_picker_config.json"
_FILTERS_DIR = "filters"


def load_picker_config() -> dict:
    """加载选股配置：文件优先，DB 回退，最后用默认值。"""
    # 1. 优先从 JSON 文件读取（方便手动编辑）
    cfg = _load_json_file(_STOCK_PICKER_CONFIG_FILE)
    if cfg:
        return cfg
    # 2. 回退到数据库
    cfg = db_manager.load_config("stock_picker")
    if cfg:
        return cfg
    # 3. 最后使用代码默认值
    return _get_default_picker_config()


def load_preset_filters() -> dict[str, dict]:
    """加载预设筛选策略：文件优先，DB 回退。返回 {key: {name, config}}。"""
    presets = {}

    # 1. 优先从 filters/ 目录读取 JSON 文件
    if os.path.exists(_FILTERS_DIR):
        for idx, filename in enumerate(
            sorted(f for f in os.listdir(_FILTERS_DIR) if f.endswith(".json")), 1
        ):
            cfg = _load_json_file(os.path.join(_FILTERS_DIR, filename))
            if cfg:
                presets[str(idx)] = {"config": cfg, "name": cfg.get("name", filename)}

    # 2. 如果文件为空，尝试从 DB 加载
    if not presets:
        try:
            for i in range(1, 20):
                key = f"filter_0{i}" if i < 10 else f"filter_{i}"
                cfg = db_manager.load_config(key)
                if cfg:
                    presets[str(i)] = {"config": cfg, "name": cfg.get("name", f"Preset {i}")}
        except Exception:
            pass

    return presets


def _load_json_file(path: str) -> dict | None:
    """安全读取 JSON 文件，失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _get_default_picker_config() -> dict:
    """返回默认选股配置（代码内置兜底）。"""
    return {
        "filters": {
            "turnover": {"min": 2.0, "max": 50.0, "enabled": True},
            "amplitude": {"min": 2.0, "max": 20.0, "enabled": True},
            "pct_change": {"min": -5.0, "max": 15.0, "enabled": True},
            "price_range": {"min": 5, "max": 500, "enabled": True},
            "volume_rate": {"enabled": False, "min_vs_avg": 1.5, "days": 5},
            "rsi": {"enabled": False, "min": 20, "max": 80},
            "rsi_oversold": {"enabled": False, "threshold": 30},
            "rsi_overbought": {"enabled": False, "threshold": 70},
            "macd_golden_cross": {"enabled": False},
            "macd_death_cross": {"enabled": False},
            "kdj": {"enabled": False, "min": 10, "max": 90},
            "kdj_low_cross": {"enabled": False, "threshold": 30},
            "bb_position": {"enabled": False, "position": "lower"},
            "ma_trend": {"enabled": False, "type": "bullish"},
            "price_vs_ma20": {"enabled": False, "relation": "above", "pct": 2.0},
            "price_vs_ma60": {"enabled": False, "relation": "above", "pct": 5.0},
            "atr_ratio": {"enabled": False, "min": 0.5},
            "high_low_ratio": {"enabled": False, "min": 0.8},
            "avg_amplitude_120": {"enabled": False, "min": 7.0},
            "avg_turnover_120": {"enabled": False, "min": 2.0},
        },
        "signal_weights": {
            "macd_golden": 2.0,
            "rsi_oversold": 1.5,
            "kdj_cross": 1.2,
            "volume": 0.8,
        },
        "lookback_days": 120,
    }


def save_picker_config(cfg: dict):
    """同时写入 JSON 文件和数据库（双重保障）。"""
    # 写文件
    try:
        with open(_STOCK_PICKER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    # 写数据库
    try:
        db_manager.save_config("stock_picker", cfg, "选股配置")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
# 核心选股函数 - 按配置筛选股票
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

    # ── Step 3: 直接使用预计算指标应用筛选 ──
    candidates = []

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
        # 5 成交量突增 — 预取批量volume数据
        if _chk("volume_rate"):
            days_n = config["filters"]["volume_rate"].get("days", 5)
            min_r = config["filters"]["volume_rate"].get("min_vs_avg", 1.5)
            # Use pre-fetched avg volume dict
            avg_vol = _vol_avg_cache.get(code, 0)
            if avg_vol > 0:
                cur_r = volume / avg_vol
                if cur_r < min_r:
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
            pct = config["filters"]["price_vs_ma20"]["pct"]
            if rel == "above" and price < ma20 * (1 + pct / 100):
                passed = False
            if rel == "below" and price > ma20 * (1 - pct / 100):
                passed = False
        # 16 价格 vs MA60
        if _chk("price_vs_ma60"):
            rel = config["filters"]["price_vs_ma60"]["relation"]
            pct = config["filters"]["price_vs_ma60"]["pct"]
            if rel == "above" and price < ma60 * (1 + pct / 100):
                passed = False
            if rel == "below" and price > ma60 * (1 - pct / 100):
                passed = False
        # 17 ATR 波动性
        if _chk("atr_ratio"):
            ratio = atr / price * 100 if price > 0 else 0
            if ratio < config["filters"]["atr_ratio"]["min"]:
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
    """选股工具交互菜单 - 支持预设筛选条件"""
    console.print("\n[bold cyan]=== 选股工具 ===[/]")

    # 检查本地缓存状态
    cached_count = len(get_cached_stock_codes())
    if cached_count == 0:
        console.print("[red]本地无缓存数据！请先按 [bold]c[/bold] 进入缓存管理下载历史数据。[/]")
        Prompt.ask("\n按 Enter 返回")
        return

    # 从文件读取预设条件（文件优先，DB 回退）
    preset_configs = load_preset_filters()

    # 显示预设列表
    config = None
    if preset_configs:
        console.print("[cyan]可用的预设筛选条件:[/]")
        for key, info in preset_configs.items():
            console.print(f"  [{key}] {info['name']}")
        console.print("  [0] 使用默认配置")
        console.print("  [q] 返回")
        choice = Prompt.ask("\n选择筛选条件", default="0", show_choices=False)
        if choice == "q":
            return
        elif choice in preset_configs:
            config = preset_configs[choice]["config"]
            console.print(f"[green]已选择: {preset_configs[choice]['name']}[/]")
        else:
            config = load_picker_config()
    else:
        config = load_picker_config()

    # 确定股票池
    if config.get("pool"):
        pool = config["pool"]
        pool_desc = f"配置指定 ({len(pool)} 只)"
    else:
        pool = None  # pick_stocks 内部会自动取全部缓存股票
        pool_desc = f"全部缓存A股 ({cached_count} 只)"

    # 显示配置摘要
    enabled_filters = [k for k, v in config["filters"].items() if v.get("enabled", False)]
    console.print(f"\n[dim]股票池: {pool_desc}[/]")
    if enabled_filters:
        console.print(f"[dim]启用筛选: {', '.join(enabled_filters)}[/]")
    else:
        console.print("[dim]启用筛选: 基础条件 (换手率、振幅、涨幅、价格)[/]")

    lookback = Prompt.ask(
        "\n[dim]回溯交易日数[/]",
        default="60",
        show_default=True,
    )
    try:
        lookback = int(lookback)
        lookback = max(lookback, 10)  # minimum 10 trading days
    except ValueError:
        lookback = 120
    config["lookback_days"] = lookback

    choice = Prompt.ask(
        "\n[dim](s)开始选股 (q)返回[/]",
        choices=["s", "q"], default="s", show_choices=False,
    )
    if choice == "q":
        return

    console.print(f"\n[dim]正在筛选 {cached_count} 只股票 ({lookback}日回溯)...[/]")
    candidates = pick_stocks(pool, config)

    if not candidates:
        console.print("[yellow]未找到符合条件的股票，可以尝试放宽筛选条件[/]")
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
    console.print("  [2] 刷新全部 (重新下载所有A股数据)")
    console.print("  [q] 返回")

    choice = Prompt.ask("\n选择", choices=["1", "2", "q"], default="q", show_choices=False)
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
    else:
        to_fetch = all_codes
        action = f"刷新全部 ({len(to_fetch)} 只)"

    if not to_fetch:
        console.print("[green]缓存已是最新，无需下载[/]")
        Prompt.ask("\n按 Enter 返回")
        return

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

        days = Prompt.ask(
            "查看最近多少个交易日",
            default="60",
            show_default=True,
        )
        try:
            days = int(days)
            days = max(days, 5)
        except ValueError:
            days = 60

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
        t.add_column("换手", justify="right", width=7)
        t.add_column("振幅", justify="right", width=7)

        for r in rows:
            pct = r.get("pctChg", 0) or 0
            vol = r.get("volume", 0) or 0
            t.add_row(
                str(r.get("date", "")),
                f"¥{r.get('open', 0) or 0:.2f}",
                f"¥{r.get('high', 0) or 0:.2f}",
                f"¥{r.get('low', 0) or 0:.2f}",
                f"¥{r.get('close', 0) or 0:.2f}",
                Text.from_markup(color_pct(pct)),
                f"{int(vol / 100):,}",
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
        console.print("  [c] 缓存管理")
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
