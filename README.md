# Trading bot: Opus designs, Jev decides, the exchange executes

> **Not financial advice.** This is a paper-trading research tool. Every entry above $100 needs
> your manual approval, and live trading is locked behind an explicit acknowledgement.

```
strategy.md ──► technical rules (daily bars) ──► entry signal?
                                                    │ yes
                    headlines + bars (as DATA) ──► Jev: 4 fixed-outcome questions, 1 parallel call
                                                    │ every probability clears its threshold?
                                                 risk manager: size cap · open positions · kill switch
                                                    │ notional > $100?
                                                 Telegram / dashboard approval (30 min TTL)
                                                    │
                                                 Alpaca PAPER: market buy + server-side stop & target
```

- **Opus (design time):** the rules in `strategy.md`, the backtester, and the parameter search.
- **Jev (run time):** TypeSafe's System One model ([typesafe.ai](https://typesafe.ai), Python SDK
  `typesafe-sdk`). It scores each entry signal with calibrated probabilities on fixed questions.
  It can veto an entry. It can't originate a trade, change its size, or block an exit.
- **Alpaca:** only executes. Stops and targets sit at the broker as bracket legs, so they work
  even if the bot is down.

## Project layout

| Path | What |
|---|---|
| `strategy.md` | Risk rules + strategy + Jev thresholds. The JSON block at the bottom is what the code reads |
| `bot/backtest.py` | Grid search, survivor filter (drawdown, win rate, regimes), out-of-sample check |
| `bot/strategy.py` | Indicator + entry/exit rules shared by backtest and live |
| `bot/jev_layer.py` | Jev questions, state builder, threshold gates (fail closed) |
| `bot/risk.py` | Position cap, max positions, daily loss limit, kill switch |
| `bot/approvals.py` | Manual approval queue |
| `bot/runner.py` | 24/7 loop: risk every minute, signals at 15:45 ET, report at 16:15 ET |
| `bot/report.py` | FIFO P&L, daily report (trades, P&L, win rate, largest loss, Jev latency & cost) |
| `bot/final_check.py` | Pre-live checklist, computed from data |
| `dashboard/app.py` | Strategy-tester dashboard: every signal, its probabilities, and the result |
| `deploy/` | Dockerfile, compose, systemd units, **DEPLOY.md** (VPS + BotFather walkthrough) |

## Setup (local)

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
```

Fill in `.env`. **That file is the only place keys go.** It's git-ignored.

1. **Jev key:** [console.typesafe.ai](https://console.typesafe.ai) → `TYPESAFE_API_KEY`
2. **Alpaca paper keys:** [app.alpaca.markets](https://app.alpaca.markets), switch to *Paper Trading* →
   API Keys → `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`. Keep `ALPACA_PAPER=true`.
   Alpaca's Trading API has no withdrawal endpoint, and this code never touches account transfers.
3. **Telegram:** see [deploy/DEPLOY.md](deploy/DEPLOY.md#2-create-the-telegram-bot-botfather)
4. **Dashboard token:** `python -c "import secrets;print(secrets.token_urlsafe(32))"`

The bot never asks for, stores, or types passwords or 2FA codes. If anything ever prompts you for
one in connection with this bot, stop.

## Run it

```sh
python -m pytest -q                       # 26 tests: risk, approvals, Jev gates, backtest, runner flow
python -m bot.backtest --write            # 2016→today, SPY+QQQ; writes the winner to strategy.md only if it passes out-of-sample
python -m bot.runner --test-kill-switch   # paper drill: trip, verify halt, reset
python -m bot.runner                      # the bot (Ctrl-C to stop)
python -m dashboard.app                   # http://127.0.0.1:8000/?token=<DASHBOARD_TOKEN>
python -m bot.final_check                 # before ever considering live money
```

For 24/7 operation, follow [deploy/DEPLOY.md](deploy/DEPLOY.md).

## Output

- **A running bot on paper:** `docker compose ... up -d` on the VPS
- **Dashboard link:** `http://localhost:8000/?token=…` through your SSH tunnel
- **strategy.md:** the rulebook (marked `validated: true` once the backtest winner passes out-of-sample)
- **Daily report** (Telegram + `reports/daily-YYYY-MM-DD.json`): trades, P&L, win rate, largest
  loss, Jev average latency and cost per decision

## Final check (before suggesting live money)

`python -m bot.final_check` computes the first three. The fourth needs your judgment.

1. **Does paper match the backtest?** ≥ 20 closed paper trades, win rate within ±10 points and
   average trade % within ±0.5 points of the backtest's full-period numbers.
2. **Did the kill switch fire in testing?** `--test-kill-switch` passed on paper, and a
   critical kill-switch event is in the log.
3. **Is the strategy validated?** It survived in-sample *and* out-of-sample filters.
4. **Which market regime would break this?** A sideways, whipsawing market (SPY in 2015 is a good
   example): breakouts trigger and immediately fail, and each loss costs about the stop
   distance. Check `by_regime` and `by_year` in `reports/backtest.json`. A strategy that only
   made money in one regime is a bet on that regime continuing.

Only if all four hold would switching to live be worth discussing, with a smaller size than
paper, `approval_threshold_usd` kept low, and `LIVE_TRADING_ACK=I_ACCEPT_LIVE_RISK` set by you.

## WHAT COULD BLOW UP THIS ACCOUNT?

Ranked roughly by likelihood × damage:

1. **Overfitting.** The grid tests 180 parameter sets. Some will look great by chance. The
   out-of-sample split and regime filter reduce this risk but can't remove it. Two years of
   data is a minimum, not proof. *Mitigation:* paper must match the backtest before any live money.
2. **Overnight gaps through the stop.** A stop is a market order once triggered. A bad CPI print or
   geopolitical shock can gap QQQ 3–5% past it at the open. With $1,000 positions that's ~$50 per
   position, but the daily loss limit is measured *after* the fact and can't prevent a gap.
3. **Correlation.** SPY and QQQ are highly correlated. "Two positions" is closer to one large bet
   than two independent ones. Both stops can hit on the same day.
4. **Silent failure.** If the VPS dies, no new trades happen, but open positions keep their
   broker-side stop and target, because bracket legs live at Alpaca. You won't get alerts while
   it's down. *Mitigation:* `restart: unless-stopped`, and treat a missing 16:15 daily report as
   an alarm.
5. **Jev misjudging or being steered by headlines.** Headlines are untrusted data. A crafted
   or misleading headline can shift a probability. Damage is bounded: Jev can only *allow*
   an entry the price rules already produced. It can't size trades or block exits. All
   failures fail closed.
6. **Key leakage.** Anyone holding your Alpaca keys can trade the account, though not withdraw.
   *Mitigation:* `.env` with `chmod 600`, SSH-key-only VPS, dashboard bound to localhost,
   rotate keys if in doubt. Never commit `.env`.
7. **Config mistakes.** A typo in `strategy.md` (e.g. `max_position_usd: 100000`) is obeyed.
   *Mitigation:* the loader rejects missing or non-positive risk values. Read the startup
   Telegram message and `git diff strategy.md` after every change.
8. **Paper ≠ live.** Paper fills are optimistic: no queue position, no partial fills, IEX data.
   Expect live results to be worse than paper by at least the spread on every trade.
9. **Strategy decay.** Momentum edges in index ETFs are thin and crowded. A strategy that
   backtested well can simply stop working. *Mitigation:* the daily report's win rate and P&L
   against the backtest; kill it if it drifts.
