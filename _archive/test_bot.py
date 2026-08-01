"""
Circuit Full Bot Test
Tests swing trades, ETF options, wheel, and ETF accumulation.
No day trades.

Run: python3 test_bot.py
"""

import requests
import json
import os
import time
from datetime import datetime
from auth import get_valid_token
from ledger import load_ledger

BASE_URL   = "https://api.schwabapi.com/trader/v1"
MARKET_URL = "https://api.schwabapi.com/marketdata/v1"

def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}

def get_account():
    resp = requests.get(f"{BASE_URL}/accounts/accountNumbers", headers=headers(), timeout=10)
    return resp.json()[0]["hashValue"] if resp.ok else ""

def get_positions(encrypted):
    resp = requests.get(f"{BASE_URL}/accounts/{encrypted}?fields=positions", headers=headers(), timeout=15)
    if resp.ok:
        return resp.json()["securitiesAccount"].get("positions", [])
    return []

def get_cash(encrypted):
    resp = requests.get(f"{BASE_URL}/accounts/{encrypted}?fields=positions", headers=headers(), timeout=15)
    if resp.ok:
        return resp.json()["securitiesAccount"]["currentBalances"]["cashBalance"]
    return 0

def get_candles(symbol, period=5, frequency=30):
    resp = requests.get(
        f"{MARKET_URL}/pricehistory", headers=headers(),
        params={"symbol": symbol, "periodType": "day", "period": period,
                "frequencyType": "minute", "frequency": frequency,
                "needExtendedHoursData": False},
        timeout=15
    )
    return resp.json().get("candles", []) if resp.ok else []

def get_daily_candles(symbol):
    resp = requests.get(
        f"{MARKET_URL}/pricehistory", headers=headers(),
        params={"symbol": symbol, "periodType": "month", "period": 1,
                "frequencyType": "daily", "frequency": 1},
        timeout=15
    )
    return resp.json().get("candles", []) if resp.ok else []

def get_option_chain(symbol, option_type="PUT"):
    resp = requests.get(
        f"{MARKET_URL}/chains", headers=headers(),
        params={"symbol": symbol, "contractType": option_type,
                "strikeCount": 10, "strategy": "SINGLE"},
        timeout=15
    )
    return resp.json() if resp.ok else {}


# ── TEST 1: SWING TRADES ──────────────────────────────────────────────────────

def test_swing_signals(symbols=None):
    print(f"\n{'='*55}")
    print("TEST 1 — SWING TRADE SIGNALS")
    print(f"{'='*55}")

    if symbols is None:
        # Get live movers
        resp = requests.get(
            f"{MARKET_URL}/movers/NASDAQ", headers=headers(),
            params={"sort": "VOLUME", "frequency": 1}, timeout=10
        )
        if resp.ok:
            movers = resp.json().get("screeners", [])[:10]
            symbols = [m["symbol"] for m in movers]
        else:
            symbols = ["AAPL", "NVDA", "AMD", "TSLA", "MSFT"]

    print(f"Scanning {len(symbols)} symbols for swing setups...")
    qualified = []

    for sym in symbols:
        # Daily candle for bias
        daily = get_daily_candles(sym)
        if len(daily) < 20:
            continue

        closes_d = [c["close"] for c in daily]
        ma20_d   = sum(closes_d[-20:]) / 20
        ma10_d   = sum(closes_d[-10:]) / 10
        price    = closes_d[-1]
        daily_up = price > ma20_d and price > ma10_d

        # 30m candles for entry
        candles_30 = get_candles(sym, 5, 30)
        if len(candles_30) < 20:
            continue

        closes_30 = [c["close"] for c in candles_30]
        ma20_30   = sum(closes_30[-20:]) / 20
        price_30  = closes_30[-1]
        aligned   = price_30 > ma20_30

        # Candle strength
        c     = candles_30[-1]
        rng   = c["high"] - c["low"]
        body  = abs(c["close"] - c["open"])
        strength = body / rng if rng > 0 else 0

        # Volume spike
        vols    = [c["volume"] for c in candles_30[-21:]]
        avg_vol = sum(vols[:-1]) / 20 if len(vols) > 1 else 0
        vol_spike = candles_30[-1]["volume"] > avg_vol * 1.3 if avg_vol > 0 else False

        score = 0
        if daily_up:   score += 2
        if aligned:    score += 2
        if strength > 0.35: score += 2
        if vol_spike:  score += 2

        status = "✅ QUALIFY" if score >= 6 else "❌ skip"
        print(f"  {sym}: score={score}/8 daily={'UP' if daily_up else 'DOWN'} strength={strength:.2f} vol={'spike' if vol_spike else 'low'} → {status}")

        if score >= 6:
            qualified.append({"symbol": sym, "score": score, "price": price})

        time.sleep(0.3)

    print(f"\nQualified for swing: {len(qualified)} symbols")
    for q in qualified:
        print(f"  {q['symbol']} @ ${q['price']:.2f} (score {q['score']}/8)")


# ── TEST 2: ETF OPTIONS / WHEEL ───────────────────────────────────────────────

def test_etf_options(encrypted):
    print(f"\n{'='*55}")
    print("TEST 2 — ETF OPTIONS / WHEEL")
    print(f"{'='*55}")

    positions = get_positions(encrypted)
    cash      = get_cash(encrypted)
    ledger    = load_ledger()
    wheel_state = ledger.get("wheel_state", {})

    etf_universe = {
        "QYLD": {"min_premium": 0.20, "target_delta": 0.25},
        "RYLD": {"min_premium": 0.20, "target_delta": 0.25},
        "SCHA": {"min_premium": 0.20, "target_delta": 0.25},
        "SCHB": {"min_premium": 0.20, "target_delta": 0.25},
        "SCHG": {"min_premium": 0.20, "target_delta": 0.25},
        "SCHD": {"min_premium": 0.25, "target_delta": 0.25},
        "JEPI": {"min_premium": 0.30, "target_delta": 0.20},
    }

    # Current ETF positions
    etf_shares = {}
    for p in positions:
        sym = p["instrument"]["symbol"]
        if sym in etf_universe:
            etf_shares[sym] = p.get("longQuantity", 0)

    print(f"Cash available: ${cash:,.2f}")
    print(f"\nETF Positions:")
    for sym, cfg in etf_universe.items():
        shares = etf_shares.get(sym, 0)
        phase  = wheel_state.get(sym, {}).get("phase", "none")
        status = "✅ CALL phase" if shares >= 100 else f"PUT phase ({shares:.0f}/100 shares)"
        print(f"  {sym}: {shares:.0f} shares | {status}")

    print(f"\nScanning options chains...")
    opportunities = []

    for sym, cfg in etf_universe.items():
        shares = etf_shares.get(sym, 0)

        if shares >= 100:
            # Test covered call
            chain = get_option_chain(sym, "CALL")
            underlying = chain.get("underlyingPrice", 0)
            call_map   = chain.get("callExpDateMap", {})
            best_call  = None

            for expiry, strikes in call_map.items():
                try:
                    dte = int(expiry.split(":")[1])
                except Exception:
                    continue
                if not (21 <= dte <= 45):
                    continue
                for strike_str, opts in strikes.items():
                    strike = float(strike_str)
                    if not (underlying * 1.01 <= strike <= underlying * 1.06):
                        continue
                    opt   = opts[0] if opts else None
                    if not opt:
                        continue
                    delta = abs(opt.get("delta", 0) or 0)
                    if not (0.15 <= delta <= 0.35):
                        continue
                    bid   = opt.get("bid", 0)
                    ask   = opt.get("ask", 0)
                    prem  = (bid + ask) / 2
                    if prem >= cfg["min_premium"] and bid > 0:
                        ann = (prem / strike) * (365 / dte) * 100
                        if best_call is None or ann > best_call["ann"]:
                            best_call = {"strike": strike, "dte": dte, "prem": prem,
                                        "delta": delta, "ann": round(ann, 1)}

            if best_call:
                net = best_call["prem"] * 100 - 0.65
                print(f"  ✅ {sym} COVERED CALL: strike=${best_call['strike']} dte={best_call['dte']}d prem=${best_call['prem']:.2f} delta={best_call['delta']:.2f} yield={best_call['ann']}%/yr net=${net:.2f}")
                opportunities.append({"sym": sym, "type": "call", **best_call})
            else:
                print(f"  ❌ {sym}: no qualifying call found")

        else:
            # Test cash secured put
            put_collateral = 0
            chain = get_option_chain(sym, "PUT")
            underlying = chain.get("underlyingPrice", 0)
            put_map    = chain.get("putExpDateMap", {})
            best_put   = None

            for expiry, strikes in put_map.items():
                try:
                    dte = int(expiry.split(":")[1])
                except Exception:
                    continue
                if not (21 <= dte <= 45):
                    continue
                for strike_str, opts in strikes.items():
                    strike = float(strike_str)
                    collat = strike * 100
                    if collat > cash:
                        continue
                    if not (underlying * 0.93 <= strike <= underlying * 0.99):
                        continue
                    opt   = opts[0] if opts else None
                    if not opt:
                        continue
                    delta = abs(opt.get("delta", 0) or 0)
                    if not (0.15 <= delta <= 0.35):
                        continue
                    bid   = opt.get("bid", 0)
                    ask   = opt.get("ask", 0)
                    prem  = (bid + ask) / 2
                    if prem >= cfg["min_premium"] and bid > 0:
                        ann = (prem / strike) * (365 / dte) * 100
                        if best_put is None or ann > best_put["ann"]:
                            best_put = {"strike": strike, "dte": dte, "prem": prem,
                                       "delta": delta, "ann": round(ann, 1),
                                       "collat": collat}

            if best_put:
                net = best_put["prem"] * 100 - 0.65
                print(f"  ✅ {sym} CASH PUT: strike=${best_put['strike']} dte={best_put['dte']}d prem=${best_put['prem']:.2f} delta={best_put['delta']:.2f} yield={best_put['ann']}%/yr collat=${best_put['collat']:,.0f} net=${net:.2f}")
                opportunities.append({"sym": sym, "type": "put", **best_put})
            else:
                cash_needed = underlying * 0.97 * 100 if underlying > 0 else 0
                print(f"  ❌ {sym}: no qualifying put (need ${cash_needed:,.0f} collateral, have ${cash:,.0f})")

        time.sleep(0.5)

    print(f"\nTotal ETF options opportunities: {len(opportunities)}")
    if opportunities:
        print("Best opportunities:")
        for o in sorted(opportunities, key=lambda x: x.get("ann", 0), reverse=True)[:3]:
            print(f"  {o['sym']} {o['type'].upper()} @ {o['ann']}%/yr")


# ── TEST 3: ETF ACCUMULATION ─────────────────────────────────────────────────

def test_etf_accumulation(encrypted):
    print(f"\n{'='*55}")
    print("TEST 3 — ETF ACCUMULATION / ROADMAP")
    print(f"{'='*55}")

    ledger   = load_ledger()
    capital  = ledger.get("trading_capital", 2167)
    etf_b    = ledger.get("etf_bucket", 0)
    avg_daily = 79.98

    etf_targets = {
        "QYLD": {"price": 16,  "monthly_prem": 20},
        "RYLD": {"price": 18,  "monthly_prem": 22},
        "SCHA": {"price": 25,  "monthly_prem": 18},
        "SCHB": {"price": 29,  "monthly_prem": 25},
        "SCHG": {"price": 30,  "monthly_prem": 25},
        "SCHD": {"price": 31,  "monthly_prem": 30},
        "JEPI": {"price": 60,  "monthly_prem": 50},
    }

    positions = get_positions(encrypted)
    etf_shares = {}
    for p in positions:
        sym = p["instrument"]["symbol"]
        if sym in etf_targets:
            etf_shares[sym] = p.get("longQuantity", 0)

    print(f"Capital: ${capital:,.0f} | ETF bucket: ${etf_b:.2f} | Avg daily: ${avg_daily:.2f}")
    print(f"\nETF Roadmap:")

    total_monthly = 0
    for sym, cfg in etf_targets.items():
        shares    = etf_shares.get(sym, 0)
        needed    = max(0, 100 - shares)
        cost      = needed * cfg["price"]
        pct       = shares / 100 * 100
        # ETA based on ETF sweep (60% of daily profits)
        daily_etf = avg_daily * 0.60
        days_eta  = int(cost / (daily_etf * cfg["price"] / cfg["price"])) if cost > 0 else 0
        days_eta  = int(cost / max(daily_etf, 1))

        if shares >= 100:
            monthly = cfg["monthly_prem"]
            total_monthly += monthly
            print(f"  ✅ {sym}: {shares:.0f}/100 UNLOCKED → ${monthly}/mo premium")
        else:
            print(f"  📊 {sym}: {shares:.0f}/100 ({pct:.0f}%) → need ${cost:,.0f} → ~{days_eta}d")

    print(f"\nTotal monthly premium now: ${total_monthly}/mo")
    print(f"Full potential (all unlocked): ${sum(c['monthly_prem'] for c in etf_targets.values())}/mo")

    # SGOV check
    print(f"\nSGOV Parking Check:")
    tax_owed = ledger.get("ytd_tax_owed", 0)
    bot_b    = ledger.get("bot_bucket", 0)
    cash     = get_cash(encrypted)
    tax_park = min(tax_owed, cash * 0.30) if tax_owed > 100 else 0
    bot_park = max(bot_b - 200, 0) if bot_b > 300 else 0
    total_park = tax_park + bot_park + (etf_b if etf_b < 50 else 0)
    print(f"  Tax owed: ${tax_owed:.2f} | Would park: ${tax_park:.2f}")
    print(f"  Bot excess: ${bot_b:.2f} | Would park: ${bot_park:.2f}")
    print(f"  ETF bucket: ${etf_b:.2f}")
    if total_park >= 100:
        print(f"  ✅ Would park ${total_park:.2f} in SGOV (~5%/yr)")
    else:
        print(f"  ❌ Not enough to park (${total_park:.2f} < $100 minimum)")


# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n[ CIRCUIT ] FULL BOT TEST")
    print("Testing: Swing | ETF Options | ETF Accumulation")
    print("Day trades: SKIPPED")
    print(f"Time: {datetime.now().strftime('%H:%M ET')}\n")

    encrypted = get_account()
    if not encrypted:
        print("Error: Could not get account")
        exit(1)

    # Test 1: Swing signals
    test_swing_signals()

    # Test 2: ETF options / wheel
    test_etf_options(encrypted)

    # Test 3: ETF accumulation
    test_etf_accumulation(encrypted)

    print(f"\n{'='*55}")
    print("TEST COMPLETE")
    print(f"{'='*55}\n")
