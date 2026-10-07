"""Round-trip P&L (FIFO over fills) and the daily report."""
from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import date

from bot.config import ROOT
from bot.store import Store


def round_trips(store: Store) -> list[dict]:
    """Match sells against earlier buys FIFO. Each closed lot carries the signal id of its opening order."""
    fills = store.query("SELECT * FROM fills ORDER BY ts, id")
    lots: dict[str, deque] = defaultdict(deque)
    trips = []
    for f in fills:
        if f["side"] == "buy":
            sig = None
            cid = f["client_order_id"] or ""
            if cid.startswith("sig-"):
                sig = int(cid.split("-")[1])
            lots[f["symbol"]].append({"qty": f["qty"], "price": f["price"], "ts": f["ts"], "signal_id": sig})
            continue
        remaining = f["qty"]
        while remaining > 1e-9 and lots[f["symbol"]]:
            lot = lots[f["symbol"]][0]
            q = min(lot["qty"], remaining)
            trips.append({
                "symbol": f["symbol"], "signal_id": lot["signal_id"], "qty": q,
                "entry": lot["price"], "exit": f["price"], "opened": lot["ts"], "closed": f["ts"],
                "pnl": round((f["price"] - lot["price"]) * q, 2),
                "ret_pct": round((f["price"] / lot["price"] - 1) * 100, 3),
            })
            lot["qty"] -= q
            remaining -= q
            if lot["qty"] <= 1e-9:
                lots[f["symbol"]].popleft()
    return trips


def attribute_pnl(store: Store) -> None:
    """Write each signal's realized P&L back onto the signal row for the dashboard."""
    per_signal: dict[int, float] = defaultdict(float)
    for t in round_trips(store):
        if t["signal_id"] is not None:
            per_signal[t["signal_id"]] += t["pnl"]
    for sid, pnl in per_signal.items():
        store.update_signal(sid, realized_pnl=round(pnl, 2))


def jev_stats(store: Store, since: str | None = None) -> dict:
    where, params = ("WHERE ts >= ?", (since,)) if since else ("", ())
    r = store.query(
        f"SELECT COUNT(*) n, AVG(latency_ms) lat, AVG(cost_usd) cost, SUM(cost_usd) total, "
        f"SUM(passed) passed, SUM(error IS NOT NULL) errors FROM jev_decisions {where}", params)[0]
    return {"decisions": r["n"] or 0, "avg_latency_ms": round(r["lat"] or 0, 1),
            "avg_cost_usd": r["cost"] or 0.0, "total_cost_usd": r["total"] or 0.0,
            "passed": r["passed"] or 0, "errors": r["errors"] or 0}


def daily_report(store: Store, account: dict | None, day: date | None = None) -> dict:
    day = day or date.today()
    d = day.isoformat()
    trips = round_trips(store)
    today = [t for t in trips if t["closed"][:10] == d]
    fills_today = store.query("SELECT * FROM fills WHERE substr(ts,1,10)=?", (d,))
    wins = [t for t in trips if t["pnl"] > 0]
    rep = {
        "date": d,
        "trades_today": len(fills_today),
        "closed_round_trips_today": len(today),
        "realized_pnl_today": round(sum(t["pnl"] for t in today), 2),
        "account_change_today": round(account["equity"] - account["last_equity"], 2) if account else None,
        "equity": account["equity"] if account else None,
        "cumulative_round_trips": len(trips),
        "cumulative_realized_pnl": round(sum(t["pnl"] for t in trips), 2),
        "win_rate": round(len(wins) / len(trips), 3) if trips else None,
        "avg_trade_pct": round(sum(t["ret_pct"] for t in trips) / len(trips), 3) if trips else None,
        "largest_loss": min((t["pnl"] for t in trips), default=0.0),
        "signals_today": store.query("SELECT status, COUNT(*) n FROM signals WHERE substr(ts,1,10)=? GROUP BY status", (d,)),
        "jev_today": jev_stats(store, since=d),
        "jev_all_time": jev_stats(store),
    }
    out = ROOT / "reports" / f"daily-{d}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rep, indent=2))
    return rep


def format_report(rep: dict) -> str:
    j = rep["jev_today"]
    wr = f"{rep['win_rate']:.0%}" if rep["win_rate"] is not None else "n/a"
    acct = f"${rep['account_change_today']:+,.2f}" if rep["account_change_today"] is not None else "n/a"
    lines = [
        f"📊 Daily report {rep['date']}",
        f"Fills today: {rep['trades_today']} · closed trades: {rep['closed_round_trips_today']}",
        f"Realized P&L today: ${rep['realized_pnl_today']:+,.2f} · account change: {acct}",
        f"Cumulative: {rep['cumulative_round_trips']} trades, ${rep['cumulative_realized_pnl']:+,.2f}, win rate {wr}",
        f"Largest loss: ${rep['largest_loss']:,.2f}",
        f"Jev: {j['decisions']} decisions, avg {j['avg_latency_ms']} ms, "
        f"avg ${j['avg_cost_usd']:.6f}/decision, {j['errors']} errors",
    ]
    return "\n".join(lines)
