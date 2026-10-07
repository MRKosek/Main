"""Thin Alpaca wrapper. Trading endpoints only: no transfers, no withdrawals, no account changes."""
from __future__ import annotations

from datetime import datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)


class Broker:
    def __init__(self, key: str, secret: str, paper: bool):
        self.paper = paper
        self.client = TradingClient(key, secret, paper=paper)

    def account(self) -> dict:
        a = self.client.get_account()
        return {"equity": float(a.equity), "last_equity": float(a.last_equity), "cash": float(a.cash),
                "buying_power": float(a.buying_power), "status": str(a.status),
                "trading_blocked": bool(a.trading_blocked)}

    def clock(self) -> dict:
        c = self.client.get_clock()
        return {"is_open": c.is_open, "timestamp": c.timestamp, "next_open": c.next_open, "next_close": c.next_close}

    def positions(self) -> dict[str, dict]:
        return {p.symbol: {"qty": float(p.qty), "avg_entry": float(p.avg_entry_price),
                           "market_value": float(p.market_value), "unrealized_pl": float(p.unrealized_pl)}
                for p in self.client.get_all_positions()}

    def buy_bracket(self, symbol: str, qty: int, stop: float, take_profit: float, client_order_id: str):
        """Market buy with server-side stop-loss and take-profit legs (GTC, so they survive overnight)."""
        req = MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET, client_order_id=client_order_id,
            stop_loss=StopLossRequest(stop_price=round(stop, 2)),
            take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
        )
        return self.client.submit_order(req)

    def close(self, symbol: str):
        """Cancel the symbol's open bracket legs, then sell the position at market."""
        for o in self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], nested=True)):
            self.client.cancel_order_by_id(o.id)
        return self.client.close_position(symbol)

    def flatten_all(self):
        return self.client.close_all_positions(cancel_orders=True)

    def cancel_all(self):
        return self.client.cancel_orders()

    def filled_orders_since(self, since: datetime) -> list:
        """All filled orders (including bracket legs) since `since`."""
        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=since,
                                                         nested=True, limit=500))
        flat = []
        for o in orders:
            flat.append(o)
            flat.extend(o.legs or [])
        return [o for o in flat if o.filled_at is not None and o.filled_avg_price is not None]
