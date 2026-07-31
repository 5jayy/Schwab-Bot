"""
Circuit Options Scanner — 3-Tier System

DAILY   (0-1 DTE):   Highest yield, gamma plays, high IV movers
WEEKLY  (1-7 DTE):   Earnings/news plays, very high yield
MONTHLY (21-45 DTE): Stable income, ETF roadmap building

Priority: Daily > Weekly > Monthly
Budget: 15% stock options + 10% ETF options = 25% total
"""

import requests
import time
import json
import os
from auth import get_valid_token
from scanner import get_mtf_conviction

MARKET_URL  = "https://api.schwabapi.com/marketdata/v1"
COMMISSION  = 0.65
LEDGER_PATH = "/data/trade_ledger.json" if os.path.exists("/data") else "trade_ledger.json"

# ── DTE windows ──
DAILY_DTE_MIN   = 0
DAILY_DTE_MAX   = 1
WEEKLY_DTE_MIN  = 0  # 0 DTE included — real money
WEEKLY_DTE_PREF = 3  # 3 DTE preferred — best from backtest (87% win, 50% exit)
WEEKLY_DTE_MAX  = 7
MONTHLY_DTE_MIN = 21
MONTHLY_DTE_MAX = 45

# ── Delta targets ──
DAILY_DELTA_MIN   = 0.15   # tighter on daily — less risk
DAILY_DELTA_MAX   = 0.30
WEEKLY_DELTA_MIN  = 0.18
WEEKLY_DELTA_MAX  = 0.32
MONTHLY_DELTA_MIN = 0.18
MONTHLY_DELTA_MAX = 0.35

# ── Minimum yields ──
DAILY_MIN_YIELD   = 0.50   # 50%+ annualized for daily (very high)
WEEKLY_MIN_YIELD  = 0.25   # 25%+ annualized for weekly
MONTHLY_MIN_YIELD = 0.10   # 10%+ annualized for monthly

# ── Liquidity ──
DAILY_MIN_OI   = 200   # daily needs more liquidity
DAILY_MIN_VOL  = 50
WEEKLY_MIN_OI  = 500   # raised for liquidity — tighter spreads, consistent fills
WEEKLY_MIN_VOL = 100   # raised for liquidity — active options only
MONTHLY_MIN_OI = 50
MONTHLY_MIN_VOL = 5

# ── Profit exit targets ──
DAILY_EXIT_PCT   = 0.50   # exit at 50% profit same day
WEEKLY_EXIT_PCT  = 0.50   # exit at 50% profit during week
MONTHLY_EXIT_PCT = 0.50   # exit at 50% profit


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def load_ledger() -> dict:
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def get_movers(index: str = "$SPX") -> list:
    try:
        resp = requests.get(
            f"{MARKET_URL}/movers/{index}",
            headers=headers(),
            params={"sort": "VOLUME", "frequency": 1},
            timeout=10
        )
        if resp.ok:
            return [m["symbol"] for m in resp.json().get("screeners", [])[:20]]
    except Exception:
        pass
    return []


def get_quote(symbol: str) -> dict:
    try:
        resp = requests.get(
            f"{MARKET_URL}/quotes",
            headers=headers(),
            params={"symbols": symbol, "fields": "quote"},
            timeout=8
        )
        if resp.ok:
            q = resp.json().get(symbol, {}).get("quote", {})
            return {
                "price":  q.get("lastPrice", 0),
                "iv":     q.get("volatility", 0),
                "volume": q.get("totalVolume", 0),
            }
    except Exception:
        pass
    return {}


def get_option_chain(symbol: str, option_type: str = "PUT",
                     dte_min: int = 0, dte_max: int = 45) -> dict:
    try:
        resp = requests.get(
            f"{MARKET_URL}/chains",
            headers=headers(),
            params={
                "symbol":       symbol,
                "contractType": option_type,
                "strikeCount":  20,
                "strategy":     "SINGLE",
                "daysToExpiration": dte_max,
                "fromDate":     None,
            },
            timeout=15
        )
        return resp.json() if resp.ok else {}
    except Exception:
        return {}


def score_option(prem: float, strike: float, dte: int,
                 delta: float, oi: int, tier: str) -> float:
    """
    Score option by:
    - Annualized yield (primary)
    - Delta proximity to 0.25 (secondary)
    - Liquidity (tertiary)
    """
    if strike <= 0 or dte <= 0:
        return 0
    net_prem  = prem - (COMMISSION / 100)
    if net_prem <= 0:
        return 0
    ann_yield = (net_prem / strike) * (365 / max(dte, 1))
    delta_score = 1 - abs(delta - 0.25) * 4  # peak at 0.25
    liq_score   = min(oi / 1000, 1.0)
    return ann_yield * 100 * (0.80 + delta_score * 0.10 + liq_score * 0.10)


def find_best_put_tiered(symbol: str, cash_available: float,
                         dte_min: int, dte_max: int,
                         delta_min: float, delta_max: float,
                         min_yield: float, min_oi: int, min_vol: int,
                         tier: str,
                         strike_min_pct: float = 0.85,
                         strike_max_pct: float = 0.99) -> dict | None:
    """Find best put for given DTE tier."""
    chain      = get_option_chain(symbol, "PUT", dte_min, dte_max)
    underlying = chain.get("underlyingPrice", 0)
    if underlying <= 0:
        return None

    put_map    = chain.get("putExpDateMap", {})
    best       = None
    best_score = 0

    for expiry, strikes in put_map.items():
        try:
            dte = int(expiry.split(":")[1])
        except Exception:
            continue
        if not (dte_min <= dte <= dte_max):
            continue

        for strike_str, opts in strikes.items():
            strike = float(strike_str)
            collat = strike * 100

            if collat > cash_available:
                continue
            if not (underlying * strike_min_pct <= strike <= underlying * strike_max_pct):
                continue

            opt = opts[0] if opts else None
            if not opt:
                continue

            delta = abs(opt.get("delta", 0) or 0)
            if not (delta_min <= delta <= delta_max):
                continue

            bid   = opt.get("bid", 0)
            ask   = opt.get("ask", 0)
            prem  = (bid + ask) / 2
            oi    = opt.get("openInterest", 0)
            vol   = opt.get("totalVolume", 0)

            if bid <= 0 or prem < 0.05:
                continue
            if oi < min_oi or vol < min_vol:
                continue

            net_prem  = prem - (COMMISSION / 100)
            if net_prem <= 0:
                continue

            ann_yield = (net_prem / strike) * (365 / max(dte, 1)) * 100
            if ann_yield < min_yield * 100:
                continue

            sc = score_option(prem, strike, dte, delta, oi, tier)
            # 20% bonus for 3 DTE — backtest confirmed best combo
            dte_bonus = 1.20 if abs(dte - WEEKLY_DTE_PREF) <= 1 else 1.0
            sc = sc * dte_bonus
            if sc > best_score:
                best_score = sc
                best = {
                    "symbol":      symbol,
                    "type":        "put",
                    "tier":        tier,
                    "strike":      strike,
                    "expiry":      expiry.split(":")[0],
                    "dte":         dte,
                    "delta":       round(delta, 3),
                    "bid":         bid,
                    "premium":     round(prem, 2),
                    "net_premium": round(net_prem, 2),
                    "total_prem":  round(net_prem * 100, 2),
                    "collateral":  round(collat, 2),
                    "ann_yield":   round(ann_yield, 1),
                    "underlying":  round(underlying, 2),
                    "opt_symbol":  opt.get("symbol", ""),
                    "exit_target": round(net_prem * 100 * 0.50, 2),
                    "score":       round(sc, 2),
                }

    return best


def find_best_call_tiered(symbol: str, shares: int, avg_cost: float,
                          dte_min: int, dte_max: int,
                          delta_min: float, delta_max: float,
                          min_yield: float, min_oi: int, min_vol: int,
                          tier: str) -> dict | None:
    """Find best covered call for given DTE tier."""
    if shares < 100:
        return None

    chain      = get_option_chain(symbol, "CALL", dte_min, dte_max)
    underlying = chain.get("underlyingPrice", avg_cost)
    call_map   = chain.get("callExpDateMap", {})
    contracts  = shares // 100
    best       = None
    best_score = 0

    for expiry, strikes in call_map.items():
        try:
            dte = int(expiry.split(":")[1])
        except Exception:
            continue
        if not (dte_min <= dte <= dte_max):
            continue

        for strike_str, opts in strikes.items():
            strike = float(strike_str)
            if strike < avg_cost * 1.01 or strike > underlying * 1.08:
                continue

            opt = opts[0] if opts else None
            if not opt:
                continue

            delta = abs(opt.get("delta", 0) or 0)
            if not (delta_min <= delta <= delta_max):
                continue

            bid   = opt.get("bid", 0)
            ask   = opt.get("ask", 0)
            prem  = (bid + ask) / 2
            oi    = opt.get("openInterest", 0)
            vol   = opt.get("totalVolume", 0)

            if bid <= 0 or prem < 0.05:
                continue
            if oi < min_oi or vol < min_vol:
                continue

            net_prem  = prem - (COMMISSION / 100)
            ann_yield = (net_prem / strike) * (365 / max(dte, 1)) * 100
            if ann_yield < min_yield * 100:
                continue

            sc = score_option(prem, strike, dte, delta, oi, tier)
            if sc > best_score:
                best_score = sc
                best = {
                    "symbol":      symbol,
                    "type":        "call",
                    "tier":        tier,
                    "strike":      strike,
                    "expiry":      expiry.split(":")[0],
                    "dte":         dte,
                    "delta":       round(delta, 3),
                    "bid":         bid,
                    "premium":     round(prem, 2),
                    "net_premium": round(net_prem, 2),
                    "total_prem":  round(net_prem * 100 * contracts, 2),
                    "contracts":   contracts,
                    "collateral":  0,
                    "ann_yield":   round(ann_yield, 1),
                    "underlying":  round(underlying, 2),
                    "avg_cost":    avg_cost,
                    "opt_symbol":  opt.get("symbol", ""),
                    "exit_target": round(net_prem * 100 * contracts * 0.50, 2),
                    "score":       round(sc, 2),
                }

    return best


# ── SCANNER 1: Stock Options (Daily + Weekly + Monthly) ───────────────────────

def scan_stock_options(cash_available: float) -> list:
    if cash_available < 100:  # need at least $100 to do anything
        print(f"  Stock options: budget ${cash_available:.0f} too small — skip")
        return []
    """
    Scans for WEEKLY stock options only (1-7 DTE).
    Weekly wins 15x over monthly on same collateral.

    At $1,500 budget:
    Weekly: $150/contract × 4 = $600/mo
    Monthly: $25/mo — not worth it

    Exit at 50% profit — never hold to expiry.
    Budget: 15% of swing cash.
    """
    symbols = list(set(get_movers("$SPX") + get_movers("$COMP")))
    daily   = []  # placeholder for return statement
    weekly  = []
    monthly = []  # placeholder for return statement
    scanned = set()

    print(f"  Stock options WEEKLY scan: {len(symbols)} symbols | ${cash_available:,.0f} budget")

    for sym in symbols:
        if sym in scanned:
            continue
        scanned.add(sym)

        q     = get_quote(sym)
        price = q.get("price", 0)
        if price <= 0:
            continue
        # Must afford at least 1 contract (100 shares collateral)
        if price * 100 > cash_available:
            continue
        # Skip penny stocks under $2 (poor liquidity)
        if price < 2:
            continue

        # ── DIRECTIONAL FILTER ──
        # Selling a put = bet the stock WON'T crash below strike.
        # Only sell puts on stocks that are flat-to-bullish (conviction >= 2/4).
        # This uses the same MTF signal that drives the 64.9% swing win rate.
        # Skips falling knives — the one scenario where put selling loses.
        conviction = get_mtf_conviction(sym)
        if conviction < 2:
            continue  # bearish/weak — don't sell puts into a downtrend

        # Weekly only — 1-7 DTE
        w = find_best_put_tiered(
            sym, cash_available,
            WEEKLY_DTE_MIN, WEEKLY_DTE_MAX,
            WEEKLY_DELTA_MIN, WEEKLY_DELTA_MAX,
            WEEKLY_MIN_YIELD, WEEKLY_MIN_OI, WEEKLY_MIN_VOL,
            "weekly",
            strike_min_pct=0.90,
            strike_max_pct=0.99,
        )
        if w:
            w["exit_at"] = round(w["premium"] * 0.50, 2)  # exit at 50% profit
            weekly.append(w)

        time.sleep(0.12)

    # Sort each tier by score
    daily.sort(key=lambda x: x["score"], reverse=True)
    weekly.sort(key=lambda x: x["score"], reverse=True)
    monthly.sort(key=lambda x: x["score"], reverse=True)

    # Priority: daily first, then weekly, then monthly
    return daily[:2] + weekly[:2] + monthly[:2]


# ── SCANNER 2: ETF Options (Roadmap Building) ─────────────────────────────────

def scan_etf_options_live(cash_available: float = 0, positions: list = None) -> list:
    """
    ETF options scanner — SELF-FUNDING from ETF portfolio.
    Covered calls on owned ETF shares (100+ shares required).
    Premium collected builds more ETF shares — compounds within the ETF sleeve.
    Uses ZERO trading cash — income comes from the ETF holdings themselves.
    """
    opportunities = []
    ledger        = load_ledger()
    owned_etfs    = ledger.get("owned_etfs", {})

    # Count how many ETFs we can write calls on
    writable = [s for s, d in owned_etfs.items() if d.get("shares", 0) >= 100]
    if not writable and not positions:
        print(f"  ETF options: no ETF with 100+ shares yet — accumulating")
        return []
    print(f"  ETF options: {len(writable)} ETF(s) with 100+ shares — writing covered calls")

    # 1. Covered calls on owned ETFs (100+ shares) — all tiers
    if positions:
        for p in positions:
            inst     = p.get("instrument", {})
            sym      = inst.get("symbol", "")
            if inst.get("assetType") != "EQUITY":
                continue
            shares   = int(p.get("longQuantity", 0))
            avg_cost = p.get("averagePrice", 0)
            if shares < 100 or avg_cost <= 0:
                continue

            # Try weekly first then monthly for ETF calls
            for tier, dmin, dmax, dymin, doi, dvol, dmin_d, dmax_d in [
                ("weekly",  WEEKLY_DTE_MIN,  WEEKLY_DTE_MAX,  WEEKLY_MIN_YIELD,  WEEKLY_MIN_OI,  WEEKLY_MIN_VOL,  WEEKLY_DELTA_MIN,  WEEKLY_DELTA_MAX),
                ("monthly", MONTHLY_DTE_MIN, MONTHLY_DTE_MAX, MONTHLY_MIN_YIELD, MONTHLY_MIN_OI, MONTHLY_MIN_VOL, MONTHLY_DELTA_MIN, MONTHLY_DELTA_MAX),
            ]:
                call = find_best_call_tiered(
                    sym, shares, avg_cost,
                    dmin, dmax, dmin_d, dmax_d,
                    dymin, doi, dvol, tier
                )
                if call:
                    call["category"]     = "etf_call"
                    call["roadmap_note"] = str(shares) + " shares owned"
                    opportunities.append(call)
                    break

            time.sleep(0.15)

    # 2. Covered calls on owned ETFs from live scanner (NO cash-secured puts)
    # ETF options income comes from ETF SHARES you own, not trading capital.
    # If you own 100+ shares of an ETF, sell a covered call against it.
    scanned = {o["symbol"] for o in opportunities}

    for sym, data in owned_etfs.items():
        if sym in scanned:
            continue
        shares_owned = data.get("shares", 0)
        avg_cost     = data.get("avg_price", 0)
        # Need at least 100 shares to write 1 covered call
        if shares_owned < 100 or avg_cost <= 0:
            continue

        # Covered call — weekly then monthly
        for tier, dmin, dmax, dymin, doi, dvol, dmin_d, dmax_d in [
            ("weekly",  WEEKLY_DTE_MIN,  WEEKLY_DTE_MAX,  WEEKLY_MIN_YIELD,  WEEKLY_MIN_OI,  WEEKLY_MIN_VOL,  WEEKLY_DELTA_MIN,  WEEKLY_DELTA_MAX),
            ("monthly", MONTHLY_DTE_MIN, MONTHLY_DTE_MAX, MONTHLY_MIN_YIELD, MONTHLY_MIN_OI, MONTHLY_MIN_VOL, MONTHLY_DELTA_MIN, MONTHLY_DELTA_MAX),
        ]:
            call = find_best_call_tiered(
                sym, shares_owned, avg_cost,
                dmin, dmax, dmin_d, dmax_d,
                dymin, doi, dvol, tier
            )
            if call:
                call["category"]     = "etf_call_owned"
                call["exit_at"]      = round(call["premium"] * 0.50, 2)
                call["roadmap_note"] = str(shares_owned) + " ETF shares owned"
                call["shares_owned"] = shares_owned
                opportunities.append(call)
                scanned.add(sym)
                break

        time.sleep(0.15)

    calls = [o for o in opportunities if o["type"] == "call"]
    puts  = sorted([o for o in opportunities if o["type"] == "put"],
                   key=lambda x: x["score"], reverse=True)
    return calls + puts


ETF_MIN_YIELD = 0.08  # 8% for ETFs


def scan_options(cash_available: float, positions: list = None,
                 extra_symbols: list = None) -> list:
    """Legacy combined scan — kept for compatibility."""
    results  = scan_stock_options(cash_available * 0.60)
    results += scan_etf_options_live(cash_available * 0.40, positions)
    return sorted(results, key=lambda x: x["score"], reverse=True)


if __name__ == "__main__":
    print("\n[ CIRCUIT ] OPTIONS SCANNER — 3 TIERS")
    print("="*55)

    STOCK_CASH = 502   # 15% of $3,340
    ETF_CASH   = 334   # 10% of $3,340

    print(f"\nSTOCK OPTIONS (${STOCK_CASH:,.0f} | daily > weekly > monthly):")
    stock = scan_stock_options(STOCK_CASH)
    if stock:
        for r in stock[:6]:
            print(f"  [{r['tier'].upper()}] {r['symbol']} PUT | strike ${r['strike']} | {r['dte']}d | delta {r['delta']}")
            print(f"    yield {r['ann_yield']}%/yr | net ${r['total_prem']:.2f} | exit @50% = ${r['exit_target']:.2f}")
    else:
        print("  No qualifying options (market closed or low IV)")

    print(f"\nETF OPTIONS (${ETF_CASH:,.0f} | weekly > monthly):")
    etf = scan_etf_options_live(ETF_CASH)
    if etf:
        for r in etf[:4]:
            typ = r["type"].upper()
            print(f"  [{r['tier'].upper()}] {r['symbol']} {typ} | strike ${r['strike']} | {r['dte']}d | {r.get('roadmap_note','')}")
            print(f"    yield {r['ann_yield']}%/yr | net ${r['total_prem']:.2f} | exit @50% = ${r['exit_target']:.2f}")
    else:
        print("  No qualifying ETF options")

    print("\n" + "="*55)
