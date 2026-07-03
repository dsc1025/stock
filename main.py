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
import pandas as pd

from data_engine import (
    login, logout, get_stock_history,
    add_indicators, generate_signals, get_market_snapshot, get_realtime_quotes,
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


def load_watchlist() -> list[str]:
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            codes = [line.strip() for line in f if line.strip()]
        return codes or DEFAULT_WATCHLIST[:]
    return DEFAULT_WATCHLIST[:]


def save_watchlist(codes: list[str]):
    with open(WATCHLIST_FILE, "w") as f:
        f.write("\n".join(codes))


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
            "[dim]r刷新 w行情 a分析 p持仓 o记录 b买 s卖 +加 -删 q退[/]",
            choices=["r","w","a","p","o","b","s","+","-","R","q"],
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
