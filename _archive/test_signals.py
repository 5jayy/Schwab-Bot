"""
Circuit Signal Tester
Tests every entry/exit signal on live data so we can fix what's blocking trades.

Run: python3 test_signals.py AAPL
Run all: python3 test_signals.py
"""

import sys
import requests
import time
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"

def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}

def get_candles(symbol, period=5, frequency=30):
    resp = requests.get(
        f"{BASE_URL}/pricehistory", headers=headers(),
        params={"symbol": symbol, "periodType": "day", "period": period,
                "frequencyType": "minute", "frequency": frequency,
                "needExtendedHoursData": False},
        timeout=15
    )
    return resp.json().get("candles", []) if resp.ok else []

def get_quote(symbol):
    resp = requests.get(f"{BASE_URL}/quotes/{symbol}", headers=headers(), timeout=10)
    return resp.json().get(symbol, {}).get("quote", {}) if resp.ok else {}

def test_candle_strength(candles):
    """Body/range ratio — how strong is the signal candle."""
    if not candles:
        return 0, "no candles"
    c = candles[-1]
    rng  = c["high"] - c["low"]
    body = abs(c["close"] - c["open"])
    if rng == 0:
        return 0, "zero range"
    ratio = body / rng
    close_pos = (c["close"] - c["low"]) / rng
    score = ratio * 50 + close_pos * 30
    verdict = "✅ STRONG" if ratio > 0.35 else "❌ WEAK (need >0.35)"
    return round(score, 1), f"{verdict} | body/range={ratio:.2f} close_pos={close_pos:.2f}"

def test_volume_spike(candles):
    """Volume must be 1.3x 20-bar average."""
    if len(candles) < 21:
        return False, "not enough candles"
    vols    = [c["volume"] for c in candles[-21:]]
    avg_vol = sum(vols[:-1]) / 20
    cur_vol = vols[-1]
    ratio   = cur_vol / avg_vol if avg_vol > 0 else 0
    verdict = "✅ SPIKE" if ratio >= 1.3 else f"❌ NO SPIKE (need 1.3x, got {ratio:.2f}x)"
    return ratio >= 1.3, f"{verdict} | cur={cur_vol:,} avg={avg_vol:,.0f}"

def test_fvg(candles):
    """Fair Value Gap — 3 candle gap detection."""
    if len(candles) < 3:
        return {}, "not enough candles"
    results = []
    for i in range(len(candles)-2, max(len(candles)-10, 1), -1):
        c1, c2, c3 = candles[i-2], candles[i-1], candles[i]
        # Bullish FVG: gap between c1 high and c3 low
        if c3["low"] > c1["high"]:
            gap_size = c3["low"] - c1["high"]
            cur_price = candles[-1]["close"]
            in_gap    = c1["high"] <= cur_price <= c3["low"]
            returning = abs(cur_price - c1["high"]) / c1["high"] < 0.02
            results.append({
                "type": "bullish", "gap_size": round(gap_size, 3),
                "gap_top": c3["low"], "gap_bot": c1["high"],
                "in_gap": in_gap, "returning": returning,
                "index": i
            })
        # Bearish FVG: gap between c1 low and c3 high
        elif c3["high"] < c1["low"]:
            gap_size = c1["low"] - c3["high"]
            cur_price = candles[-1]["close"]
            in_gap    = c3["high"] <= cur_price <= c1["low"]
            results.append({
                "type": "bearish", "gap_size": round(gap_size, 3),
                "gap_top": c1["low"], "gap_bot": c3["high"],
                "in_gap": in_gap, "returning": False,
                "index": i
            })

    if not results:
        return {}, "❌ NO FVG found in last 10 candles"

    best = results[0]
    if best["in_gap"]:
        verdict = "⚠️  PRICE IN GAP — blocks entry"
    elif best["returning"]:
        verdict = "✅ RETURNING TO FVG — boosts score"
    else:
        verdict = f"ℹ️  FVG exists ({best['type']}) but price not near it"

    return best, verdict

def test_wick_rejection(candles):
    """Lower wick > 45% of range = bullish rejection."""
    if not candles:
        return False, "no candles"
    c   = candles[-1]
    rng = c["high"] - c["low"]
    if rng == 0:
        return False, "zero range"
    lower_wick = min(c["open"], c["close"]) - c["low"]
    ratio      = lower_wick / rng
    verdict    = "✅ WICK REJECTION" if ratio > 0.45 else f"❌ NO REJECTION (need >0.45, got {ratio:.2f})"
    return ratio > 0.45, verdict

def test_order_flow(symbol):
    """Bid/ask imbalance — buying pressure."""
    q = get_quote(symbol)
    if not q:
        return 0, "no quote"
    bid     = q.get("bidPrice", 0)
    ask     = q.get("askPrice", 0)
    bid_sz  = q.get("bidSize", 0)
    ask_sz  = q.get("askSize", 0)
    last    = q.get("lastPrice", 0)
    spread  = ask - bid
    mid     = (bid + ask) / 2 if bid and ask else 0
    # Price above mid = buying pressure
    if mid > 0 and last > 0:
        pressure = (last - mid) / mid * 100
    else:
        pressure = 0
    # Volume imbalance
    if bid_sz + ask_sz > 0:
        imbalance = (bid_sz - ask_sz) / (bid_sz + ask_sz)
    else:
        imbalance = 0
    score   = pressure * 10 + imbalance * 20
    verdict = "✅ BUYING PRESSURE" if score > 0 else "❌ SELLING PRESSURE"
    return round(score, 1), f"{verdict} | pressure={pressure:.3f}% imbalance={imbalance:.2f} spread=${spread:.3f}"

def test_mtf_alignment(symbol):
    """Check multi-timeframe alignment."""
    frames = {
        "30m (swing bias)": get_candles(symbol, 5, 30),
        "5m (trigger)":     get_candles(symbol, 2, 5),
    }
    aligned = 0
    results = []
    for label, candles in frames.items():
        if len(candles) < 20:
            results.append(f"  {label}: ❌ no data")
            continue
        closes = [c["close"] for c in candles]
        ma20   = sum(closes[-20:]) / 20
        ma10   = sum(closes[-10:]) / 10
        price  = closes[-1]
        up     = price > ma20 and price > ma10
        if up:
            aligned += 1
            results.append(f"  {label}: ✅ UP (price > MA10/MA20)")
        else:
            results.append(f"  {label}: ❌ DOWN/FLAT")
    conviction = aligned
    verdict = f"Conviction {conviction}/2"
    return conviction, verdict, results

def test_win_rate_gate():
    """Check if win rate gate is blocking."""
    from ledger import load_ledger
    ledger  = load_ledger()
    history = ledger.get("win_rate_history", [])
    wr      = sum(history) / len(history) if history else 0
    if len(history) < 10:
        verdict = f"⚠️  GATE SKIPPED (only {len(history)} trades, need 10)"
    elif wr < 0.40:
        verdict = f"❌ WIN RATE GATE BLOCKING ({wr:.0%} < 40%)"
    else:
        verdict = f"✅ WIN RATE OK ({wr:.0%})"
    return wr, verdict, len(history)

def test_position_sizing(capital=2167):
    """Show what position sizes would be."""
    ceiling = min(capital * 0.10, 300)  # 10% of capital, max $300
    print(f"\n── Position Sizing ──")
    print(f"Capital:  ${capital:,.0f}")
    print(f"Ceiling:  ${ceiling:,.0f}")
    print(f"4/4 conv: ${ceiling:,.0f} (full)")
    print(f"3/4+FVG:  ${ceiling*0.70:,.0f} (70%)")
    print(f"3/4:      ${ceiling*0.50:,.0f} (50%)")

def run_full_test(symbol: str):
    print(f"\n{'='*55}")
    print(f"SIGNAL TEST — {symbol}")
    print(f"{'='*55}")

    # Get candles
    candles_30m = get_candles(symbol, 5, 30)
    candles_5m  = get_candles(symbol, 2, 5)
    print(f"Candles: {len(candles_30m)} x 30m | {len(candles_5m)} x 5m")

    if not candles_30m:
        print("❌ No candles — API issue or market closed")
        return

    # Current price
    q     = get_quote(symbol)
    price = q.get("lastPrice", candles_30m[-1]["close"])
    print(f"Price:   ${price:.2f}")

    # 1. Candle strength
    score, verdict = test_candle_strength(candles_30m)
    print(f"\n── 1. Candle Strength ──")
    print(f"Score: {score} | {verdict}")

    # 2. Volume spike
    spiked, verdict = test_volume_spike(candles_30m)
    print(f"\n── 2. Volume Spike ──")
    print(f"{verdict}")

    # 3. FVG
    fvg, verdict = test_fvg(candles_30m)
    print(f"\n── 3. FVG Detection ──")
    print(f"{verdict}")
    if fvg:
        print(f"  Gap: ${fvg.get('gap_bot',0):.2f} - ${fvg.get('gap_top',0):.2f} ({fvg.get('type','')})")

    # 4. Wick rejection
    wick, verdict = test_wick_rejection(candles_30m)
    print(f"\n── 4. Wick Rejection ──")
    print(f"{verdict}")

    # 5. Order flow
    flow_score, verdict = test_order_flow(symbol)
    print(f"\n── 5. Order Flow ──")
    print(f"Score: {flow_score} | {verdict}")

    # 6. MTF alignment
    conv, verdict, frame_results = test_mtf_alignment(symbol)
    print(f"\n── 6. MTF Alignment ──")
    print(f"{verdict}")
    for r in frame_results:
        print(r)

    # 7. Win rate gate
    wr, verdict, n_trades = test_win_rate_gate()
    print(f"\n── 7. Win Rate Gate ──")
    print(f"{verdict} ({n_trades} trades in history)")

    # Summary
    print(f"\n── SUMMARY ──")
    gates_passed = 0
    gates_total  = 5
    if score > 20:           gates_passed += 1; print(f"✅ Candle strength ({score})")
    else:                    print(f"❌ Candle strength too weak ({score})")
    if spiked:               gates_passed += 1; print(f"✅ Volume spike")
    else:                    print(f"❌ Volume spike missing")
    if fvg and not fvg.get("in_gap"): gates_passed += 1; print(f"✅ FVG clear")
    elif fvg and fvg.get("in_gap"):   print(f"❌ Price in FVG gap — blocked")
    else:                    gates_passed += 1; print(f"✅ No FVG block")
    if flow_score > 0:       gates_passed += 1; print(f"✅ Order flow positive")
    else:                    print(f"❌ Order flow negative")
    if conv >= 1:            gates_passed += 1; print(f"✅ MTF aligned ({conv}/2)")
    else:                    print(f"❌ MTF not aligned")

    print(f"\nGates passed: {gates_passed}/{gates_total}")
    if gates_passed >= 4:
        print(f"🟢 WOULD TRADE — strong setup")
    elif gates_passed >= 3:
        print(f"🟡 MARGINAL — might trade with FVG boost")
    else:
        print(f"🔴 NO TRADE — too many gates failing")

    print(f"{'='*55}\n")


if __name__ == "__main__":
    symbols = sys.argv[1:] if len(sys.argv) > 1 else ["AAPL", "NVDA", "AMD", "TSLA", "MSFT"]
    test_position_sizing()
    for sym in symbols:
        run_full_test(sym)
        time.sleep(1)
