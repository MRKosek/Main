"""Strategy-tester dashboard: every signal, its Jev probabilities and the result.

    python -m dashboard.app        # binds to DASHBOARD_HOST:DASHBOARD_PORT (127.0.0.1:8000 by default)

Reach it from your laptop through an SSH tunnel: `ssh -L 8000:localhost:8000 you@vps`, then
open http://localhost:8000/?token=<DASHBOARD_TOKEN>.
"""
from __future__ import annotations

import hmac
import json
from html import escape

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from bot.approvals import Approvals
from bot.config import ROOT, load_settings
from bot.report import jev_stats, round_trips
from bot.risk import KillSwitch
from bot.store import Store

settings = load_settings(require_trading_keys=False)
if len(settings.dashboard_token) < 16:
    raise SystemExit("Set DASHBOARD_TOKEN in .env (at least 16 random characters).")
store = Store(settings.db_path)
kill = KillSwitch(store)
approvals = Approvals(store, settings.strategy["risk"]["approval_ttl_minutes"])
app = FastAPI(title="Trading bot dashboard", docs_url=None, redoc_url=None, openapi_url=None)
COOKIE = "bot_session"


def auth(request: Request) -> None:
    supplied = request.cookies.get(COOKIE) or request.query_params.get("token") or ""
    if not hmac.compare_digest(supplied.encode(), settings.dashboard_token.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized. Open /?token=<DASHBOARD_TOKEN>.")


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _: None = Depends(auth)):
    resp = HTMLResponse(render())
    if request.query_params.get("token"):
        resp.set_cookie(COOKIE, settings.dashboard_token, httponly=True, samesite="strict", max_age=7 * 86400)
    return resp


@app.post("/approve/{approval_id}")
def approve(approval_id: int, decision: str = Form(...), _: None = Depends(auth)):
    approvals.decide(approval_id, approve=decision == "approve", by="dashboard")
    return RedirectResponse("/", status_code=303)


@app.post("/kill")
def kill_now(_: None = Depends(auth)):
    kill.trip("manual kill from dashboard")
    return RedirectResponse("/", status_code=303)


@app.get("/api/signals")
def api_signals(_: None = Depends(auth)):
    return JSONResponse(signals_with_jev(500))


def signals_with_jev(limit: int) -> list[dict]:
    rows = store.query(
        "SELECT s.*, j.probabilities, j.gates, j.latency_ms, j.cost_usd, j.error AS jev_error "
        "FROM signals s LEFT JOIN jev_decisions j ON j.signal_id = s.id ORDER BY s.id DESC LIMIT ?", (limit,))
    for r in rows:
        r["gates"] = json.loads(r["gates"]) if r["gates"] else []
        r["probabilities"] = json.loads(r["probabilities"]) if r["probabilities"] else {}
    return rows


CSS = """
:root{--bg:#0f1115;--card:#171a21;--text:#e6e8ee;--muted:#9aa3b2;--line:#2a2f3a;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--accent:#58a6ff}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f24;--muted:#57606a;--line:#d0d7de;--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700;--accent:#0969da}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1200px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0 12px}h2{font-size:15px;margin:0 0 8px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:14px}
.stat .v{font-size:20px;font-weight:650;font-variant-numeric:tabular-nums}.stat .k{color:var(--muted);font-size:12px}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap;vertical-align:top}th{color:var(--muted);font-weight:600;font-size:12px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.muted{color:var(--muted)}.pill{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid currentColor;font-size:12px}
.gate{display:inline-block;margin-right:8px}.reason{white-space:normal;max-width:360px;color:var(--muted)}
button{font:inherit;padding:5px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--text);cursor:pointer}button.danger{border-color:var(--bad);color:var(--bad)}button.go{border-color:var(--ok);color:var(--ok)}
.banner{padding:10px 14px;border-radius:10px;margin-bottom:14px;border:1px solid var(--bad);color:var(--bad);font-weight:600}
"""

STATUS_CLASS = {"submitted": "ok", "blocked_jev": "warn", "blocked_risk": "warn", "pending_approval": "accent",
                "rejected": "muted", "expired": "muted", "error": "bad", "new": "muted"}


def money(x) -> str:
    return "–" if x is None else f"${x:,.2f}"


def render() -> str:
    acct = store.get("last_account", {}) or {}
    trips = round_trips(store)
    wins = sum(t["pnl"] > 0 for t in trips)
    js = jev_stats(store)
    bt_path = ROOT / "reports" / "backtest.json"
    bt = json.loads(bt_path.read_text()) if bt_path.exists() else None
    validated = settings.strategy.get("validated")
    e = escape

    parts = [f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<meta http-equiv=refresh content=60><title>Trading Bot</title><style>{CSS}</style></head><body><main>",
             f"<h1>Trading bot · {'PAPER' if settings.paper else '<span class=bad>LIVE</span>'} "
             f"<span class='pill {'ok' if validated else 'warn'}'>{'strategy validated' if validated else 'strategy NOT validated'}</span></h1>"]
    if kill.tripped:
        parts.append(f"<div class=banner>🛑 Kill switch active: {e(kill.reason)}. Send /resume on Telegram to re-enable.</div>")

    day = acct.get("equity", 0) - acct.get("last_equity", 0) if acct else None
    stats = [("Equity", money(acct.get("equity"))), ("Today", money(day)),
             ("Closed trades", str(len(trips))), ("Win rate", f"{wins / len(trips):.0%}" if trips else "–"),
             ("Realized P&L", money(sum(t["pnl"] for t in trips))),
             ("Largest loss", money(min((t["pnl"] for t in trips), default=0))),
             ("Jev avg latency", f"{js['avg_latency_ms']} ms"), ("Jev cost / decision", f"${js['avg_cost_usd']:.6f}")]
    parts.append("<div class=grid>" + "".join(
        f"<div class='card stat'><div class=k>{k}</div><div class=v>{v}</div></div>" for k, v in stats) + "</div>")

    pend = approvals.pending()
    parts.append("<div class=card><h2>Pending approvals</h2>")
    if pend:
        parts.append("<div class=scroll><table><tr><th>#</th><th>Symbol</th><th>Qty</th><th>Notional</th><th>Stop</th><th>Target</th><th>Expires (UTC)</th><th></th></tr>")
        for a in pend:
            parts.append(
                f"<tr><td>{a['id']}</td><td>{e(a['symbol'])}</td><td>{a['qty']}</td><td>{money(a['notional'])}</td>"
                f"<td>{a['stop']}</td><td>{a['take_profit']}</td><td>{e(a['expires'][11:16])}</td><td>"
                f"<form method=post action='/approve/{a['id']}' style='display:inline'>"
                f"<button class=go name=decision value=approve>Approve</button> "
                f"<button name=decision value=reject>Reject</button></form></td></tr>")
        parts.append("</table></div>")
    else:
        parts.append("<p class=muted>None.</p>")
    parts.append("<form method=post action='/kill' onsubmit=\"return confirm('Halt ALL trading and flatten positions?')\">"
                 "<button class=danger>Kill switch: halt all trading</button></form></div>")

    parts.append("<div class=card><h2>Signals</h2><div class=scroll><table><tr><th>Time (UTC)</th><th>Symbol</th>"
                 "<th>Price</th><th>Jev probabilities vs thresholds</th><th>Latency</th><th>Status</th><th>Result</th><th>Reason</th></tr>")
    for s in signals_with_jev(200):
        gates = "".join(
            f"<span class='gate {'ok' if g['passed'] else 'bad'}'>{e(g['question'])} {e(str(g['value']))} {e(g['op'])} {g['threshold']}</span>"
            for g in s["gates"]) or (f"<span class=bad>{e(s['jev_error'] or '')}</span>" if s.get("jev_error") else "<span class=muted>–</span>")
        pnl = s.get("realized_pnl")
        res = "<span class=muted>–</span>" if pnl is None else f"<span class={'ok' if pnl > 0 else 'bad'}>{money(pnl)}</span>"
        cls = STATUS_CLASS.get(s["status"], "muted")
        parts.append(
            f"<tr><td>{e(s['ts'][:16].replace('T', ' '))}</td><td>{e(s['symbol'])}</td><td>{money(s['price'])}</td>"
            f"<td>{gates}</td><td>{s['latency_ms'] or '–'}{' ms' if s['latency_ms'] else ''}</td>"
            f"<td><span class='pill {cls}'>{e(s['status'])}</span></td><td>{res}</td>"
            f"<td class=reason>{e(s['reason'] or '')}</td></tr>")
    parts.append("</table></div></div>")

    if bt and bt.get("winner"):
        w, o, f = bt["winner"], bt["out_of_sample_result"], bt["full_period_result"]
        parts.append("<div class=card><h2>Backtest</h2><div class=scroll><table><tr><th>Period</th><th>Return</th><th>CAGR</th>"
                     "<th>Max DD</th><th>Win rate</th><th>Profit factor</th><th>Trades</th></tr>")
        for label, r in [(f"In-sample {bt['in_sample'][0]}→{bt['in_sample'][1]}", w),
                         (f"Out-of-sample →{bt['out_of_sample'][1]}", o), ("Full period", f)]:
            parts.append(f"<tr><td>{e(label)}</td><td>{r['total_return_pct']}%</td><td>{r['cagr_pct']}%</td>"
                         f"<td>{r['max_drawdown_pct']}%</td><td>{r['win_rate']:.0%}</td><td>{r['profit_factor']}</td><td>{r['trades']}</td></tr>")
        parts.append(f"</table></div><p class=muted>Out-of-sample: {'passed' if bt['out_of_sample_passed'] else 'FAILED'} · "
                     f"by regime: {e(json.dumps(f.get('by_regime', {})))}</p></div>")
    elif bt:
        parts.append("<div class=card><h2>Backtest</h2><p class=warn>No parameter set survived the filters.</p></div>")

    events = store.query("SELECT * FROM events ORDER BY id DESC LIMIT 30")
    parts.append("<div class=card><h2>Events</h2><div class=scroll><table>")
    for ev in events:
        cls = {"critical": "bad", "error": "bad", "warning": "warn"}.get(ev["level"], "muted")
        parts.append(f"<tr><td>{e(ev['ts'][:16].replace('T', ' '))}</td><td class={cls}>{e(ev['kind'])}</td>"
                     f"<td class=reason>{e(ev['message'])}</td></tr>")
    parts.append("</table></div></div></main></body></html>")
    return "".join(parts)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.dashboard_host, port=settings.dashboard_port)
