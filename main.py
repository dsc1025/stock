"""
Stock quantitative analysis & simulated trading terminal.
Uses baostock for historical/end-of-day data; refreshes on demand.

Controls:
  [r] refresh data        [q] quit
  [w] watchlist view      [a] analysis view
  [p] portfolio view      [o] orders history
  [b] buy order           [s] sell order
  [+] add to watchlist    [-] remove from watchlist
  [R] reset portfolio
"""
from __future__ import annotations
import sys
import os
import time
import threading
import json
from datetime import datetime
from typing import Optional, List, Dict

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.columns import Columns
from rich.prompt import Prompt, Confirm
from rich.rule import Rule
from rich import box
from rich.progress import (
    Progress, SpinnerColumn, BarColumn,
    TextColumn, TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn,
)
import pandas as pd

from data_engine import (
    login, logout, get_stock_history, clear_hist_cache,
    add_indicators, generate_signals, get_market_snapshot, get_realtime_quotes,
    get_all_stock_codes, get_cached_stock_codes, load_hist_from_disk, refresh_hist_cache,
)
from trading_engine import Portfolio

console = Console()

# Default watchlist (A-share codes)
DEFAULT_WATCHLIST = [
    "sh.600519",  # 贵州茅台
    "sh.601318",  # 中国平安
    "sh.600036",  # 招商银行
    "sz.000858",  # 五粮液
    "sh.600900",  # 长江电力
    "sz.300750",  # 宁德时代
    "sz.000001",  # 平安银行
    "sh.688599",  # 天合光能
]

WATCHLIST_FILE = "watchlist.txt"
PORTFOLIO_INITIAL = 1_000_000.0

# ═════════════════════════════════════════════════════════════════════════
# 股票选股模块配置
# ═════════════════════════════════════════════════════════════════════════

STOCK_PICKER_CONFIG = "stock_picker_config.json"

# A股备用扩展池 - 当自选股数量不足时补充候选股票
# 注意：选股时会优先使用自选股 + filter配置中的pool字段
A_SHARE_POOL = [
    "sh.600519",  # 贵州茅台
    "sh.601318",  # 中国平安
    "sh.600036",  # 招商银行
    "sz.000858",  # 五粮液
    "sh.600900",  # 长江电力
    "sz.300750",  # 宁德时代
    "sz.000001",  # 平安银行
    "sh.688599",  # 天合光能
    "sz.000651",  # 格力电器
    "sh.601988",  # 中国银行
    "sh.603501",  # 韦尔股份
    "sz.002594",  # 比亚迪
    "sh.600028",  # 中国石化
    "sz.000333",  # 美的集团
    "sh.601398",  # 工商银行
    "sh.600030",  # 中信证券
    "sz.000002",  # 万科A
    "sh.601166",  # 兴业银行
    "sz.300059",  # 东方财富
    "sh.600276",  # 恒瑞医药
    "sz.000538",  # 云南白药
    "sh.601888",  # 中国中免
    "sz.002415",  # 海康威视
    "sh.600887",  # 伊利股份
    "sz.000725",  # 京东方A
    "sh.601601",  # 中国太保
    "sz.300015",  # 爱尔眼科
    "sh.600050",  # 中国联通
    "sz.002236",  # 大华股份
    "sh.601919",  # 中远海控
]


def load_watchlist() -> list[str]:
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            codes = [line.strip() for line in f if line.strip()]
        return codes or DEFAULT_WATCHLIST[:]
    return DEFAULT_WATCHLIST[:]


def save_watchlist(codes: list[str]):
    with open(WATCHLIST_FILE, "w") as f:
        f.write("\n".join(codes))


# ─────────────────────────────────────────────────────────────────────────
# 选股配置管理函数
# ─────────────────────────────────────────────────────────────────────────

def load_picker_config() -> dict:
    """加载选股配置文件，如果不存在则返回默认配置"""
    if os.path.exists(STOCK_PICKER_CONFIG):
        with open(STOCK_PICKER_CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    return get_default_picker_config()


def get_default_picker_config() -> dict:
    """返回默认选股配置 - 包含所有可用的筛选因子"""
    return {
        "filters": {
            "turnover": {"min": 2.0, "max": 50.0, "enabled": True},  # 换手率(%)范围
            "amplitude": {"min": 2.0, "max": 20.0, "enabled": True},  # 振幅(%)范围
            "pct_change": {"min": -5.0, "max": 15.0, "enabled": True},  # 涨跌幅(%)范围
            "price_range": {"min": 5, "max": 500, "enabled": True},  # 股价范围(¥)
            "volume_rate": {"enabled": False, "min_vs_avg": 1.5, "days": 5},  # 成交量倍数
            "rsi": {"enabled": False, "min": 20, "max": 80},  # RSI值范围
            "rsi_oversold": {"enabled": False, "threshold": 30},  # RSI超卖(<30)
            "rsi_overbought": {"enabled": False, "threshold": 70},  # RSI超买(>70)
            "macd_golden_cross": {"enabled": False},  # MACD金叉信号
            "macd_death_cross": {"enabled": False},  # MACD死叉信号
            "kdj": {"enabled": False, "min": 10, "max": 90},  # KDJ-K值范围
            "kdj_low_cross": {"enabled": False, "threshold": 30},  # KDJ低位金叉
            "bb_position": {"enabled": False, "position": "lower"},  # 布林带位置
            "ma_trend": {"enabled": False, "type": "bullish"},  # 均线趋势
            "price_vs_ma20": {"enabled": False, "relation": "above", "pct": 2.0},  # 价格vs MA20
            "price_vs_ma60": {"enabled": False, "relation": "above", "pct": 5.0},  # 价格vs MA60
            "atr_ratio": {"enabled": False, "min": 0.5},  # ATR波动性
            "high_low_ratio": {"enabled": False, "min": 0.8},  # 最低价/最高价比值
        },
        "signal_weights": {
            "macd_golden": 2.0,  # MACD金叉权重
            "rsi_oversold": 1.5,  # RSI超卖权重
            "kdj_cross": 1.2,  # KDJ金叉权重
            "volume": 0.8,  # 成交量权重
        },
        "max_results": 20,  # 最多返回20个结果
    }


def save_picker_config(cfg: dict):
    """保存选股配置到JSON文件"""
    with open(STOCK_PICKER_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────
# 核心选股函数 - 按配置筛选股票
# ─────────────────────────────────────────────────────────────────────────

def pick_stocks(pool: list[str] | None, config: dict) -> list[dict]:
    """
    核心选股引擎 - 使用本地磁盘缓存进行筛选，无需实时网络请求。

    处理流程：
    1. pool=None 时自动取所有已缓存股票
    2. 读磁盘CSV，计算指标并应用筛选
    3. 仅对通过筛选的少量候选股批量拉取实时欧（取名称+实时价）
    """
    if not pool:
        pool = get_cached_stock_codes()
    if not pool:
        return []

    candidates = []

    for code in pool:
        # 读磁盘缓存（毫秒级，不发网络请求）
        df_hist = load_hist_from_disk(code)
        if df_hist is None or len(df_hist) < 2:
            continue

        try:
            df_hist = add_indicators(df_hist)
            last = df_hist.iloc[-1]   # 最新一个交易日
            prev = df_hist.iloc[-2]   # 前一个交易日

            # 从缓存数据提取基本指标
            price      = float(last["close"])
            open_      = float(last["open"])  if pd.notna(last.get("open"))  else price
            high_      = float(last["high"])  if pd.notna(last.get("high"))  else price
            low_       = float(last["low"])   if pd.notna(last.get("low"))   else price
            turnover   = float(last.get("turn",   0) or 0)
            pct_change = float(last.get("pctChg", 0) or 0)
            amplitude  = (high_ - low_) / open_ * 100 if open_ > 0 else 0

            # 技术指标
            rsi    = float(last.get("RSI14", 50) or 50)
            k      = float(last.get("K",     50) or 50)
            d      = float(last.get("D",     50) or 50)
            dif    = float(last.get("DIF",    0) or 0)
            dea    = float(last.get("DEA",    0) or 0)
            prev_k = float(prev.get("K",     50) or 50)
            prev_d = float(prev.get("D",     50) or 50)
            prev_dif = float(prev.get("DIF",  0) or 0)
            prev_dea = float(prev.get("DEA",  0) or 0)
            ma20   = float(last.get("MA20",  price) or price)
            ma60   = float(last.get("MA60",  price) or price)
            bb_up  = float(last.get("BB_UP", price + 1) or price + 1)
            bb_lo  = float(last.get("BB_LO", price - 1) or price - 1)
            atr    = float(last.get("ATR14",  0) or 0)

        except Exception:
            continue

        # ══ 执行筛选条件 ══
        passed = True
        scores: dict[str, float] = {}

        def _chk(key: str) -> bool:
            return config["filters"][key].get("enabled", False)

        # 1 换手率
        if _chk("turnover"):
            lo = config["filters"]["turnover"]["min"]
            hi = config["filters"]["turnover"].get("max", 100)
            if not (lo <= turnover <= hi): passed = False
        # 2 振幅
        if _chk("amplitude"):
            if amplitude < config["filters"]["amplitude"]["min"]: passed = False
        # 3 涨跌幅
        if _chk("pct_change"):
            lo = config["filters"]["pct_change"]["min"]
            hi = config["filters"]["pct_change"]["max"]
            if not (lo <= pct_change <= hi): passed = False
        # 4 股价范围
        if _chk("price_range"):
            lo = config["filters"]["price_range"]["min"]
            hi = config["filters"]["price_range"]["max"]
            if not (lo <= price <= hi): passed = False
        # 5 成交量突增
        if _chk("volume_rate"):
            days_n = config["filters"]["volume_rate"].get("days", 5)
            min_r  = config["filters"]["volume_rate"].get("min_vs_avg", 1.5)
            avg_vol = df_hist.iloc[-days_n:]["volume"].mean()
            if avg_vol > 0:
                cur_r = float(last["volume"]) / avg_vol
                if cur_r < min_r: passed = False
                scores["volume"] = min(cur_r, 2.0)
        # 6 RSI 范围
        if _chk("rsi"):
            if not (config["filters"]["rsi"]["min"] <= rsi <= config["filters"]["rsi"]["max"]):
                passed = False
        # 7 RSI 超卖
        if _chk("rsi_oversold"):
            thr = config["filters"]["rsi_oversold"]["threshold"]
            if rsi >= thr: passed = False
            scores["rsi_oversold"] = 1.0 - rsi / thr
        # 8 RSI 超买
        if _chk("rsi_overbought"):
            thr = config["filters"]["rsi_overbought"]["threshold"]
            if rsi <= thr: passed = False
            scores["rsi_overbought"] = rsi / 100 - thr / 100
        # 9 MACD 金叉
        if _chk("macd_golden_cross"):
            is_cross = prev_dif < prev_dea and dif > dea
            if not is_cross: passed = False
            scores["macd_golden"] = 2.0 if is_cross else 0
        # 10 MACD 死叉
        if _chk("macd_death_cross"):
            is_cross = prev_dif > prev_dea and dif < dea
            if not is_cross: passed = False
            scores["macd_death"] = 1.0 if is_cross else 0
        # 11 KDJ 范围
        if _chk("kdj"):
            if not (config["filters"]["kdj"]["min"] <= k <= config["filters"]["kdj"]["max"]):
                passed = False
        # 12 KDJ 低位金叉
        if _chk("kdj_low_cross"):
            thr = config["filters"]["kdj_low_cross"]["threshold"]
            is_cross = prev_k < prev_d and k > d and k < thr
            if not is_cross: passed = False
            scores["kdj_cross"] = 1.2 if is_cross else 0
        # 13 布林带位置
        if _chk("bb_position"):
            pos = config["filters"]["bb_position"]["position"]
            dist = (price - bb_lo) / (bb_up - bb_lo) if bb_up > bb_lo else 0.5
            if pos == "lower":
                if dist > 0.3: passed = False
                scores["bb"] = 1.0 - dist
            elif pos == "upper":
                if dist < 0.7: passed = False
                scores["bb"] = dist
        # 14 均线趋势
        if _chk("ma_trend"):
            t = config["filters"]["ma_trend"]["type"]
            if t == "bullish" and not (price > ma20 > ma60): passed = False
            if t == "bearish" and not (price < ma20 < ma60): passed = False
            scores["ma_trend"] = 1.0
        # 15 价格 vs MA20
        if _chk("price_vs_ma20"):
            rel = config["filters"]["price_vs_ma20"]["relation"]
            pct = config["filters"]["price_vs_ma20"]["pct"]
            if rel == "above" and price < ma20 * (1 + pct / 100): passed = False
            if rel == "below" and price > ma20 * (1 - pct / 100): passed = False
        # 16 价格 vs MA60
        if _chk("price_vs_ma60"):
            rel = config["filters"]["price_vs_ma60"]["relation"]
            pct = config["filters"]["price_vs_ma60"]["pct"]
            if rel == "above" and price < ma60 * (1 + pct / 100): passed = False
            if rel == "below" and price > ma60 * (1 - pct / 100): passed = False
        # 17 ATR 波动性
        if _chk("atr_ratio"):
            ratio = atr / price * 100 if price > 0 else 0
            if ratio < config["filters"]["atr_ratio"]["min"]: passed = False
            scores["atr"] = min(ratio / 2, 1.0)
        # 18 最低价/最高价比值
        if _chk("high_low_ratio"):
            hl = low_ / high_ if high_ > 0 else 0
            if hl < config["filters"]["high_low_ratio"]["min"]: passed = False
            scores["hl_ratio"] = hl

        if not passed:
            continue

        # 计算综合评分
        score = turnover + amplitude + pct_change
        weights = config.get("signal_weights", {})
        for k_name, val in scores.items():
            score += val * weights.get(k_name, 1.0)

        candidates.append({
            "code": code,
            "name": code,         # 名称待后面实时更新
            "price": price,
            "pct_change": pct_change,
            "turnover": turnover,
            "amplitude": amplitude,
            "volume": float(last.get("volume", 0) or 0),
            "rsi": rsi,
            "k": k,
            "macd_positive": dif > dea,
            "price_above_ma20": price > ma20,
            "price_above_ma60": price > ma60,
            "score": score,
        })

    # 仅对通过筛选的少量候选股，批量获取实时行情（取名称+实时价）
    if candidates:
        rt_map = {q["code"]: q for q in get_realtime_quotes([c["code"] for c in candidates])}
        for c in candidates:
            rt = rt_map.get(c["code"])
            if rt:
                c["name"] = rt.get("name", c["code"])
                if rt.get("close", 0) > 0:
                    c["price"]      = rt["close"]
                    c["pct_change"] = rt.get("pctChg", c["pct_change"])

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[: config.get("max_results", 20)]


# ─────────────────────────────────────────────────────────────────────────
# 选股结果展示
# ─────────────────────────────────────────────────────────────────────────

def make_picker_table(candidates: list[dict]) -> Table:
    """将选股结果显示为富文本表格"""
    t = Table(
        title="[bold cyan]选股结果[/]",
        box=box.SIMPLE_HEAVY,
        header_style="bold magenta",
        show_lines=True,
    )
    
    t.add_column("代码", style="cyan", width=8)
    t.add_column("名称", width=10)
    t.add_column("价格", justify="right", width=8)
    t.add_column("涨幅", justify="right", width=8)
    t.add_column("换手", justify="right", width=8)
    t.add_column("振幅", justify="right", width=8)
    t.add_column("RSI", justify="right", width=6)
    t.add_column("K值", justify="right", width=6)
    t.add_column("MACD", justify="center", width=6)
    t.add_column("MA20", justify="center", width=6)
    t.add_column("MA60", justify="center", width=6)
    t.add_column("评分", justify="right", width=8)
    
    for c in candidates:
        macd_mark = "✓" if c["macd_positive"] else "✗"
        ma20_mark = "↑" if c["price_above_ma20"] else "↓"
        ma60_mark = "↑" if c["price_above_ma60"] else "↓"
        
        t.add_row(
            _strip_prefix(c["code"]),
            c["name"],
            f"¥{c['price']:.2f}",
            Text.from_markup(color_pct(c["pct_change"])),
            f"{c['turnover']:.1f}%",
            f"{c['amplitude']:.1f}%",
            f"{c['rsi']:.0f}",
            f"{c['k']:.0f}",
            macd_mark,
            ma20_mark,
            ma60_mark,
            f"{c['score']:.1f}",
        )
    
    return t


def menu_stock_picker(watchlist: list[str]):
    """选股工具交互菜单 - 支持预设筛选条件"""
    console.print("\n[bold cyan]=== 选股工具 ===[/]")

    # 检查本地缓存状态
    cached_count = len(get_cached_stock_codes())
    if cached_count == 0:
        console.print("[red]本地无缓存数据！请先按 [bold]c[/bold] 进入缓存管理下载历史数据。[/]")
        Prompt.ask("\n按 Enter 返回")
        return

    # 从 filters 目录读取预设条件
    filters_dir = "filters"
    preset_configs = {}
    if os.path.exists(filters_dir):
        for idx, filename in enumerate(sorted(f for f in os.listdir(filters_dir) if f.endswith(".json")), 1):
            try:
                with open(os.path.join(filters_dir, filename), "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                preset_configs[str(idx)] = {"config": cfg, "name": cfg.get("name", filename)}
            except Exception:
                continue

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

    choice = Prompt.ask(
        "\n[dim](s)开始选股 (q)返回[/]",
        choices=["s", "q"], default="s", show_choices=False,
    )
    if choice == "q":
        return

    console.print(f"\n[dim]正在筛选 {cached_count} 只股票...[/]")
    candidates = pick_stocks(pool, config)

    if not candidates:
        console.print("[yellow]未找到符合条件的股票，可以尝试放宽筛选条件[/]")
    else:
        console.print(make_picker_table(candidates))
        console.print(f"\n[green]共找到 {len(candidates)} 只符合条件的股票[/]")

    Prompt.ask("\n按 Enter 返回")


def menu_cache_manager():
    """缓存管理菜单 - 下载/刷新所有A股历史数据到本地"""
    console.print("\n[bold cyan]=== 股票数据缓存管理 ===[/]")

    cached = get_cached_stock_codes()
    console.print(f"[dim]本地已缓存: [bold]{len(cached)}[/] 只股票[/]")
    if cached:
        # 显示最近一次修改的时间
        latest_mtime = 0.0
        cache_dir = "cache/hist"
        for fname in os.listdir(cache_dir) if os.path.exists(cache_dir) else []:
            p = os.path.join(cache_dir, fname)
            latest_mtime = max(latest_mtime, os.path.getmtime(p))
        if latest_mtime:
            last_upd = datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M")
            console.print(f"[dim]最近更新: {last_upd}[/]")

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

        success, errors = refresh_hist_cache(to_fetch, on_prog)

    console.print(f"\n[green]完成！成功 {success} 只，失败 {errors} 只[/]")
    Prompt.ask("\n按 Enter 返回")


def color_pct(pct: float) -> str:
    if pct > 0:
        return f"[bold red]+{pct:.2f}%[/]"
    elif pct < 0:
        return f"[bold green]{pct:.2f}%[/]"
    return f"{pct:.2f}%"


def color_val(val: float) -> str:
    if val > 0:
        return f"[bold red]+{val:,.2f}[/]"
    elif val < 0:
        return f"[bold green]{val:,.2f}[/]"
    return f"{val:,.2f}"


def _strip_prefix(code: str) -> str:
    """Strip exchange prefix for display: 'sh.600519' → '600519'."""
    for pfx in ("sh.", "sz.", "bj."):
        if code.startswith(pfx):
            return code[len(pfx):]
    return code


def _auto_prefix(num: str) -> str:
    """Infer exchange from numeric code: '600519' → 'sh.600519', '000001' → 'sz.000001'."""
    num = num.strip()
    return f"sh.{num}" if num[:1] in ("6", "9", "5") else f"sz.{num}"


# ── Views ────────────────────────────────────────────────────────

def make_watchlist_table(snapshots: list[dict], portfolio: Portfolio) -> Table:
    prices = {s["code"]: s["close"] for s in snapshots}
    held = set(portfolio.positions.keys())

    t = Table(
        title="[bold cyan]实时行情[/]",
        box=box.SIMPLE_HEAVY,
        header_style="bold magenta",
        show_lines=True,
    )
    t.add_column("代码", style="cyan", width=12)
    t.add_column("名称", width=12)
    t.add_column("最新价", justify="right", width=10)
    t.add_column("涨跌幅", justify="right", width=10)
    t.add_column("开盘", justify="right", width=10)
    t.add_column("最高", justify="right", width=10)
    t.add_column("最低", justify="right", width=10)
    t.add_column("成交量(手)", justify="right", width=12)
    t.add_column("持仓", justify="center", width=6)

    for s in snapshots:
        code = s["code"]
        name = s.get("name", "")
        held_mark = "[bold yellow]★[/]" if code in held else ""
        t.add_row(
            _strip_prefix(code),
            name,
            f"¥{s['close']:.2f}",
            Text.from_markup(color_pct(s["pctChg"])),
            f"¥{s['open']:.2f}",
            f"¥{s['high']:.2f}",
            f"¥{s['low']:.2f}",
            f"{int(s['volume']/100):,}",
            held_mark,
        )
    return t


def make_analysis_panel(code: str, name: str = "", realtime_price: float = 0, realtime_pct: float = 0) -> Panel:
    df = get_stock_history(code, days=60)
    if df.empty:
        return Panel(f"[red]无法获取 {code} 历史数据[/]", title="分析")

    df = add_indicators(df)
    last = df.iloc[-1]
    signals = generate_signals(df)
    
    current = realtime_price
    current_pct = realtime_pct

    # Stats grid
    stats = Table.grid(expand=True)
    stats.add_column(ratio=1)
    stats.add_column(ratio=1)
    stats.add_column(ratio=1)
    stats.add_column(ratio=1)

    def stat(label, value): return f"[dim]{label}:[/] [bold]{value}[/]"

    stats.add_row(
        stat("实时价格", f"¥{current:.2f}"),
        stat("涨跌幅", color_pct(current_pct)),
        stat("成交额", f"¥{last['amount']/1e8:.2f}亿"),
        stat("换手率", f"{last['turn']:.2f}%") if pd.notna(last.get("turn")) else "",
    )
    stats.add_row(
        stat("MA5", f"¥{last['MA5']:.2f}" if pd.notna(last.get("MA5")) else "N/A"),
        stat("MA10", f"¥{last['MA10']:.2f}" if pd.notna(last.get("MA10")) else "N/A"),
        stat("MA20", f"¥{last['MA20']:.2f}" if pd.notna(last.get("MA20")) else "N/A"),
        stat("MA60", f"¥{last['MA60']:.2f}" if pd.notna(last.get("MA60")) else "N/A"),
    )
    stats.add_row(
        stat("RSI14", f"{last['RSI14']:.1f}" if pd.notna(last.get("RSI14")) else "N/A"),
        stat("MACD", f"{last['MACD']:.4f}" if pd.notna(last.get("MACD")) else "N/A"),
        stat("KDJ-K", f"{last['K']:.1f}" if pd.notna(last.get("K")) else "N/A"),
        stat("ATR14", f"¥{last['ATR14']:.2f}" if pd.notna(last.get("ATR14")) else "N/A"),
    )
    stats.add_row(
        stat("布林上轨", f"¥{last['BB_UP']:.2f}" if pd.notna(last.get("BB_UP")) else "N/A"),
        stat("布林中轨", f"¥{last['BB_MID']:.2f}" if pd.notna(last.get("BB_MID")) else "N/A"),
        stat("布林下轨", f"¥{last['BB_LO']:.2f}" if pd.notna(last.get("BB_LO")) else "N/A"),
        stat("历史收盘", f"¥{last['close']:.2f}" if pd.notna(last.get("close")) else "N/A"),
    )

    # Mini chart (price sparkline)
    chart = _sparkline(df["close"].tail(30).tolist())

    # Signals
    sig_text = "\n".join(f"  • {s}" for s in signals) if signals else "  暂无明显信号"

    content = Table.grid(expand=True)
    content.add_column()
    content.add_row(stats)
    content.add_row(f"\n[dim]近30日价格走势:[/] {chart}\n")
    content.add_row(f"[bold yellow]量化信号:[/]\n{sig_text}")

    return Panel(
        content,
        title=f"[bold cyan]深度分析: {name} ({_strip_prefix(code)})[/]",
        border_style="cyan",
    )


def make_portfolio_panel(portfolio: Portfolio, prices: dict[str, float]) -> Panel:
    total = portfolio.total_assets(prices)
    mv = portfolio.market_value(prices)
    pnl = portfolio.pnl(prices)
    pnl_pct = portfolio.pnl_pct(prices)

    summary = Table.grid(expand=True)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_row(
        f"[dim]总资产:[/] [bold]¥{total:,.2f}[/]",
        f"[dim]可用资金:[/] [bold]¥{portfolio.cash:,.2f}[/]",
        f"[dim]持仓市值:[/] [bold]¥{mv:,.2f}[/]",
        f"[dim]总盈亏:[/] {color_val(pnl)} ({color_pct(pnl_pct)})",
    )

    pos_table = Table(
        box=box.SIMPLE,
        header_style="bold magenta",
        show_lines=True,
    )
    pos_table.add_column("代码", style="cyan")
    pos_table.add_column("名称")
    pos_table.add_column("持股数", justify="right")
    pos_table.add_column("成本价", justify="right")
    pos_table.add_column("现价", justify="right")
    pos_table.add_column("持仓市值", justify="right")
    pos_table.add_column("盈亏", justify="right")
    pos_table.add_column("盈亏%", justify="right")
    pos_table.add_column("买入日期")

    if not portfolio.positions:
        pos_table.add_row(*["—"] * 9)
    else:
        for code, pos in portfolio.positions.items():
            price = prices.get(code, pos.avg_cost)
            p_pnl, p_pnl_pct = portfolio.position_pnl(code, price)
            pos_table.add_row(
                _strip_prefix(code),
                pos.name,
                f"{pos.shares:,}",
                f"¥{pos.avg_cost:.2f}",
                f"¥{price:.2f}",
                f"¥{pos.shares * price:,.2f}",
                Text.from_markup(color_val(p_pnl)),
                Text.from_markup(color_pct(p_pnl_pct)),
                pos.buy_date,
            )

    content = Table.grid(expand=True)
    content.add_row(summary)
    content.add_row(Rule(style="dim"))
    content.add_row(pos_table)

    return Panel(content, title="[bold cyan]模拟持仓[/]", border_style="green")


def make_orders_panel(portfolio: Portfolio) -> Panel:
    t = Table(box=box.SIMPLE, header_style="bold magenta", show_lines=False)
    t.add_column("单号", width=6)
    t.add_column("时间", width=20)
    t.add_column("代码", width=12)
    t.add_column("方向", width=6)
    t.add_column("数量", justify="right", width=8)
    t.add_column("价格", justify="right", width=10)
    t.add_column("金额", justify="right", width=14)
    t.add_column("备注")

    orders = list(reversed(portfolio.orders[-50:]))
    for o in orders:
        side_style = "[red]买入[/]" if o.side == "buy" else "[green]卖出[/]"
        t.add_row(
            str(o.order_id),
            o.timestamp,
            _strip_prefix(o.code),
            Text.from_markup(side_style),
            f"{o.shares:,}",
            f"¥{o.price:.2f}",
            f"¥{o.amount:,.2f}",
            o.note,
        )

    return Panel(t, title="[bold cyan]成交记录 (最近50条)[/]", border_style="blue")


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    bars = "▁▂▃▄▅▆▇█"
    if hi == lo:
        return bars[4] * len(values)
    result = ""
    for v in values:
        idx = int((v - lo) / (hi - lo) * (len(bars) - 1))
        result += bars[idx]
    return result


# ── Interactive menus ────────────────────────────────────────────

def menu_buy(portfolio: Portfolio, watchlist: list[str], prices: dict[str, float]):
    console.print("\n[bold cyan]=== 模拟买入 ===[/]")
    for i, code in enumerate(watchlist):
        p = prices.get(code, 0)
        console.print(f"  {i+1}. {_strip_prefix(code)}  ¥{p:.2f}")

    choice = Prompt.ask("输入股票代码 (如 600519) 或序号", default="")
    if not choice:
        return
    if choice.isdigit() and len(choice) <= 2:
        idx = int(choice) - 1
        if 0 <= idx < len(watchlist):
            code = watchlist[idx]
        else:
            console.print("[red]无效序号[/]")
            return
    elif choice.isdigit():
        code = _auto_prefix(choice)
    else:
        code = choice.strip()

    price = prices.get(code, 0)
    if price == 0:
        try:
            price = float(Prompt.ask("未在行情列表中, 请手动输入价格"))
        except ValueError:
            console.print("[red]价格无效[/]")
            return

    rt = get_realtime_quotes([code])
    name = rt[0]["name"] if rt else code
    max_shares = portfolio.max_buyable_shares(price)
    console.print(f"[dim]{name} @ ¥{price:.2f}, 最多可买 {max_shares} 股[/]")

    try:
        shares = int(Prompt.ask("买入股数 (100的整数倍)", default="100"))
    except ValueError:
        console.print("[red]数量无效[/]")
        return

    note = Prompt.ask("备注 (可选)", default="")
    ok, msg = portfolio.buy(code, name, shares, price, note)
    if ok:
        console.print(f"[bold green]{msg}[/]")
    else:
        console.print(f"[bold red]{msg}[/]")
    time.sleep(1)


def menu_sell(portfolio: Portfolio, prices: dict[str, float]):
    console.print("\n[bold cyan]=== 模拟卖出 ===[/]")
    if not portfolio.positions:
        console.print("[yellow]暂无持仓[/]")
        time.sleep(1)
        return

    codes = list(portfolio.positions.keys())
    for i, code in enumerate(codes):
        pos = portfolio.positions[code]
        price = prices.get(code, pos.avg_cost)
        pnl, pnl_pct = portfolio.position_pnl(code, price)
        console.print(
            f"  {i+1}. {_strip_prefix(code)} {pos.name}  持{pos.shares}股  "
            f"现价¥{price:.2f}  {color_pct(pnl_pct)}"
        )

    choice = Prompt.ask("输入序号或股票代码")
    if not choice:
        return
    if choice.isdigit() and len(choice) <= 2:
        idx = int(choice) - 1
        if 0 <= idx < len(codes):
            code = codes[idx]
        else:
            console.print("[red]无效序号[/]")
            return
    elif choice.isdigit():
        code = _auto_prefix(choice)
    else:
        code = choice.strip()

    if code not in portfolio.positions:
        console.print(f"[red]未持有 {code}[/]")
        time.sleep(1)
        return

    pos = portfolio.positions[code]
    price = prices.get(code, pos.avg_cost)
    console.print(f"[dim]当前持有 {pos.shares} 股, 现价 ¥{price:.2f}[/]")

    try:
        shares = int(Prompt.ask(f"卖出股数 (100的整数倍, 最多{pos.shares})", default=str(pos.shares)))
    except ValueError:
        console.print("[red]数量无效[/]")
        return

    note = Prompt.ask("备注 (可选)", default="")
    ok, msg = portfolio.sell(code, shares, price, note)
    if ok:
        console.print(f"[bold green]{msg}[/]")
    else:
        console.print(f"[bold red]{msg}[/]")
    time.sleep(1)


def menu_add_stock(watchlist: list[str]) -> list[str]:
    raw = Prompt.ask("输入添加的股票代码 (如 600519)").strip()
    if not raw:
        return watchlist
    code = _auto_prefix(raw) if raw.isdigit() else raw
    if code in watchlist:
        console.print(f"[yellow]{_strip_prefix(code)} 已在自选股中[/]")
        time.sleep(0.5)
        return watchlist
    console.print("[dim]正在验证股票代码...[/]", end="")
    rt = get_realtime_quotes([code])
    if not rt or not rt[0]["name"]:
        console.print(f"\n[red]未找到 {raw}, 请确认代码 (如 600519 或 sh.600519)[/]")
        time.sleep(1.5)
        return watchlist
    name = rt[0]["name"]
    watchlist.append(code)
    save_watchlist(watchlist)
    console.print(f"\n[green]已添加: {name} ({_strip_prefix(code)})[/]")
    time.sleep(0.8)
    return watchlist


def menu_remove_stock(watchlist: list[str]) -> list[str]:
    for i, code in enumerate(watchlist):
        console.print(f"  {i+1}. {_strip_prefix(code)}")
    choice = Prompt.ask("输入要删除的序号")
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(watchlist):
            removed = watchlist.pop(idx)
            save_watchlist(watchlist)
            console.print(f"[yellow]已删除 {removed}[/]")
    except (ValueError, IndexError):
        console.print("[red]无效序号[/]")
    time.sleep(0.5)
    return watchlist


def menu_analysis(watchlist: list[str]):
    for i, code in enumerate(watchlist):
        console.print(f"  {i+1}. {_strip_prefix(code)}")
    choice = Prompt.ask("选择分析的股票 (序号或代码)").strip()
    if choice.isdigit() and len(choice) <= 2:
        idx = int(choice) - 1
        if 0 <= idx < len(watchlist):
            code = watchlist[idx]
        else:
            return
    elif choice.isdigit():
        code = _auto_prefix(choice)
    else:
        code = choice

    console.print(f"\n[dim]正在获取 {code} 数据...[/]")
    rt = get_realtime_quotes([code])
    if not rt:
        console.print("[red]无法获取行情数据[/]")
        Prompt.ask("\n按 Enter 返回")
        return
    panel = make_analysis_panel(code, rt[0]["name"], rt[0]["close"], rt[0]["pctChg"])
    console.print(panel)
    Prompt.ask("\n按 Enter 返回")


# ── Main loop ────────────────────────────────────────────────────

def main():
    login()

    watchlist = load_watchlist()
    portfolio = Portfolio(initial_cash=PORTFOLIO_INITIAL)
    portfolio.load()

    view = "w"        # w=watchlist, p=portfolio, o=orders
    snapshots: list[dict] = []
    last_refresh = ""

    def refresh():
        nonlocal snapshots, last_refresh
        snapshots = get_market_snapshot(watchlist)
        last_refresh = datetime.now().strftime("%H:%M:%S")

    refresh()   # initial load

    while True:
        prices = {s["code"]: s["close"] for s in snapshots}
        console.clear()
        console.print(Rule(f"[dim]更新: {last_refresh}[/]"))

        if view == "w":
            if snapshots:
                console.print(make_watchlist_table(snapshots, portfolio))
            else:
                console.print("[yellow]暂无行情数据, 按 r 刷新[/]")

        elif view == "p":
            console.print(make_portfolio_panel(portfolio, prices))

        elif view == "o":
            console.print(make_orders_panel(portfolio))

        console.print()
        key = Prompt.ask(
            "[dim]r刷新 w行情 a分析 f选股 c缓存 p持仓 o记录 b买 s卖 +加 -删 q退[/]",
            choices=["r","w","a","f","c","p","o","b","s","+","-","R","q"],
            show_choices=False,
        )

        if key == "q":
            break
        elif key == "r":
            refresh()
        elif key == "w":
            view = "w"
        elif key == "p":
            view = "p"
        elif key == "o":
            view = "o"
        elif key == "a":
            console.clear()
            menu_analysis(watchlist)
            snapshots = get_market_snapshot(watchlist)  # refresh after analysis
        elif key == "f":
            console.clear()
            menu_stock_picker(watchlist)
        elif key == "c":
            console.clear()
            menu_cache_manager()
        elif key == "b":
            console.clear()
            menu_buy(portfolio, watchlist, prices)
            view = "p"
        elif key == "s":
            console.clear()
            menu_sell(portfolio, prices)
            view = "p"
        elif key == "+":
            console.clear()
            watchlist = menu_add_stock(watchlist)
        elif key == "-":
            console.clear()
            watchlist = menu_remove_stock(watchlist)
        elif key == "R":
            if Confirm.ask("[bold red]确认重置模拟账户? 所有持仓和记录将清空[/]"):
                portfolio.reset(PORTFOLIO_INITIAL)
                console.print("[green]账户已重置[/]")
                time.sleep(1)

    logout()


if __name__ == "__main__":
    main()
