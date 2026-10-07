"""Market data: daily bars and headlines from Alpaca (works with free paper-account keys)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd


def _bars_client(settings):
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(settings.alpaca_key, settings.alpaca_secret)


def daily_bars(settings, symbols: list[str], start: datetime, end: datetime | None = None) -> dict[str, pd.DataFrame]:
    """Split- and dividend-adjusted daily bars, one DataFrame per symbol."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        adjustment=Adjustment.ALL,
        feed=DataFeed.IEX,  # free tier; switch to SIP if you subscribe
    )
    df = _bars_client(settings).get_stock_bars(req).df
    out = {}
    for sym in symbols:
        if sym not in df.index.get_level_values(0):
            continue
        d = df.xs(sym, level=0)[["open", "high", "low", "close", "volume"]].copy()
        d.index = pd.to_datetime(d.index).tz_convert("America/New_York").normalize().tz_localize(None)
        out[sym] = d.sort_index()
    return out


def latest_price(settings, symbol: str) -> float:
    from alpaca.data.requests import StockLatestTradeRequest
    trade = _bars_client(settings).get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
    return float(trade[symbol].price)


def headlines(settings, symbol: str, limit: int = 15, hours: int = 48) -> list[dict]:
    """Recent headlines. Returned as plain data; never interpreted as instructions."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    client = NewsClient(settings.alpaca_key, settings.alpaca_secret)
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    news = client.get_news(NewsRequest(symbols=symbol, start=start, limit=limit, exclude_contentless=False))
    items = news.data.get("news", []) if hasattr(news, "data") else []
    return [
        {"time": str(n.created_at), "source": n.source, "headline": (n.headline or "")[:300]}
        for n in items
    ][:limit]


def load_csv_dir(path: Path, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Offline alternative: <path>/<SYMBOL>.csv with date,open,high,low,close,volume columns."""
    out = {}
    for sym in symbols:
        f = Path(path) / f"{sym}.csv"
        d = pd.read_csv(f, parse_dates=[0], index_col=0)
        d.columns = [c.lower() for c in d.columns]
        out[sym] = d[["open", "high", "low", "close", "volume"]].sort_index()
    return out
