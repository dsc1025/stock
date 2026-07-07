"""
Simulated trading engine: portfolio management, order execution, P&L tracking.
All prices use historical close prices from baostock (end-of-day simulation).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import db_manager


@dataclass
class Position:
    code: str
    name: str
    shares: int
    avg_cost: float
    buy_date: str

    @property
    def total_cost(self) -> float:
        return self.shares * self.avg_cost


@dataclass
class Order:
    order_id: int
    code: str
    side: str          # "buy" | "sell"
    shares: int
    price: float
    amount: float
    timestamp: str
    status: str = "filled"
    note: str = ""


class Portfolio:
    COMMISSION_RATE = 0.0003   # 万3
    STAMP_TAX_RATE = 0.001     # 千1 (sell only)
    MIN_COMMISSION = 5.0

    def __init__(self, initial_cash: float = 1_000_000.0):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.positions: dict[str, Position] = {}
        self.orders: list[Order] = []
        self._order_seq = 1

    # ── order execution ──────────────────────────────────────────

    def buy(self, code: str, name: str, shares: int, price: float, note: str = "") -> tuple[bool, str]:
        if shares <= 0 or shares % 100 != 0:
            return False, "买入数量必须是 100 的整数倍"
        if price <= 0:
            return False, "价格无效"

        amount = shares * price
        commission = max(amount * self.COMMISSION_RATE, self.MIN_COMMISSION)
        total_cost = amount + commission

        if total_cost > self.cash:
            return False, f"资金不足 (需要 ¥{total_cost:,.2f}, 可用 ¥{self.cash:,.2f})"

        self.cash -= total_cost

        if code in self.positions:
            pos = self.positions[code]
            new_shares = pos.shares + shares
            pos.avg_cost = (pos.shares * pos.avg_cost + amount) / new_shares
            pos.shares = new_shares
        else:
            self.positions[code] = Position(
                code=code,
                name=name,
                shares=shares,
                avg_cost=price,
                buy_date=datetime.now().strftime("%Y-%m-%d"),
            )

        order = Order(
            order_id=self._order_seq,
            code=code,
            side="buy",
            shares=shares,
            price=price,
            amount=amount,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            note=note,
        )
        self.orders.append(order)
        self._order_seq += 1
        self._save()
        return True, f"买入成功: {code} {shares}股 @ ¥{price:.2f}, 手续费 ¥{commission:.2f}"

    def sell(self, code: str, shares: int, price: float, note: str = "") -> tuple[bool, str]:
        if code not in self.positions:
            return False, f"未持有 {code}"
        pos = self.positions[code]
        if shares <= 0 or shares % 100 != 0:
            return False, "卖出数量必须是 100 的整数倍"
        if shares > pos.shares:
            return False, f"持仓不足 (持有 {pos.shares}股, 卖出 {shares}股)"

        amount = shares * price
        commission = max(amount * self.COMMISSION_RATE, self.MIN_COMMISSION)
        stamp = amount * self.STAMP_TAX_RATE
        net = amount - commission - stamp

        self.cash += net
        pos.shares -= shares
        if pos.shares == 0:
            del self.positions[code]

        order = Order(
            order_id=self._order_seq,
            code=code,
            side="sell",
            shares=shares,
            price=price,
            amount=amount,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            note=note,
        )
        self.orders.append(order)
        self._order_seq += 1
        self._save()
        return True, f"卖出成功: {code} {shares}股 @ ¥{price:.2f}, 手续费+印花税 ¥{commission+stamp:.2f}"

    # ── metrics ──────────────────────────────────────────────────

    def market_value(self, prices: dict[str, float]) -> float:
        return sum(
            pos.shares * prices.get(code, pos.avg_cost)
            for code, pos in self.positions.items()
        )

    def total_assets(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def pnl(self, prices: dict[str, float]) -> float:
        return self.total_assets(prices) - self.initial_cash

    def pnl_pct(self, prices: dict[str, float]) -> float:
        return self.pnl(prices) / self.initial_cash * 100

    def position_pnl(self, code: str, price: float) -> tuple[float, float]:
        if code not in self.positions:
            return 0.0, 0.0
        pos = self.positions[code]
        pnl = (price - pos.avg_cost) * pos.shares
        pnl_pct = (price - pos.avg_cost) / pos.avg_cost * 100
        return pnl, pnl_pct

    def max_buyable_shares(self, price: float) -> int:
        """Return max shares (100-lot) buyable at given price."""
        if price <= 0:
            return 0
        lots = int(self.cash / (price * 100 * (1 + self.COMMISSION_RATE)))
        return lots * 100

    # ── persistence ──────────────────────────────────────────────

    def _save(self):
        data = {
            "cash": self.cash,
            "initial_cash": self.initial_cash,
            "order_seq": self._order_seq,
            "positions": {
                code: {
                    "code": pos.code,
                    "name": pos.name,
                    "shares": pos.shares,
                    "avg_cost": pos.avg_cost,
                    "buy_date": pos.buy_date,
                }
                for code, pos in self.positions.items()
            },
            "orders": [
                {
                    "order_id": o.order_id,
                    "code": o.code,
                    "side": o.side,
                    "shares": o.shares,
                    "price": o.price,
                    "amount": o.amount,
                    "timestamp": o.timestamp,
                    "status": o.status,
                    "note": o.note,
                }
                for o in self.orders
            ],
        }
        db_manager.save_portfolio(data)

    def load(self):
        data = db_manager.load_portfolio()
        if not data:
            return
        self.cash = data.get("cash", self.cash)
        self.initial_cash = data.get("initial_cash", self.initial_cash)
        self._order_seq = data.get("order_seq", 1)
        self.positions = {}
        for code, p in data.get("positions", {}).items():
            self.positions[code] = Position(**p)
        self.orders = []
        for o in data.get("orders", []):
            self.orders.append(Order(**o))

    def reset(self, initial_cash: float = 1_000_000.0):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.positions = {}
        self.orders = []
        self._order_seq = 1
        db_manager.save_portfolio({
            "cash": initial_cash,
            "initial_cash": initial_cash,
            "order_seq": 1,
            "positions": {},
            "orders": [],
        })
