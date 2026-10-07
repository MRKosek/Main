# strategy.md: the rulebook

The bot reads the `json` block at the bottom of this file at startup. **That block is the
single source of truth.** If prose and the block disagree, the block wins. Edit the block,
restart the bot, and the change is logged.

> **Status: candidate parameters, NOT yet validated.** The values below are the defaults the
> backtester starts from. Run `python -m bot.backtest --write` with real Alpaca data to replace
> the `strategy` section with the backtest winner. See "How the winner is picked" below.

---

## 1. Risk rules (these were set before anything else and override everything)

| Rule | Value | Enforced in |
|---|---|---|
| Max position size | **$1,000** notional per symbol | `bot/risk.py` `check_entry` |
| Max open positions | **2** | `bot/risk.py` `check_entry` |
| Daily loss limit | **$150** (account equity vs. yesterday's close) | `bot/risk.py` `check_daily_loss` triggers the kill switch |
| Kill switch | Halts ALL trading: cancels open orders, flattens positions, refuses new entries until a manual `/resume` | `bot/risk.py` `KillSwitch` |
| Kill-switch triggers | daily loss limit hit · 5 consecutive errors · `data/KILL` file present · `/kill` on Telegram · dashboard button | `bot/runner.py` |
| Manual approval | any trade with notional **above $100** waits for `/approve <id>` on Telegram or the dashboard button. Unanswered approvals expire after 30 min | `bot/approvals.py` |
| Paper first | `ALPACA_PAPER=true` is required. Live trading also needs `LIVE_TRADING_ACK=I_ACCEPT_LIVE_RISK`, and that should only be set once the final check in README passes | `bot/config.py` |
| API keys | Trading only. The code never calls any transfer or withdrawal endpoint. Alpaca's Trading API has no withdrawal endpoint. On any other exchange, create keys with withdrawals **disabled** | `bot/broker.py` |
| Credentials | Keys come from `.env` only. The bot never asks for, stores or enters passwords or 2FA codes | n/a |
| Untrusted input | Headlines and market data go to Jev as **state (data)**. Question wording is fixed in code. Jev can only *veto* an entry the technical rules already produced. It can't create trades, change size, or block exits | `bot/jev_layer.py` |

With a $100 approval threshold and a $1,000 max position, **practically every entry needs your
approval.** That's intentional for paper trading. Raise `approval_threshold_usd` once paper
results match the backtest.

## 2. Strategy: momentum / trend following (long-only, daily bars)

- **Universe:** `SPY`, `QQQ` (liquid ETFs with tight spreads and no single-company earnings gaps)
- **Timeframe:** daily bars. Signals are evaluated once a day at **15:45 America/New_York** on
  trading days and execute as market orders before the close
- **Entry (breakout family):** close ≥ highest close of the prior `breakout_lookback` days
  **and** close > SMA(`trend_sma`). Long only
- **Entry (sma_cross family):** SMA(`fast`) crosses above SMA(`slow`) and close > SMA(`trend_sma`)
- **Stop loss:** entry − `stop_atr` × ATR(14), placed at the broker as the stop leg of a bracket order
- **Take profit:** entry + `take_profit_atr` × ATR(14), the limit leg of the same bracket
- **Trend exit:** close < SMA(`exit_sma`) → close the position at market (no Jev gate on exits)
- **Sizing (live/paper):** fixed notional: whole shares up to the max position size ($1,000)
- **Sizing (backtest):** `position_pct` of equity per position (fractional), so returns and drawdowns
  are measured in % terms. With 2 positions × 50% this is *more* exposure than live, so backtest
  drawdowns are the pessimistic case. Compare paper vs. backtest on **per-trade % return and win rate**

### How the winner is picked (`python -m bot.backtest`)

1. Pull ≥ 2 years of daily bars (default: 2016-01-01 → today, which covers the 2018 vol shock, the 2020
   crash, the 2022 bear market and the 2023–24 rally).
2. Grid-search both families on the **first 70%** of the data (in-sample).
3. Keep survivors only: max drawdown ≤ `max_drawdown_pct`, win rate ≥ `min_win_rate`,
   ≥ `min_trades` trades, and not losing money in more than one regime (bull / bear / sideways,
   classified by price vs. a rising or falling SMA200).
4. Rank survivors by return ÷ max drawdown (not raw return).
5. Re-run the top survivor on the **last 30%** (out-of-sample). If it fails the same filters there,
   nothing is written and the report says so.

## 3. Jev questions (fixed outcomes, one parallel call per entry signal)

| Name | Type | Question | Gate |
|---|---|---|---|
| `headline_sentiment` | Choice: bullish / bearish / neutral | "Taken together, are these headlines bullish, bearish or neutral for {symbol} over the next few days?" | P(bearish) ≤ 0.30 |
| `buying_pressure` | Noul (yes/no) | "Is buying pressure building in these recent bars?" | P(yes) ≥ 0.55 |
| `event_risk` | Noul | "Do the headlines point to a scheduled event in the next two trading days likely to cause a large gap?" | P(yes) ≤ 0.40 |
| `trend_quality` | Score 0–3 | "How orderly is this uptrend?" | expected score ≥ 1.5 |

A trade fires only when **every** gate passes. If the Jev call fails or times out, the trade is
**not** taken (fail closed).

---

```json strategy-config
{
  "version": 1,
  "validated": false,
  "risk": {
    "max_position_usd": 1000,
    "max_open_positions": 2,
    "daily_loss_limit_usd": 150,
    "approval_threshold_usd": 100,
    "approval_ttl_minutes": 30,
    "max_consecutive_errors": 5,
    "flatten_on_kill": true
  },
  "strategy": {
    "family": "breakout",
    "symbols": ["SPY", "QQQ"],
    "timeframe": "1Day",
    "breakout_lookback": 20,
    "fast": 20,
    "slow": 50,
    "trend_sma": 200,
    "exit_sma": 50,
    "atr_period": 14,
    "stop_atr": 2.0,
    "take_profit_atr": 4.0,
    "eval_time_et": "15:45",
    "report_time_et": "16:15"
  },
  "backtest": {
    "start": "2016-01-01",
    "initial_equity": 10000,
    "position_pct": 50,
    "slippage_bps": 2,
    "commission_per_trade": 0,
    "in_sample_frac": 0.7,
    "max_drawdown_pct": 20,
    "min_win_rate": 0.40,
    "min_trades": 15
  },
  "jev": {
    "model": "jev-latest",
    "timeout_s": 3.0,
    "max_headlines": 15,
    "gates": [
      {"question": "headline_sentiment", "outcome": "bearish", "op": "<=", "threshold": 0.30},
      {"question": "buying_pressure", "outcome": "yes", "op": ">=", "threshold": 0.55},
      {"question": "event_risk", "outcome": "yes", "op": "<=", "threshold": 0.40},
      {"question": "trend_quality", "outcome": "expected", "op": ">=", "threshold": 1.5}
    ]
  }
}
```
