"""Run before even thinking about live money:  python -m bot.final_check

Answers, from data, the questions in README's "Final check":
  1. Does paper match the backtest?   2. Did the kill switch fire in testing?
  3. Is the strategy validated (in-sample AND out-of-sample)?
The regime question needs judgment, so it's printed for you to answer. The script never enables live trading.
"""
from __future__ import annotations

import json

from bot.config import ROOT, load_settings
from bot.report import round_trips
from bot.store import Store

MIN_PAPER_TRADES = 20
WIN_RATE_TOLERANCE = 0.10      # paper win rate within ±10 points of backtest
AVG_TRADE_TOLERANCE_PCT = 0.5  # paper avg trade % within ±0.5 points of backtest


def main() -> None:
    s = load_settings(require_trading_keys=False)
    store = Store(s.db_path)
    checks: list[tuple[str, bool, str]] = []

    checks.append(("Strategy validated by backtest (in- and out-of-sample)", bool(s.strategy.get("validated")),
                   "run `python -m bot.backtest --write`"))

    bt_path = ROOT / "reports" / "backtest.json"
    bt = json.loads(bt_path.read_text()) if bt_path.exists() else {}
    full = bt.get("full_period_result") or {}
    trips = round_trips(store)
    n = len(trips)
    checks.append((f"At least {MIN_PAPER_TRADES} closed paper trades", n >= MIN_PAPER_TRADES, f"have {n}"))
    if n and full:
        wr = sum(t["pnl"] > 0 for t in trips) / n
        avg = sum(t["ret_pct"] for t in trips) / n
        checks.append(("Paper win rate matches backtest", abs(wr - full["win_rate"]) <= WIN_RATE_TOLERANCE,
                       f"paper {wr:.0%} vs backtest {full['win_rate']:.0%}"))
        checks.append(("Paper avg trade % matches backtest", abs(avg - full["avg_trade_pct"]) <= AVG_TRADE_TOLERANCE_PCT,
                       f"paper {avg:.2f}% vs backtest {full['avg_trade_pct']:.2f}%"))
    else:
        checks.append(("Paper vs backtest comparison", False, "need paper trades and reports/backtest.json"))

    drill = store.get("kill_switch_drill") or {}
    fired = store.query("SELECT COUNT(*) n FROM events WHERE kind='kill_switch' AND level='critical'")[0]["n"]
    checks.append(("Kill switch fired in testing and halted trading", bool(drill.get("passed")) and fired > 0,
                   f"drill={drill or 'never run'}; run `python -m bot.runner --test-kill-switch`"))
    checks.append(("Running on paper", s.paper, "ALPACA_PAPER must be true until this all passes"))

    print("FINAL CHECK\n")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    print("\n  [YOU] Which market regime would break this?  Look at by_regime / by_year in reports/backtest.json.")
    print("        Trend-following bleeds in sideways, whipsawing markets and gaps through stops on overnight shocks.")
    all_ok = all(ok for _, ok, _ in checks)
    print("\nResult:", "all automated checks pass. Read README 'WHAT COULD BLOW UP THIS ACCOUNT?' before deciding."
          if all_ok else "NOT ready for live money.")


if __name__ == "__main__":
    main()
