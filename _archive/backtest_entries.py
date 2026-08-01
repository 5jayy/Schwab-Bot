"""
ENTRY + EXIT BACKTEST — tests your REAL entry filters with COMBO exits.

Uses the actual entry-signal functions from scanner.py (candle_strength, ATR,
liquidity_sweep, candlestick_bonus, score) — the parts that CAN be computed on
30-min candles. The live-only pieces (MTF conviction, order flow) can't be
backtested, so this tests the price-action portion of your entry.

Compares:
  • FILTERED  — only enter when your real filters pass, exit with COMBO
  • EVERY_BAR — enter every bar with COMBO exit (no entry selectivity)
  • BUY_HOLD  — enter every bar, hold to end (market average)

The question: do your entry FILTERS raise win% / beat the market vs entering
blindly? If FILTERED beats EVERY_BAR and BUY_HOLD, your entries add edge.

Run locally (no deploy):  python3 backtest_entries.py
Tune the thresholds below and re-run to find what works.
"""

import sys
import time
import requests
from auth import get_valid_token

# Reuse the REAL entry-signal functions from the live scanner
from scanner import calc_atr, liquidity_sweep, candlestick_bonus, candle_strength

BASE_URL = "https://api.schwabapi.com/marketdata/v1"

# ---- ENTRY FILTER THRESHOLDS (tune these, re-run) ----
MIN_CANDLE_STRENGTH = 20      # live uses 20; try 30, 40 to be pickier
ATR_MIN             = 0.5     # volatility floor (%)
ATR_MAX             = 6.0     # volatility ceiling (%)
MIN_SCORE           = 40      # live Tier-2 min_score; try higher
MIN_SWEEP           = 0       # liquidity sweep bonus floor; try >0 to require it

# ---- COMBO EXIT (the proven winner) ----
HARD_STOP  = 0.07
TRAIL      = 0.07
BE_AT      = 0.02
TARGET     = 0.12


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_candles(symbol, period=10, frequency=30):
    try:
        resp = requests.get(
            f"{BASE_URL}/pricehistory", headers=headers(),
            params={"symbol": symbol, "periodType": "day", "period": period,
                    "frequencyType": "minute", "frequency": frequency,
                    "needExtendedHoursData": False},
            timeout=15)
        resp.raise_for_status()
        return resp.json().get("candles", [])
    except Exception as ex:
        print(f"  {symbol}: fetch error {ex}")
        return []


def entry_passes(window):
    """Return True if your REAL entry filters pass at this candle."""
    if len(window) < 20:
        return False
    price = window[-1]["close"]
    if price <= 0:
        return False

    # ATR volatility filter (same as live)
    atr = calc_atr(window)
    atr_pct = (atr / price * 100) if atr and price > 0 else 2.0
    if atr_pct < ATR_MIN or atr_pct > ATR_MAX:
        return False

    # Candle strength (same as live)
    strength = candle_strength(window)
    if strength < MIN_CANDLE_STRENGTH:
        return False

    # Liquidity sweep + candlestick bonus (same as live)
    sweep = liquidity_sweep(window)
    if sweep < MIN_SWEEP:
        return False
    cbonus = candlestick_bonus(window)

    # Score (mirrors live: strength*0.7 + sweep + candle_bonus)
    # change_pct approximated from last 2 closes
    prev = window[-2]["close"] if len(window) >= 2 else price
    change_pct = ((price - prev) / prev * 100) if prev > 0 else 0
    score = strength * 0.7 + sweep + cbonus + change_pct * 2
    if score < MIN_SCORE:
        return False
    return True


def combo_exit(entry, fwd):
    """COMBO: hard stop from entry + trail once green + profit target."""
    high = entry
    hard_px = entry * (1 - HARD_STOP)
    tgt_px  = entry * (1 + TARGET)
    for c in fwd:
        px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
        high = max(high, hi)
        if lo <= hard_px:
            return (hard_px - entry) / entry * 100
        if hi >= tgt_px:
            return (tgt_px - entry) / entry * 100
        if (px - entry) / entry >= BE_AT:
            trail_px = high * (1 - TRAIL)
            if px <= trail_px:
                return (px - entry) / entry * 100
    return (fwd[-1]["close"] - entry) / entry * 100


def backtest_symbol(symbol, hold_bars=12):
    candles = get_candles(symbol)
    if len(candles) < 40:
        return None
    out = {"FILTERED": [], "EVERY_BAR": [], "BUY_HOLD": []}
    for i in range(20, len(candles) - hold_bars - 1, 2):
        window = candles[:i+1]
        entry  = candles[i]["close"]
        fwd    = candles[i+1:i+1+hold_bars]
        if entry <= 0 or not fwd:
            continue
        # EVERY_BAR + BUY_HOLD (baselines)
        out["EVERY_BAR"].append(combo_exit(entry, fwd))
        out["BUY_HOLD"].append((fwd[-1]["close"] - entry) / entry * 100)
        # FILTERED — only if real entry filters pass
        if entry_passes(window):
            out["FILTERED"].append(combo_exit(entry, fwd))
    return out


def run(symbols):
    print(f"\n{'='*60}")
    print("ENTRY+EXIT BACKTEST — do your entry filters add edge?")
    print(f"{'='*60}")
    print(f"Filters: strength>={MIN_CANDLE_STRENGTH} ATR {ATR_MIN}-{ATR_MAX}% "
          f"score>={MIN_SCORE} sweep>={MIN_SWEEP}")
    print(f"Exit: COMBO (hard {int(HARD_STOP*100)}% + trail {int(TRAIL*100)}% "
          f"+ target {int(TARGET*100)}%)\n")

    agg = {"FILTERED": [], "EVERY_BAR": [], "BUY_HOLD": []}
    for sym in symbols:
        r = backtest_symbol(sym)
        if r:
            for k in agg:
                agg[k].extend(r[k])
            print(f"  tested {sym}  (filtered entries: {len(r['FILTERED'])})")
        time.sleep(0.3)

    print(f"\n{'-'*60}")
    print(f"{'STRATEGY':<12}{'trades':>8}{'win%':>8}{'avg':>9}{'worst':>9}{'total':>10}")
    print(f"{'-'*60}")
    avgs = {}
    for name in ["FILTERED", "EVERY_BAR", "BUY_HOLD"]:
        rets = agg[name]
        if not rets:
            print(f"{name:<12}{'0':>8}  (no trades — filters too strict)")
            continue
        n = len(rets); wins = [x for x in rets if x > 0]
        avgs[name] = sum(rets) / n
        tag = "  <-- market" if name == "BUY_HOLD" else ""
        print(f"{name:<12}{n:>8}{len(wins)/n*100:>7.0f}%{sum(rets)/n:>8.2f}%"
              f"{min(rets):>8.1f}%{sum(rets):>9.1f}%{tag}")

    print(f"\n{'-'*60}")
    print("VERDICT:")
    f = avgs.get("FILTERED"); e = avgs.get("EVERY_BAR"); b = avgs.get("BUY_HOLD")
    if f is None:
        print("  Filters too strict — no trades. Loosen thresholds.")
    else:
        print(f"  FILTERED avg {f:+.2f}%  vs  EVERY_BAR {e:+.2f}%  vs  market {b:+.2f}%")
        if f > b and f > 0:
            print("  -> Entry filters ADD EDGE and beat market. Keep + refine.")
        elif f > e and f > b:
            print("  -> Filters help (beat blind entry + market) but still thin.")
        elif f > e:
            print("  -> Filters beat blind entry but not market. Partial edge.")
        else:
            print("  -> Filters DON'T help. Entry logic needs a different signal.")
    print("  Tune thresholds at top of file, re-run. ~10d sample = directional.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    syms = sys.argv[1:] if len(sys.argv) > 1 else \
        ["NVDA", "AAPL", "PLUG", "RIOT", "SOFI", "F", "INTC", "AMD", "HOOD", "MARA",
         "TSLA", "PLTR", "COIN", "SNAP", "UBER"]
    run(syms)
