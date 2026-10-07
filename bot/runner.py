"""The 24/7 loop: watch risk every minute, evaluate signals once a day, execute, alert, report.

    python -m bot.runner           # run forever
    python -m bot.runner --once    # evaluate signals right now (market must be open) and exit
    python -m bot.runner --test-kill-switch   # trip the kill switch on paper and verify it halts trading
"""
from __future__ import annotations

import argparse
import logging
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from bot.approvals import Approvals
from bot.broker import Broker
from bot.config import load_settings
from bot.data import daily_bars, headlines, latest_price
from bot.jev_layer import JevGate, build_state
from bot.notify import Telegram
from bot.report import attribute_pnl, daily_report, format_report
from bot.risk import KillSwitch, RiskManager
from bot.store import Store, now_iso
from bot.strategy import add_indicators, entry_signal, exit_signal, size_entry

ET = ZoneInfo("America/New_York")
log = logging.getLogger("bot")


class Bot:
    def __init__(self, settings, broker=None, jev=None, telegram=None, store=None):
        self.s = settings
        self.cfg = settings.strategy
        self.p = self.cfg["strategy"]
        self.store = store or Store(settings.db_path)
        self.kill = KillSwitch(self.store)
        self.risk = RiskManager(self.cfg["risk"], self.store, self.kill)
        self.approvals = Approvals(self.store, self.cfg["risk"]["approval_ttl_minutes"])
        self.broker = broker or Broker(settings.alpaca_key, settings.alpaca_secret, settings.paper)
        self.jev = jev or JevGate(settings.typesafe_key, self.cfg["jev"],
                                  settings.jev_price_in_per_m, settings.jev_price_out_per_m)
        self.tg = telegram or Telegram(settings.telegram_token, settings.telegram_chat_id)
        self._kill_handled = False

    # ---------- alerts ----------
    def alert(self, text: str, level: str = "info", kind: str = "alert") -> None:
        self.store.event(kind, text, level)
        log.log(logging.WARNING if level in ("error", "critical") else logging.INFO, text)
        self.tg.send(text)

    # ---------- every minute ----------
    def tick(self) -> None:
        self.handle_commands()
        self.enforce_kill_switch()
        if self.kill.tripped:
            return
        acct = self.broker.account()
        self.store.set("last_account", acct)
        if not self.risk.check_daily_loss(acct["equity"], acct["last_equity"]):
            self.enforce_kill_switch()
            return
        for aid in self.approvals.expire_old():
            self.alert(f"⌛ Approval #{aid} expired, trade not placed")
        clock = self.broker.clock()
        if clock["is_open"]:
            for a in self.approvals.approved_ready():
                self.execute_signal(a["signal_id"], approval_id=a["id"])
        self.sync_fills()
        self.risk.record_ok()

    def enforce_kill_switch(self) -> None:
        if not self.kill.tripped:
            self._kill_handled = False
            return
        if self._kill_handled:
            return
        self._kill_handled = True
        msg = f"🛑 KILL SWITCH ACTIVE: {self.kill.reason}. All trading halted."
        try:
            self.broker.cancel_all()
            if self.cfg["risk"].get("flatten_on_kill", True):
                self.broker.flatten_all()
                msg += " Open orders cancelled and positions closed."
            else:
                msg += " Open orders cancelled. Positions left open."
        except Exception as e:  # noqa: BLE001
            msg += f" ⚠️ Cleanup failed ({type(e).__name__}: {e}). CHECK THE ACCOUNT MANUALLY."
        for a in self.approvals.pending():
            self.approvals.decide(a["id"], approve=False, by="kill-switch")
        self.alert(msg + " Send /resume to re-enable.", "critical", "kill_switch")

    def handle_commands(self) -> None:
        for text in self.tg.poll_commands():
            parts = text.split()
            cmd, arg = parts[0].lower().split("@")[0], (parts[1] if len(parts) > 1 else "")
            if cmd == "/kill":
                self.kill.trip("manual /kill from Telegram")
            elif cmd == "/resume":
                try:
                    self.kill.reset("telegram")
                    self._kill_handled = False
                    self.alert("✅ Kill switch reset. Trading resumes on the next evaluation.", "warning")
                except RuntimeError as e:
                    self.tg.send(str(e))
            elif cmd in ("/approve", "/reject") and arg.isdigit():
                ok, msg = self.approvals.decide(int(arg), approve=cmd == "/approve", by="telegram")
                self.tg.send(("✅ " if ok else "⚠️ ") + msg)
            elif cmd == "/status":
                acct = self.store.get("last_account", {})
                self.tg.send(
                    f"{'PAPER' if self.s.paper else 'LIVE'} · kill switch: "
                    f"{'ON (' + self.kill.reason + ')' if self.kill.tripped else 'off'}\n"
                    f"Equity ${acct.get('equity', 0):,.2f} (day {acct.get('equity', 0) - acct.get('last_equity', 0):+,.2f})\n"
                    f"Pending approvals: {len(self.approvals.pending())}"
                )
            else:
                self.tg.send("Commands: /status · /approve <id> · /reject <id> · /kill · /resume")

    def sync_fills(self) -> None:
        since = datetime.now(timezone.utc) - timedelta(days=30)
        new = 0
        for o in self.broker.filled_orders_since(since):
            side = o.side.value if hasattr(o.side, "value") else str(o.side)
            ts = o.filled_at.astimezone(timezone.utc).isoformat(timespec="seconds")
            if self.store.add_fill(str(o.id), ts, o.symbol, side, float(o.filled_qty), float(o.filled_avg_price),
                                   o.client_order_id):
                new += 1
                if self.store.get("fills_initialized"):
                    self.alert(f"✅ FILL {side.upper()} {o.filled_qty} {o.symbol} @ ${float(o.filled_avg_price):,.2f}",
                               kind="fill")
        self.store.set("fills_initialized", True)  # first sync imports history silently
        if new:
            attribute_pnl(self.store)

    # ---------- once a day ----------
    def evaluate(self) -> None:
        if self.kill.tripped:
            log.info("Kill switch active; skipping evaluation")
            return
        symbols = self.p["symbols"]
        start = datetime.now(timezone.utc) - timedelta(days=int(max(self.p["trend_sma"], 200) * 1.6) + 30)
        bars = daily_bars(self.s, symbols, start)
        positions = self.broker.positions()
        for sym in symbols:
            if sym not in bars:
                self.alert(f"⚠️ No bars for {sym}", "error", "error")
                continue
            b = bars[sym].copy()
            px = latest_price(self.s, sym)
            today = datetime.now(ET).date()
            # Use the live price as today's provisional close.
            if b.index[-1].date() == today:
                b.loc[b.index[-1], "close"] = px
                b.loc[b.index[-1], "high"] = max(b["high"].iloc[-1], px)
                b.loc[b.index[-1], "low"] = min(b["low"].iloc[-1], px)
            else:  # today's partial bar isn't published yet
                b.loc[pd.Timestamp(today)] = {"open": px, "high": px, "low": px, "close": px, "volume": 0}
            df = add_indicators(b, self.p)
            df["entry"] = entry_signal(df, self.p)
            df["exit"] = exit_signal(df)
            row = df.iloc[-1]

            if sym in positions and row["exit"]:
                self.broker.close(sym)
                self.alert(f"↘️ Trend exit: closing {sym} (close {px:.2f} < SMA{self.p['exit_sma']} {row['sma_exit']:.2f})")
                continue
            if not row["entry"]:
                continue
            self.handle_entry(sym, df, px, positions)

    def handle_entry(self, sym: str, df, px: float, positions: dict) -> None:
        row = df.iloc[-1]
        plan = size_entry(sym, px, float(row["atr"]), self.p, self.cfg["risk"]["max_position_usd"])
        if plan is None:
            self.store.add_signal(symbol=sym, side="buy", price=px, status="blocked_risk",
                                  reason="less than one share fits max position size")
            return
        sid = self.store.add_signal(symbol=sym, side="buy", price=px, stop=plan.stop, take_profit=plan.take_profit,
                                    qty=plan.qty, notional=plan.notional, status="new")

        news = headlines(self.s, sym, limit=self.cfg["jev"]["max_headlines"])
        state = build_state(sym, df, news, as_of=datetime.now(ET).isoformat(timespec="minutes"))
        d = self.jev.decide(sym, state)
        self.store.execute(
            "INSERT INTO jev_decisions(ts, signal_id, symbol, passed, probabilities, gates, latency_ms, "
            "input_tokens, output_tokens, cost_usd, error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), sid, sym, int(d.passed), _json(d.probabilities), _json(d.gates), d.latency_ms,
             d.input_tokens, d.output_tokens, d.cost_usd, d.error),
        )
        if not d.passed:
            why = d.error or "; ".join(f"{g['question']}.{g['outcome']}={g['value']} (need {g['op']} {g['threshold']})"
                                       for g in d.gates if not g["passed"])
            self.store.update_signal(sid, status="blocked_jev", reason=why)
            self.alert(f"🚫 {sym} entry blocked by Jev: {why}", kind="signal")
            return

        verdict = self.risk.check_entry(plan.notional, len(positions), sym in positions)
        if not verdict.ok:
            self.store.update_signal(sid, status="blocked_risk", reason=verdict.reason)
            self.alert(f"🚫 {sym} entry blocked by risk: {verdict.reason}", kind="signal")
            return
        if verdict.needs_approval:
            aid = self.approvals.request(sid)
            gates = ", ".join(f"{g['question']} {g['value']}" for g in d.gates)
            self.alert(
                f"🟡 APPROVAL NEEDED #{aid}: BUY {plan.qty} {sym} ≈ ${plan.notional:,.2f}\n"
                f"stop {plan.stop} · target {plan.take_profit}\nJev: {gates}\n"
                f"Reply /approve {aid} or /reject {aid} (expires in {self.cfg['risk']['approval_ttl_minutes']} min)",
                kind="approval",
            )
            return
        self.execute_signal(sid)

    def execute_signal(self, signal_id: int, approval_id: int | None = None) -> None:
        sig = self.store.signal(signal_id)
        positions = self.broker.positions()
        # Re-check risk at execution time: things may have changed while waiting for approval.
        verdict = self.risk.check_entry(sig["notional"], len(positions), sig["symbol"] in positions)
        if approval_id is not None:
            self.approvals.mark_executed(approval_id)
        if not verdict.ok:
            self.store.update_signal(signal_id, status="blocked_risk", reason=verdict.reason)
            self.alert(f"🚫 {sig['symbol']} not executed: {verdict.reason}", kind="signal")
            return
        try:
            order = self.broker.buy_bracket(sig["symbol"], int(sig["qty"]), sig["stop"], sig["take_profit"],
                                            client_order_id=f"sig-{signal_id}-{int(time.time())}")
        except Exception as e:  # noqa: BLE001
            self.store.update_signal(signal_id, status="error", reason=str(e)[:300])
            self.risk.record_error(f"order failed for {sig['symbol']}: {e}")
            self.alert(f"❌ ORDER ERROR {sig['symbol']}: {e}", "error", "error")
            return
        self.store.update_signal(signal_id, status="submitted", order_id=str(order.id))
        self.alert(f"📤 Submitted BUY {sig['qty']} {sig['symbol']} (stop {sig['stop']}, target {sig['take_profit']})",
                   kind="order")

    def send_daily_report(self) -> None:
        rep = daily_report(self.store, self.store.get("last_account"))
        self.alert(format_report(rep), kind="report")

    # ---------- scheduling ----------
    def run_forever(self) -> None:
        mode = "PAPER" if self.s.paper else "LIVE"
        self.alert(f"🤖 Bot started ({mode}). Symbols {self.p['symbols']}, "
                   f"eval {self.p['eval_time_et']} ET, validated={self.cfg.get('validated')}")
        while True:
            try:
                self.tick()
                self.maybe_run_daily_jobs()
            except Exception as e:  # noqa: BLE001
                n = self.risk.record_error(f"{type(e).__name__}: {e}")
                log.error(traceback.format_exc())
                self.tg.send(f"❌ ERROR ({n} in a row): {type(e).__name__}: {str(e)[:300]}")
                self.enforce_kill_switch()
            time.sleep(60)

    def maybe_run_daily_jobs(self) -> None:
        now = datetime.now(ET)
        today = now.date().isoformat()
        hhmm = now.strftime("%H:%M")
        if hhmm >= self.p["eval_time_et"] and self.store.get("last_eval_date") != today:
            clock = self.broker.clock()
            if clock["is_open"]:
                self.store.set("last_eval_date", today)
                self.evaluate()
        if hhmm >= self.p["report_time_et"] and self.store.get("last_report_date") != today \
                and now.weekday() < 5:
            self.store.set("last_report_date", today)
            self.send_daily_report()


def _json(x) -> str:
    import json
    return json.dumps(x, default=str)


def test_kill_switch(bot: Bot) -> None:
    """Paper-only drill: trip the switch and confirm entries are refused and orders cancelled."""
    if not bot.s.paper:
        raise SystemExit("Kill-switch drill runs on paper only.")
    bot.kill.trip("kill-switch drill (--test-kill-switch)")
    bot.enforce_kill_switch()
    v = bot.risk.check_entry(50, 0, False)
    assert not v.ok, "risk manager accepted an entry while the kill switch was active"
    open_orders = bot.broker.client.get_orders()
    print(f"Entry refused: {v.reason}")
    print(f"Open orders after kill: {len(open_orders)}")
    print(f"Positions after kill: {len(bot.broker.positions())}")
    bot.store.set("kill_switch_drill", {"passed": not open_orders, "at": datetime.now(timezone.utc).isoformat()})
    bot.kill.reset("drill")
    bot.alert("🧪 Kill-switch drill complete: trading refused while active; switch reset.", kind="kill_switch")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-kill-switch", action="store_true")
    args = ap.parse_args()
    bot = Bot(load_settings())
    if args.test_kill_switch:
        test_kill_switch(bot)
    elif args.once:
        bot.tick()
        bot.evaluate()
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
