"""Backtest the momentum rules, grid-search parameters, filter survivors, check out-of-sample.

    python -m bot.backtest                 # report only
    python -m bot.backtest --write         # also write the winner into strategy.md
    python -m bot.backtest --csv data/csv  # offline, from CSV files instead of Alpaca

Fills: entries and trend exits at the signal day's close (live evaluates at 15:45 and submits
market orders then). Stops and take-profits fill intrabar; on a gap, at the open. If one bar
touches both the stop and the target, the stop is assumed (pessimistic). Slippage on every fill.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from bot.config import ROOT, load_strategy_config, write_strategy_section
from bot.strategy import add_indicators, entry_signal, exit_signal, regime


@dataclass
class Trade:
    symbol: str
    entry_date: str
    exit_date: str
    entry: float
    exit: float
    qty: float
    pnl: float
    ret_pct: float
    reason: str
    regime: str


@dataclass
class Result:
    params: dict
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    sharpe: float
    trades: int
    avg_trade_pct: float
    exposure_pct: float
    by_regime: dict = field(default_factory=dict)
    by_year: dict = field(default_factory=dict)
    trade_log: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)


def run_backtest(bars: dict[str, pd.DataFrame], p: dict, bt: dict, max_open: int,
                 start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> Result:
    """Portfolio simulation over all symbols. Indicators use full history; trading is limited to [start, end]."""
    frames = {}
    for sym, b in bars.items():
        df = add_indicators(b, p)
        df["entry"] = entry_signal(df, p)
        df["exit"] = exit_signal(df)
        df["regime"] = regime(df)
        frames[sym] = df
    dates = sorted(set().union(*[f.index for f in frames.values()]))
    closes = {sym: f["close"].reindex(dates).ffill() for sym, f in frames.items()}
    dates = [d for d in dates if (start is None or d >= start) and (end is None or d <= end)]

    slip = bt["slippage_bps"] / 10_000
    commission = bt["commission_per_trade"]
    cash = float(bt["initial_equity"])
    positions: dict[str, dict] = {}
    trades: list[Trade] = []
    curve: list[tuple[str, float]] = []
    invested_days = 0

    def close_pos(sym: str, date, raw_price: float, reason: str) -> None:
        nonlocal cash
        pos = positions.pop(sym)
        price = raw_price * (1 - slip)
        cash += pos["qty"] * price - commission
        pnl = (price - pos["entry"]) * pos["qty"] - 2 * commission
        trades.append(Trade(sym, str(pos["date"].date()), str(date.date()), round(pos["entry"], 4),
                            round(price, 4), pos["qty"], round(pnl, 2),
                            round((price / pos["entry"] - 1) * 100, 3), reason, pos["regime"]))

    for date in dates:
        # 1) stops / targets on open positions, using today's range
        for sym in list(positions):
            f = frames[sym]
            if date not in f.index:
                continue
            bar, pos = f.loc[date], positions[sym]
            if bar["low"] <= pos["stop"]:
                close_pos(sym, date, min(bar["open"], pos["stop"]), "stop")
            elif bar["high"] >= pos["tp"]:
                close_pos(sym, date, max(bar["open"], pos["tp"]), "take_profit")

        # 2) trend exits at the close
        for sym in list(positions):
            f = frames[sym]
            if date in f.index and f.at[date, "exit"]:
                close_pos(sym, date, f.at[date, "close"], "trend_exit")

        # mark to market
        equity = cash + sum(
            pos["qty"] * closes[s].at[date] for s, pos in positions.items()
        )

        # 3) entries at the close
        for sym, f in frames.items():
            if sym in positions or len(positions) >= max_open or date not in f.index:
                continue
            row = f.loc[date]
            if not row["entry"]:
                continue
            price = row["close"] * (1 + slip)
            notional = min(equity * bt["position_pct"] / 100, cash - commission)
            if notional <= 0:
                continue
            qty = notional / price
            cash -= qty * price + commission
            positions[sym] = {
                "qty": qty, "entry": price, "date": date, "regime": row["regime"],
                "stop": row["close"] - p["stop_atr"] * row["atr"],
                "tp": row["close"] + p["take_profit_atr"] * row["atr"],
            }

        if positions:
            invested_days += 1
        equity = cash + sum(pos["qty"] * closes[s].at[date] for s, pos in positions.items())
        curve.append((str(date.date()), equity))

    # close anything still open at the last price so it counts
    if dates:
        last = dates[-1]
        for sym in list(positions):
            close_pos(sym, last, closes[sym].at[last], "end_of_test")

    return _metrics(p, bt, trades, curve, invested_days, len(dates))


def _metrics(p, bt, trades: list[Trade], curve, invested_days: int, n_days: int) -> Result:
    eq = pd.Series([v for _, v in curve], index=pd.to_datetime([d for d, _ in curve])) if curve else pd.Series(dtype=float)
    init = float(bt["initial_equity"])
    final = init + sum(t.pnl for t in trades)
    total_ret = (final / init - 1) * 100
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9) if len(eq) > 1 else 1e-9
    cagr = ((final / init) ** (1 / years) - 1) * 100 if final > 0 and len(eq) > 1 else -100.0
    max_dd = float(((eq / eq.cummax()) - 1).min() * -100) if len(eq) else 0.0
    daily = eq.pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0.0
    wins = [t for t in trades if t.pnl > 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in trades if t.pnl <= 0)

    def group(key) -> dict:
        out = {}
        for k, ts in itertools.groupby(sorted(trades, key=key), key=key):
            ts = list(ts)
            out[k] = {"trades": len(ts), "pnl": round(float(sum(t.pnl for t in ts)), 2),
                      "win_rate": round(float(sum(t.pnl > 0 for t in ts) / len(ts)), 3)}
        return out

    return Result(
        params=p,
        total_return_pct=round(total_ret, 2),
        cagr_pct=round(cagr, 2),
        max_drawdown_pct=round(max_dd, 2),
        win_rate=round(len(wins) / len(trades), 3) if trades else 0.0,
        profit_factor=round(gross_win / gross_loss, 2) if gross_loss > 0 else (float("inf") if gross_win else 0.0),
        sharpe=round(sharpe, 2),
        trades=len(trades),
        avg_trade_pct=round(float(np.mean([t.ret_pct for t in trades])), 3) if trades else 0.0,
        exposure_pct=round(invested_days / n_days * 100, 1) if n_days else 0.0,
        by_regime=group(lambda t: t.regime),
        by_year=group(lambda t: t.exit_date[:4]),
        trade_log=[asdict(t) for t in trades],
        equity_curve=[(d, round(v, 2)) for d, v in curve],
    )


def survives(r: Result, bt: dict) -> tuple[bool, str]:
    if r.trades < bt["min_trades"]:
        return False, f"only {r.trades} trades"
    if r.max_drawdown_pct > bt["max_drawdown_pct"]:
        return False, f"drawdown {r.max_drawdown_pct}% > {bt['max_drawdown_pct']}%"
    if r.win_rate < bt["min_win_rate"]:
        return False, f"win rate {r.win_rate:.0%} < {bt['min_win_rate']:.0%}"
    if r.total_return_pct <= 0:
        return False, "lost money"
    losing_regimes = [k for k, v in r.by_regime.items() if v["pnl"] < 0 and k != "unknown"]
    if len(losing_regimes) > 1:
        return False, f"lost money in regimes {losing_regimes}"
    return True, "ok"


def score(r: Result) -> float:
    """Return per unit of drawdown, not raw return."""
    return r.cagr_pct / max(r.max_drawdown_pct, 1.0)


def param_grid(base: dict) -> list[dict]:
    grid = []
    common = itertools.product([150, 200], [20, 50], [1.5, 2.0, 3.0], [3.0, 4.0, 6.0])
    for trend, exit_sma, stop_atr, tp_atr in common:
        for lookback in [20, 55]:
            grid.append({**base, "family": "breakout", "breakout_lookback": lookback, "trend_sma": trend,
                         "exit_sma": exit_sma, "stop_atr": stop_atr, "take_profit_atr": tp_atr})
        for fast, slow in [(10, 30), (20, 50), (50, 100)]:
            grid.append({**base, "family": "sma_cross", "fast": fast, "slow": slow, "trend_sma": trend,
                         "exit_sma": exit_sma, "stop_atr": stop_atr, "take_profit_atr": tp_atr})
    return grid


def optimize(bars: dict[str, pd.DataFrame], cfg: dict) -> dict:
    base, bt, max_open = cfg["strategy"], cfg["backtest"], cfg["risk"]["max_open_positions"]
    all_dates = sorted(set().union(*[b.index for b in bars.values()]))
    start = pd.Timestamp(bt["start"])
    # leave room for the longest indicator to warm up before trading starts
    tradable = [d for d in all_dates if d >= start]
    if len(tradable) < 2 * 252:
        raise SystemExit(f"Need at least 2 years of data after {bt['start']}, got {len(tradable)} days.")
    split = tradable[int(len(tradable) * bt["in_sample_frac"])]

    rows = []
    for p in param_grid(base):
        r = run_backtest(bars, p, bt, max_open, start=tradable[0], end=split)
        ok, why = survives(r, bt)
        rows.append((r, ok, why))
    survivors = sorted([r for r, ok, _ in rows if ok], key=score, reverse=True)

    report = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "symbols": list(bars),
        "in_sample": [str(tradable[0].date()), str(split.date())],
        "out_of_sample": [str(split.date()), str(tradable[-1].date())],
        "tested": len(rows),
        "survivors": len(survivors),
        "rejections": _count_reasons(rows),
        "winner": None,
        "out_of_sample_result": None,
        "out_of_sample_passed": False,
        "full_period_result": None,
    }
    if not survivors:
        return report
    best = survivors[0]
    oos = run_backtest(bars, best.params, bt, max_open, start=split, end=tradable[-1])
    # Out-of-sample is shorter, so scale the trade-count floor to its length.
    oos_bt = {**bt, "min_trades": max(3, int(bt["min_trades"] * (1 - bt["in_sample_frac"]) / bt["in_sample_frac"]))}
    oos_ok, oos_why = survives(oos, oos_bt)
    full = run_backtest(bars, best.params, bt, max_open, start=tradable[0], end=tradable[-1])
    report.update({
        "winner": _summary(best),
        "top5": [_summary(r) for r in survivors[:5]],
        "out_of_sample_result": _summary(oos),
        "out_of_sample_passed": oos_ok,
        "out_of_sample_reason": oos_why,
        "full_period_result": {**_summary(full), "by_regime": full.by_regime, "by_year": full.by_year},
        "full_period_trades": full.trade_log,
        "full_period_equity": full.equity_curve,
    })
    return report


def _summary(r: Result) -> dict:
    d = asdict(r)
    d.pop("trade_log")
    d.pop("equity_curve")
    return d


def _count_reasons(rows) -> dict:
    out: dict[str, int] = {}
    for _, ok, why in rows:
        if not ok:
            key = why.split(" ")[0]
            out[key] = out.get(key, 0) + 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="write the winner into strategy.md if it passes out-of-sample")
    ap.add_argument("--csv", help="directory of <SYMBOL>.csv files instead of Alpaca")
    args = ap.parse_args()

    cfg = load_strategy_config()
    symbols = cfg["strategy"]["symbols"]
    warmup_start = pd.Timestamp(cfg["backtest"]["start"]) - pd.Timedelta(days=400)
    if args.csv:
        from bot.data import load_csv_dir
        bars = load_csv_dir(args.csv, symbols)
    else:
        from bot.config import load_settings
        from bot.data import daily_bars
        settings = load_settings()
        bars = daily_bars(settings, symbols, warmup_start.to_pydatetime())

    report = optimize(bars, cfg)
    out = ROOT / "reports" / "backtest.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print_report(report)

    if args.write:
        if report["winner"] and report["out_of_sample_passed"]:
            write_strategy_section(report["winner"]["params"], validated=True)
            print("\nWinner written to strategy.md (validated=true). Restart the bot to pick it up.")
        else:
            print("\nNothing written: no strategy survived both in-sample and out-of-sample filters.")


def print_report(rep: dict) -> None:
    print(f"Tested {rep['tested']} parameter sets on {rep['symbols']}")
    print(f"In-sample {rep['in_sample'][0]} → {rep['in_sample'][1]}   out-of-sample → {rep['out_of_sample'][1]}")
    print(f"Survivors: {rep['survivors']}   rejections: {rep['rejections']}")
    if not rep["winner"]:
        print("No survivors. Loosen nothing blindly. Revisit the idea instead.")
        return
    keys = ["total_return_pct", "cagr_pct", "max_drawdown_pct", "win_rate", "profit_factor", "sharpe", "trades", "exposure_pct"]
    for label, r in [("IN-SAMPLE winner", rep["winner"]), ("OUT-OF-SAMPLE", rep["out_of_sample_result"]),
                     ("FULL PERIOD", rep["full_period_result"])]:
        print(f"\n{label}: " + "  ".join(f"{k}={r[k]}" for k in keys))
    w = rep["winner"]["params"]
    print("\nWinner params:", {k: w[k] for k in ["family", "breakout_lookback", "fast", "slow", "trend_sma",
                                                "exit_sma", "stop_atr", "take_profit_atr"]})
    print("By regime (full):", rep["full_period_result"]["by_regime"])
    print("Out-of-sample:", "PASSED" if rep["out_of_sample_passed"] else f"FAILED ({rep.get('out_of_sample_reason')})")


if __name__ == "__main__":
    main()
