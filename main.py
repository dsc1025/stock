"""
Stock quantitative analysis terminal — refactored.
Architecture:
  db_schema.py        → table definitions
  repository/stock_repo.py → data access layer
  services/data_fetcher.py → baostock + Sina API
  services/data_sync.py    → download orchestration
  indicators.py       → technical indicator computation
  stock_picker.py     → selection engine (local DB only)
  ui/display.py       → Rich table rendering

Controls:
  [f] stock picker      [g] stock detail
  [c] cache manager     [q] quit
"""
from __future__ import annotations
from datetime import datetime

from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.progress import (
    Progress, SpinnerColumn, BarColumn,
    TextColumn, TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn,
)

from db_schema import init_tables
from services.data_fetcher import login, logout, fetch_realtime_quotes
from services import data_sync
from repository import stock_repo
from indicators import add_indicators, generate_signals
from stock_picker import pick_stocks
from ui.display import (
    make_picker_table, make_history_table,
    _strip_prefix, auto_prefix,
)

console = Console()


# ─────────────────────────────────────────────────────────────────────────
# Menu: stock picker
# ─────────────────────────────────────────────────────────────────────────

def menu_stock_picker():
    """Stock selection tool — local database only, no live API."""
    console.print("\n[bold cyan]=== 选股工具: 放量后缩量回调 ===[/]")

    cached_count = stock_repo.get_cached_count()
    if cached_count == 0:
        console.print("[red]本地无缓存数据！请先按 [bold]c[/bold] 进入缓存管理下载历史数据。[/]")
        Prompt.ask("\n按 Enter 返回")
        return

    console.print(f"[dim]股票池: 全部缓存股 ({cached_count} 只有数据)[/]")
    console.print("[dim]策略: 阶段放量(连续5日>1.5×20日均量) → 当前缩量(≤5日均量)[/]")

    lookback = Prompt.ask(
        "\n[dim]回溯交易日数[/]", default="60", show_default=True,
    )
    try:
        lookback = int(lookback)
        lookback = max(lookback, 10)
    except ValueError:
        lookback = 60

    config = {
        "filters": {
            "turnover": {"min": 0.5, "max": 30.0, "enabled": True},
            "amplitude": {"min": 0.5, "max": 15.0, "enabled": True},
            "price_range": {"min": 5, "max": 500, "enabled": True},
            "volume_rate": {
                "enabled": True, "min_vs_avg": 0.1, "max_vs_avg": 1.0, "days": 5,
            },
            "consecutive_volume_surge": {
                "enabled": True, "consecutive_days": 5,
                "min_volume_ratio": 1.5, "avg_window": 20,
            },
        },
        "signal_weights": {"consecutive_vol": 2.0, "volume": 0.5},
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


# ─────────────────────────────────────────────────────────────────────────
# Menu: cache manager
# ─────────────────────────────────────────────────────────────────────────

def menu_cache_manager():
    """Cache management — download/refresh A-share data into local DB."""
    console.print("\n[bold cyan]=== 股票数据缓存管理 ===[/]")

    cached_count = stock_repo.get_cached_count()
    all_stocks = stock_repo.get_all_stock_ids()
    console.print(f"[dim]stock_code 表: [bold]{len(all_stocks)}[/] 只")
    console.print(f"[dim]已缓存日线数据: [bold]{cached_count}[/] 只[/]")

    console.print("\n  [1] 初始化股票列表 (拉取全量A股代码)")
    console.print("  [2] 补全缺失 (下载还没有K线数据的股票)")
    console.print("  [3] 增量更新 (仅更新每只股票的新数据)")
    console.print("  [4] 全量刷新 (重新下载所有K线数据)")
    console.print("  [q] 返回")

    choice = Prompt.ask("\n选择", choices=["1", "2", "3", "4", "q"], default="q", show_choices=False)
    if choice == "q":
        return

    if choice == "1":
        console.print("\n[dim]正在从 baostock 获取全量A股列表...[/]")
        n = data_sync.sync_all_stock_codes()
        console.print(f"[green]完成！已写入 {n} 只股票代码[/]")
        Prompt.ask("\n按 Enter 返回")
        return

    # For choices 2/3/4, we need stock_codes populated first
    if not all_stocks:
        console.print("[yellow]stock_code 表为空，请先执行 [1] 初始化股票列表[/]")
        Prompt.ask("\n按 Enter 返回")
        return

    if choice == "2":
        console.print("\n[cyan]补全缺失的股票数据...[/]")
        n = data_sync.sync_missing()
        console.print(f"[green]完成！补全 {n} 只[/]")

    elif choice == "3":
        console.print(f"\n[cyan]增量更新 {len(all_stocks)} 只股票...[/]")
        if not Confirm.ask("确认开始?"):
            return
        stock_ids = [s["id"] for s in all_stocks]
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description:<12}"),
            BarColumn(), TaskProgressColumn(),
            TextColumn("[dim]{task.completed}/{task.total}[/]"),
            TimeElapsedColumn(), TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("增量更新...", total=len(stock_ids))

            def on_prog(code: str, done: int, total: int):
                progress.update(task, completed=done, description=f"[cyan]{_strip_prefix(code)}[/]")

            success, errors = data_sync.sync_kline_incremental(stock_ids, on_progress=on_prog)
        console.print(f"\n[green]完成！成功 {success} 只，失败 {errors} 只[/]")

    elif choice == "4":
        console.print(f"\n[yellow]全量刷新 {len(all_stocks)} 只股票，每只约0.5秒，请耐心等待...[/]")
        if not Confirm.ask("确认开始?"):
            return
        stock_ids = [s["id"] for s in all_stocks]
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description:<12}"),
            BarColumn(), TaskProgressColumn(),
            TextColumn("[dim]{task.completed}/{task.total}[/]"),
            TimeElapsedColumn(), TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("全量下载...", total=len(stock_ids))

            def on_prog(code: str, done: int, total: int):
                progress.update(task, completed=done, description=f"[cyan]{_strip_prefix(code)}[/]")

            success, errors = data_sync.sync_kline_full(stock_ids, days=500, on_progress=on_prog)
        console.print(f"\n[green]完成！成功 {success} 只，失败 {errors} 只[/]")

    Prompt.ask("\n按 Enter 返回")


# ─────────────────────────────────────────────────────────────────────────
# Menu: stock detail
# ─────────────────────────────────────────────────────────────────────────

def menu_stock_detail():
    """Single stock detail — view K-line history and indicators."""
    console.print("\n[bold cyan]=== 个股详情 ===[/]")

    while True:
        raw = Prompt.ask("输入股票代码").strip()
        if not raw:
            return

        code = auto_prefix(raw) if raw.isdigit() else raw

        # Look up stock_id
        code_map = stock_repo.get_stock_id_map()
        sid = code_map.get(code)
        if not sid:
            console.print(f"[red]未找到 {raw}，请确认代码（需先在缓存管理中下载数据）[/]\n")
            continue

        # Get stock info
        all_stocks = {s["id"]: s for s in stock_repo.get_all_stock_ids()}
        stock_info = all_stocks.get(sid, {})
        name = stock_info.get("name", code)

        # Load history
        rows = stock_repo.load_history(sid)
        if not rows:
            console.print(f"[yellow]{name} ({_strip_prefix(code)}) 暂无本地缓存数据[/]\n")
            continue

        total_days = len(rows)
        days_str = Prompt.ask(
            f"查看最近多少个交易日 (默认120, 最多{total_days})",
            default="120", show_default=True,
        )
        try:
            days = int(days_str) if days_str else 120
            days = max(min(days, total_days), 5)
        except ValueError:
            days = 120

        # Compute indicators and get latest signals
        rows_with_ind = add_indicators(rows)
        signals = generate_signals(rows_with_ind)

        display_rows = rows[-days:][::-1]  # most recent first

        # Prepend real-time quote as first row
        rt = fetch_realtime_quotes([code])
        if rt and rt[0].get("name"):
            r = rt[0]
            rt_row = {
                "date": datetime.today().strftime("%Y-%m-%d"),
                "open": r.get("open"),
                "high": r.get("high"),
                "low": r.get("low"),
                "close": r.get("close"),
                "pct_chg": r.get("pctChg"),
                "volume": r.get("volume"),
                "amount": r.get("amount"),
                "turn": 0,
            }
            display_rows.insert(0, rt_row)

        console.print(make_history_table(display_rows, code, name))

        if signals:
            console.print("\n[bold yellow]技术信号:[/]")
            for s in signals:
                console.print(f"  • {s}")

        console.print()


# ─────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────

def main():
    # Init database tables
    try:
        init_tables()
    except Exception as e:
        console.print(f"[bold red]数据库连接失败: {e}[/]")
        console.print("[yellow]请确保MySQL已启动，并检查 db_config.py 中的连接配置[/]")
        return

    login()

    while True:
        console.clear()
        console.print("\n[bold cyan]股票量化分析终端[/]")
        console.print()
        console.print("  [f] 选股工具")
        console.print("  [g] 个股详情")
        console.print("  [c] 缓存管理 (初始化/补全/增量/全量)")
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


if __name__ == "__main__":
    main()
