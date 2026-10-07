"""Momentum / trend-following rules shared by the backtester and the live runner.

Bars are a DataFrame indexed by date with columns open, high, low, close, volume.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def add_indicators(bars: pd.DataFrame, p: dict) -> pd.DataFrame:
    df = bars.copy()
    c = df["close"]
    df["sma_fast"] = c.rolling(p["fast"]).mean()
    df["sma_slow"] = c.rolling(p["slow"]).mean()
    df["sma_trend"] = c.rolling(p["trend_sma"]).mean()
    df["sma_exit"] = c.rolling(p["exit_sma"]).mean()
    # Highest close of the PRIOR n days, so today's close can break above it.
    df["prior_high"] = c.shift(1).rolling(p["breakout_lookback"]).max()
    prev_close = c.shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(p["atr_period"]).mean()
    df["sma200"] = c.rolling(200).mean()
    return df


def entry_signal(df: pd.DataFrame, p: dict) -> pd.Series:
    """True on bars whose close produces a long entry signal."""
    uptrend = df["close"] > df["sma_trend"]
    if p["family"] == "breakout":
        trigger = df["close"] >= df["prior_high"]
    elif p["family"] == "sma_cross":
        above = df["sma_fast"] > df["sma_slow"]
        trigger = above & ~above.shift(1, fill_value=False)
    else:
        raise ValueError(f"Unknown strategy family {p['family']!r}")
    valid = df[["sma_trend", "atr", "prior_high", "sma_slow"]].notna().all(axis=1)
    return (trigger & uptrend & valid).fillna(False)


def exit_signal(df: pd.DataFrame) -> pd.Series:
    return (df["close"] < df["sma_exit"]).fillna(False)


def regime(df: pd.DataFrame) -> pd.Series:
    """bull: above a rising SMA200. bear: below a falling SMA200. Otherwise sideways."""
    slope = df["sma200"] - df["sma200"].shift(20)
    above = df["close"] > df["sma200"]
    out = np.where(above & (slope > 0), "bull", np.where(~above & (slope < 0), "bear", "sideways"))
    out = pd.Series(out, index=df.index)
    out[df["sma200"].isna()] = "unknown"
    return out


@dataclass
class EntryPlan:
    symbol: str
    price: float
    stop: float
    take_profit: float
    qty: int
    notional: float
    atr: float


def size_entry(symbol: str, price: float, atr: float, p: dict, max_position_usd: float) -> EntryPlan | None:
    """Fixed-notional sizing: whole shares up to max_position_usd. None if less than one share fits."""
    if price <= 0 or atr <= 0:
        return None
    qty = int(max_position_usd // price)
    if qty < 1:
        return None
    stop = round(price - p["stop_atr"] * atr, 2)
    take_profit = round(price + p["take_profit_atr"] * atr, 2)
    return EntryPlan(symbol, price, stop, take_profit, qty, round(qty * price, 2), atr)
