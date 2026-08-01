# MONDAY BUILD HANDOFF — Positive Options Bot Rebuild

## Goal
Rebuild the Schwab bot around the TWO strategies that backtested POSITIVE on
real 4-month EODHD data. Ready to run Monday. Weekly/monthly backtest reporting
to fix/upgrade over time.

## What backtested POSITIVE (real 4-month EODHD data, modeled premiums)
1. **High-IV put selling**: d20 delta, IV~50%, exit 50% profit / 2x stop
   → +0.043% expectancy, 84% win. KEY: only positive when IV elevated.
   Low IV (35%) was NEGATIVE. The IV filter is what makes it work.
2. **ETF covered calls**: 2% OTM, ~+0.037% expectancy, 92% win. Small but positive.

Everything else tested NEGATIVE on real data: day-trading (all variants:
momentum, CRT, EMA200+pullback, scoring system) consistently ~-0.09%/trade
across all 4 months. DO NOT build day-trading/swing/call-buying — proven negative.

## The rebuild spec
- **Capital: $11,000.** Bot sizes within full 11k. Keep SGOV reserve (~$705).
- **Live scanner** must size to fit 11k (cash-secured puts on stocks cheap
  enough to afford — strike x100 <= available cash per contract).
- **Strategy 1 — Put selling WITH IV FILTER:**
  - Read real IV from Schwab option chain (it's in the chain data)
  - Only sell when IV above a threshold (this is the positive-edge filter)
  - Delta ~0.20, exit 50% profit, 2x premium stop
  - Bot sizes contracts within 11k
- **Strategy 2 — ETF covered calls:** when own 100 ETF shares, sell ~2% OTM calls
- **Weekly/monthly backtest job:** auto-run backtest, report expectancy via
  Telegram, flag if a strategy turns negative.

## Key project facts (carry over)
- Fly.io app: schwab-bot-wandering-comet-7755 | machine 908001deae74d8 | IAD
- GitHub: https://github.com/5jayy/Schwab-Bot
- Local: /Users/jay/Desktop/schwab bot/ (venv: source venv/bin/activate)
- Deploy: git add/commit/push then flyctl deploy. Logs: fly logs --no-tail | tail
- Backtests run LOCALLY (no deploy): python3 <file>.py after activating venv
- Data source for backtests: EODHD demo key (works!):
  https://eodhd.com/api/intraday/AAPL.US?api_token=demo&interval=5m&fmt=json
  Demo tickers: AAPL.US, TSLA.US, AMZN.US, VTI.US (~4 months 5m data)
- WebSocket streaming (stream.py): Phase 1+2A done/proven. Phase 2B needs live fill.
- Existing put selling already works live (LCID/CLSK/RIVN puts). The rebuild
  ADDS the IV filter + capital sizing to what exists.

## Honest guardrails (keep these)
- Options premiums CANNOT be historically backtested for free (no data). The
  positive results are MODELED. Validate live SMALL, compare real vs modeled.
- High win rate != profitable. Track EXPECTANCY = Win% x AvgWin - Loss% x AvgLoss.
- Positive edge is THIN (+0.043% modeled). Run small, confirm live, scale only
  if real results match. This builds slow income, not fast returns.

## Backtest files built this session (in the bot folder)
backtest_multi.py (multi-strategy, real EODHD data), backtest_options_real.py
(put/ETF config sweep — this is the one that found the positive configs),
plus day-trade backtests (all showed negative — reference only).

## First task in new chat
Start the put-selling rebuild: add IV filter to the live put scanner + wire
capital sizing to fit 11k. Test the backtest config, then build the live change.
