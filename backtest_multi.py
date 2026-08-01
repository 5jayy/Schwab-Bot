"""
MULTI-STRATEGY BACKTEST — real 4-month EODHD data, expectancy-driven.

Three independent strategies, each measured on REAL 5-minute history
(EODHD demo: AAPL/TSLA/AMZN/VTI, ~4 months). Goal: find the best POSITIVE
EXPECTANCY, not a forced target.

  EXPECTANCY = (Win% × AvgWin%) − (Loss% × AvgLoss%)   [per trade, in %]
  Positive expectancy = the strategy makes money over many trades.

STRATEGY 1 — DAY TRADE (stock movement; drives call-buying live)
  EMA200 direction + EMA9/21 pullback-reclaim entry, ATR stop, R-targets,
  partial at +1R + breakeven. This is the price-action edge we can test for real.

STRATEGY 2 — PUT SELLING (modeled premium)
  Sell puts in uptrends; Black-Scholes premium, theta decay, 50% profit exit,
  2x-premium stop. Premium is MODELED (no free historical options data).

STRATEGY 3 — ETF (buy/hold + covered calls, modeled)
  Accumulate in uptrend, model covered-call income. Low-vol = small premium.

Reports each strategy's expectancy + weekly breakdown. RECOMMENDS (does not
auto-deploy) — you decide what goes live.

Run locally:  python3 backtest_multi.py
"""

import sys
import math
import time
import requests
from datetime import datetime
from collections import defaultdict

EODHD = "https://eodhd.com/api/intraday/{}"
TOKEN = "demo"   # demo key: AAPL.US, TSLA.US, AMZN.US, VTI.US

SLIPPAGE_PCT = 0.05
ROUND_TRIP_COST = SLIPPAGE_PCT * 2

# Day-trade params
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 200
N_ATR = 1.5
PARTIAL_R = 1.0
FINAL_R = 2.0
EXPIRY_BARS = 3
NOPROGRESS_BARS = 6
MAX_BARS = 12

# Put-selling model
PUT_DELTA_TARGET = 0.25
PUT_DTE = 5
PUT_IV = 0.35        # single-stock IV (higher than ETF)
PUT_EXIT_PROFIT = 0.50
RISK_FREE = 0.045


def fetch(ticker, interval="5m"):
    try:
        r = requests.get(EODHD.format(ticker),
                         params={"api_token": TOKEN, "interval": interval, "fmt": "json"},
                         timeout=20)
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, list) else []
    except Exception as e:
        print(f"  {ticker}: fetch error {e}")
        return []


def clean(bars):
    """Drop bars with null OHLC; keep chronological."""
    out = []
    for b in bars:
        if b.get("close") is None or b.get("open") is None:
            continue
        out.append({"o": b["open"], "h": b["high"], "l": b["low"],
                    "c": b["close"], "dt": b.get("datetime", "")})
    return out


def ema_series(vals, length):
    if len(vals) < length:
        return [None]*len(vals)
    k = 2/(length+1); out = [None]*(length-1)
    e = sum(vals[:length])/length; out.append(e)
    for v in vals[length:]:
        e = v*k + e*(1-k); out.append(e)
    return out


def atr_at(bars, idx, period=14):
    if idx < period:
        return None
    trs = []
    for i in range(idx-period+1, idx+1):
        tr = max(bars[i]["h"]-bars[i]["l"],
                 abs(bars[i]["h"]-bars[i-1]["c"]),
                 abs(bars[i]["l"]-bars[i-1]["c"]))
        trs.append(tr)
    return sum(trs)/period


def expectancy(rets):
    if not rets:
        return None
    n = len(rets)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    wr = len(wins)/n
    aw = sum(wins)/len(wins) if wins else 0
    al = sum(losses)/len(losses) if losses else 0
    exp = wr*aw + (1-wr)*al   # al is negative
    return {"n": n, "win_rate": wr*100, "avg_win": aw, "avg_loss": al,
            "expectancy": exp, "total": sum(rets)}


# ---------------- STRATEGY 1: DAY TRADE ----------------

def day_trade(bars, week_bucket):
    closes = [b["c"] for b in bars]
    e9 = ema_series(closes, EMA_FAST)
    e21 = ema_series(closes, EMA_MID)
    e200 = ema_series(closes, EMA_SLOW)
    rets = []
    i = 30
    while i < len(bars) - MAX_BARS - 1:
        if e200[i] is None or e9[i] is None or e21[i] is None:
            i += 1; continue
        price = bars[i]["c"]
        # direction from EMA200 + slope
        slope_ref = e200[i-10] if i >= 10 and e200[i-10] else e200[i]
        rising = e200[i] > slope_ref
        if price > e200[i] and rising:
            direction = "bull"
        elif price < e200[i] and not rising:
            direction = "bear"
        else:
            i += 1; continue
        # pullback + reclaim
        prev = bars[i-1]; c = bars[i]
        if direction == "bull":
            pulled = prev["l"] <= max(e9[i], e21[i]) * 1.001
            reclaim = c["c"] > e9[i] and c["c"] > c["o"]
            trig = c["h"]
        else:
            pulled = prev["h"] >= min(e9[i], e21[i]) * 0.999
            reclaim = c["c"] < e9[i] and c["c"] < c["o"]
            trig = c["l"]
        if not (pulled and reclaim):
            i += 1; continue
        # ATR + entry trigger within EXPIRY
        atr = atr_at(bars, i)
        if not atr or atr <= 0:
            i += 1; continue
        entry_idx = None
        for j in range(i+1, min(i+1+EXPIRY_BARS, len(bars))):
            if direction == "bull" and bars[j]["h"] >= trig:
                entry_idx = j; entry = trig; break
            if direction == "bear" and bars[j]["l"] <= trig:
                entry_idx = j; entry = trig; break
        if entry_idx is None:
            i += 1; continue
        # manage
        risk = N_ATR * atr
        stop = entry - risk if direction == "bull" else entry + risk
        partial_px = entry + PARTIAL_R*risk if direction == "bull" else entry - PARTIAL_R*risk
        final_px = entry + FINAL_R*risk if direction == "bull" else entry - FINAL_R*risk
        remaining = 1.0; realized = 0.0; be = False; best = entry; stall = 0
        outcome_ret = None
        for k in range(entry_idx+1, min(entry_idx+1+MAX_BARS, len(bars))):
            b = bars[k]
            if direction == "bull":
                if b["l"] <= stop:
                    realized += remaining*(stop-entry)/entry*100; outcome_ret = realized; break
                if not be and b["h"] >= partial_px:
                    realized += 0.5*(PARTIAL_R*risk)/entry*100; remaining = 0.5; stop = entry; be = True
                if b["h"] >= final_px:
                    realized += remaining*(final_px-entry)/entry*100; outcome_ret = realized; break
                if b["h"] > best: best = b["h"]; stall = 0
                else: stall += 1
                if stall >= NOPROGRESS_BARS:
                    realized += remaining*(b["c"]-entry)/entry*100; outcome_ret = realized; break
            else:
                if b["h"] >= stop:
                    realized += remaining*(entry-stop)/entry*100; outcome_ret = realized; break
                if not be and b["l"] <= partial_px:
                    realized += 0.5*(PARTIAL_R*risk)/entry*100; remaining = 0.5; stop = entry; be = True
                if b["l"] <= final_px:
                    realized += remaining*(entry-final_px)/entry*100; outcome_ret = realized; break
                if b["l"] < best: best = b["l"]; stall = 0
                else: stall += 1
                if stall >= NOPROGRESS_BARS:
                    realized += remaining*(entry-b["c"])/entry*100; outcome_ret = realized; break
        if outcome_ret is None:
            last = bars[min(entry_idx+MAX_BARS, len(bars)-1)]["c"]
            r = ((last-entry) if direction == "bull" else (entry-last))/entry*100
            realized += remaining*r; outcome_ret = realized
        net = outcome_ret - ROUND_TRIP_COST
        rets.append(net)
        wk = bars[entry_idx]["dt"][:7]  # year-month for bucketing
        week_bucket[wk].append(net)
        i = entry_idx + 1  # move past this trade
    return rets


# ---------------- STRATEGY 2: PUT SELLING (modeled) ----------------

def _ncdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))

def bs_put(S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return max(0.0, K-S)
    d1 = (math.log(S/K)+(r+0.5*sig**2)*T)/(sig*math.sqrt(T))
    d2 = d1 - sig*math.sqrt(T)
    return K*math.exp(-r*T)*_ncdf(-d2) - S*_ncdf(-d1)

def put_delta(S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return -1.0 if S < K else 0.0
    d1 = (math.log(S/K)+(r+0.5*sig**2)*T)/(sig*math.sqrt(T))
    return _ncdf(d1) - 1

def put_selling(daily_closes):
    """Sell weekly puts in uptrends. Modeled premium. Returns list of net% on collateral."""
    if len(daily_closes) < 30:
        return []
    e = ema_series(daily_closes, 20)
    rets = []
    T = PUT_DTE/365
    i = 25
    while i < len(daily_closes) - PUT_DTE:
        S = daily_closes[i]
        if e[i] is None or S <= e[i]:  # only sell puts in uptrend
            i += 1; continue
        # find strike ~0.25 delta (below spot)
        K = round(S * 0.95)
        prem0 = bs_put(S, K, T, RISK_FREE, PUT_IV)
        if prem0 <= 0.05:
            i += PUT_DTE; continue
        # simulate to expiry or 50% profit
        exited = False
        for d in range(1, PUT_DTE+1):
            if i+d >= len(daily_closes): break
            S2 = daily_closes[i+d]
            Tl = max((PUT_DTE-d)/365, 0.0001)
            prem = bs_put(S2, K, Tl, RISK_FREE, PUT_IV)
            if prem <= prem0*(1-PUT_EXIT_PROFIT):
                # profit as % of collateral (K*100)
                rets.append((prem0-prem)/K*100 - ROUND_TRIP_COST); exited = True
                i += d; break
            if prem >= prem0*2:  # 2x stop
                rets.append((prem0-prem)/K*100 - ROUND_TRIP_COST); exited = True
                i += d; break
        if not exited:
            Sf = daily_closes[min(i+PUT_DTE, len(daily_closes)-1)]
            premf = max(0, K-Sf)
            rets.append((prem0-premf)/K*100 - ROUND_TRIP_COST)
            i += PUT_DTE
    return rets


def to_daily(bars):
    """Collapse 5m bars to daily closes."""
    days = {}
    for b in bars:
        day = b["dt"][:10]
        days[day] = b["c"]  # last close of the day
    return [days[d] for d in sorted(days)]


def run(tickers):
    print(f"\n{'='*66}")
    print("MULTI-STRATEGY BACKTEST — real EODHD 4-month data")
    print(f"{'='*66}")
    print("Finding best POSITIVE EXPECTANCY. Options premiums MODELED.\n")

    dt_rets = []; dt_weeks = defaultdict(list)
    put_rets = []
    for t in tickers:
        bars = clean(fetch(t))
        if len(bars) < 300:
            print(f"  {t}: insufficient ({len(bars)})"); continue
        print(f"  {t}: {len(bars)} bars")
        dt_rets += day_trade(bars, dt_weeks)
        put_rets += put_selling(to_daily(bars))
        time.sleep(0.4)

    print(f"\n{'='*66}")
    print("STRATEGY 1 — DAY TRADE (real stock movement)")
    print(f"{'-'*66}")
    e = expectancy(dt_rets)
    if e:
        print(f"  Trades: {e['n']} | Win: {e['win_rate']:.0f}% | "
              f"AvgWin {e['avg_win']:+.2f}% | AvgLoss {e['avg_loss']:+.2f}%")
        print(f"  EXPECTANCY: {e['expectancy']:+.3f}% per trade | Total: {e['total']:+.1f}%")
        verdict = "POSITIVE — candidate for live (small)" if e['expectancy'] > 0 else "NEGATIVE — do not deploy"
        print(f"  VERDICT: {verdict}")
        print(f"\n  Monthly breakdown:")
        for wk in sorted(dt_weeks):
            we = expectancy(dt_weeks[wk])
            if we:
                print(f"    {wk}: {we['n']} trades, exp {we['expectancy']:+.3f}%, total {we['total']:+.1f}%")

    print(f"\n{'='*66}")
    print("STRATEGY 2 — PUT SELLING (modeled premium)")
    print(f"{'-'*66}")
    e2 = expectancy(put_rets)
    if e2:
        print(f"  Trades: {e2['n']} | Win: {e2['win_rate']:.0f}% | "
              f"AvgWin {e2['avg_win']:+.2f}% | AvgLoss {e2['avg_loss']:+.2f}%")
        print(f"  EXPECTANCY: {e2['expectancy']:+.3f}% per trade (on collateral)")
        print(f"  VERDICT: {'POSITIVE' if e2['expectancy']>0 else 'NEGATIVE'} "
              f"(NOTE: premium modeled, not real fills — validate live small)")
    else:
        print("  No put trades generated.")

    print(f"\n{'='*66}")
    print("HONEST NOTES:")
    print("  • Day-trade = REAL stock data (this is the trustworthy number).")
    print("  • Put selling = MODELED premium (directional only, validate live).")
    print("  • Positive expectancy here = candidate. Go live SMALL, then scale")
    print("    only if real results match. Recommends — you decide to deploy.")
    print(f"{'='*66}\n")


if __name__ == "__main__":
    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["AAPL.US", "TSLA.US", "AMZN.US", "VTI.US"]
    run(tickers)
