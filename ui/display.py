"""Display utilities: Rich table rendering for stock data."""
from __future__ import annotations

from rich.table import Table
from rich.text import Text
from rich import box


def color_pct(pct: float) -> str:
    """Rich-styled percentage string: red for positive, green for negative."""
    if pct > 0:
        return f"[bold red]+{pct:.2f}%[/]"
    elif pct < 0:
        return f"[bold green]{pct:.2f}%[/]"
    return f"{pct:.2f}%"


def _strip_prefix(code: str) -> str:
    """Strip exchange prefix: 'sh.600519' → '600519'."""
    for pfx in ("sh.", "sz.", "bj."):
        if code.startswith(pfx):
            return code[len(pfx):]
    return code


def auto_prefix(num: str) -> str:
    """Infer exchange prefix from numeric code: '600519' → 'sh.600519'."""
    num = num.strip()
    return f"sh.{num}" if num[:1] in ("6", "9", "5") else f"sz.{num}"


def make_picker_table(candidates: list[dict]) -> Table:
    """Render stock picker results as a Rich table."""
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
    t.add_column("评分", justify="right", width=7)

    for c in candidates:
        t.add_row(
            _strip_prefix(c["code"]),
            c["name"],
            f"¥{c['price']:.2f}",
            Text.from_markup(color_pct(c["pct_change"])),
            f"{int(c.get('volume', 0) / 100):,}",
            f"{c['turnover']:.1f}%",
            f"{c['amplitude']:.1f}%",
            f"{c['score']:.1f}",
        )

    return t


def make_history_table(rows: list[dict], code: str, name: str) -> Table:
    """Render single-stock K-line history as a Rich table."""
    t = Table(
        title=f"[bold]{name}[/] [dim]({_strip_prefix(code)}) 最近 {len(rows)} 个交易日[/]",
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

    for r in rows:
        pct = r.get("pct_chg", 0) or 0
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
        )

    return t
