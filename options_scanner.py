"""
Circuit Options Scanner — Two Separate Systems

1. scan_stock_options() — Stock options (monthly high IV)
   - Finds high IV stocks from movers
   - 21-45 DTE (monthly cycle)
   - Best premium yield per dollar
   - Pure income — doesn't need to own shares
   - Budget: 15% of cash

2. scan_etf_options_live() — ETF options (roadmap building)
   - Uses live scanner to find cheap optionable ETFs
   - Prioritizes ETFs close to 100 shares (roadmap)
   - Covered calls on owned ETFs first
   - Cash secured puts on ETFs we want to own
   - Budget: 10% of cash

Completely separate — no mixing, no competition.
"""

import requests
import time
import json
import os
from auth import get_valid_token

MARKET_URL  = "https://api.schwabapi.com/marketdata/v1"
COMMISSION  = 0.65
LEDGER_PATH = "/data/trade_ledger.json" if os.path.exists("/data") else "trade_ledger.json"

# Stock options settings
STOCK_MIN_YIELD   = 0.15   # 15% annualized minimum
STOCK_MIN_OI      = 100
STOCK_MIN_VOL     = 10
STOCK_DELTA_MIN   = 0.18
STOCK_DELTA_MAX   = 0.35
STOCK_DTE_MIN     = 21
STOCK_DTE_MAX     = 45

# ETF options settings
ETF_MIN_YIELD     = 0.08   # 8% annualized (ETFs have lower IV)
ETF_MIN_OI        = 50
ETF_MIN_VOL       = 5
ETF_DELTA_MIN     = 0.18
ETF_DELTA_MAX     = 0.35
ETF_DTE_MIN       = 21
ETF_DTE_MAX       = 45


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def load_ledger() -> dict:
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def get_movers() -> list:
    """Get top movers from SPX and NASDAQ."""
    symbols = []
    for index in ["$SPX", "$COMP"]:
        try:
            resp = requests.get(
                f"{MARKET_URL}/movers/{index}",
                headers=headers(),
                params={"sort": "VOLUME", "frequency": 1},
                timeout=10
            )
            if resp.ok:
                symbols += [m["symbol"] for m in resp.json().get("screeners", [])[:15]]
        except Exception:
            pass
    return list(set(symbols))


def get_option_chain(symbol: str, option_type: str = "PUT") -> dict:
    try:
        resp = requests.get(
            f"{MARKET_URL}/chains",
            headers=headers(),
            params={
                "symbol":       symbol,
                "contractType": option_type,
                "strikeCount":  15,
                "strategy":     "SINGLE",
            },
            timeout=15
        )
        return resp.json() if resp.ok else {}
    except Exception:
        return {}


def get_quote_price(symbol: str) -> float:
    try:
        resp = requests.get(
            f"{MARKET_URL}/quotes/{symbol}",
            headers=headers(),
            timeout=8
        )
        if resp.ok:
            return resp.json().get(symbol, {}).get("quote", {}).get("lastPrice", 0)
    except Exception:
        pass
    return 0


def find_best_put(symbol: str, cash_available: float,
                  min_yield: float, min_oi: int, min_vol: int,
                  delta_min: float, delta_max: float,
                  dte_min: int, dte_max: int,
                  strike_min_pct: float = 0.85,
                  strike_max_pct: float = 0.99) -> dict | None:
    """Generic put finder — used by both stock and ETF scanners."""
    chain      = get_option_chain(symbol, "PUT")
    underlying = chain.get("underlyingPrice", 0)
    if underlying <= 0:
        return None

    put_map    = chain.get("putExpDateMap", {})
    best       = None
    best_yield = 0

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

            if bid <= 0 or prem < 0.10:
                continue
            if oi < min_oi or vol < min_vol:
                continue

            net_prem  = prem - (COMMISSION / 100)
            if net_prem <= 0:
                continue

            ann_yield = (net_prem / strike) * (365 / dte)
            if ann_yield < min_yield:
                continue

            if ann_yield > best_yield:
                best_yield = ann_yield
                best = {
                    "symbol":      symbol,
                    "type":        "put",
                    "strike":      strike,
                    "expiry":      expiry.split(":")[0],
                    "dte":         dte,
                    "delta":       round(delta, 3),
                    "bid":         bid,
                    "premium":     round(prem, 2),
                    "net_premium": round(net_prem, 2),
                    "total_prem":  round(net_prem * 100, 2),
                    "collateral":  round(collat, 2),
                    "ann_yield":   round(ann_yield * 100, 1),
                    "underlying":  round(underlying, 2),
                    "opt_symbol":  opt.get("symbol", ""),
                }

    return best


def find_best_call(symbol: str, shares: int, avg_cost: float,
                   min_yield: float, min_oi: int, min_vol: int,
                   delta_min: float, delta_max: float,
                   dte_min: int, dte_max: int) -> dict | None:
    """Generic call finder for covered calls."""
    if shares < 100:
        return None

    chain      = get_option_chain(symbol, "CALL")
    underlying = chain.get("underlyingPrice", avg_cost)
    call_map   = chain.get("callExpDateMap", {})
    contracts  = shares // 100
    best       = None
    best_yield = 0

    for expiry, strikes in call_map.items():
        try:
            dte = int(expiry.split(":")[1])
        except Exception:
            continue
        if not (dte_min <= dte <= dte_max):
            continue

        for strike_str, opts in strikes.items():
            strike = float(strike_str)
            if strike < avg_cost * 1.01 or strike > underlying * 1.10:
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

            if bid <= 0 or prem < 0.10:
                continue
            if oi < min_oi or vol < min_vol:
                continue

            net_prem  = prem - (COMMISSION / 100)
            ann_yield = (net_prem / strike) * (365 / dte)

            if ann_yield < min_yield:
                continue

            if ann_yield > best_yield:
                best_yield = ann_yield
                best = {
                    "symbol":      symbol,
                    "type":        "call",
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
                    "ann_yield":   round(ann_yield * 100, 1),
                    "underlying":  round(underlying, 2),
                    "avg_cost":    avg_cost,
                    "opt_symbol":  opt.get("symbol", ""),
                }

    return best


# ── SCANNER 1: Stock Options (monthly high IV) ────────────────────────────────

def scan_stock_options(cash_available: float) -> list:
    """
    Finds best cash secured puts on high IV stocks.
    Monthly cycle (21-45 DTE).
    Pure income — doesn't need to own the stock.
    Budget: 15% of swing cash.
    """
    opportunities = []
    symbols       = get_movers()
    scanned       = set()

    print(f"  Stock options scan: {len(symbols)} movers | ${cash_available:,.0f} budget")

    for sym in symbols:
        if sym in scanned:
            continue
        scanned.add(sym)

        price = get_quote_price(sym)
        if price <= 0:
            continue

        # Skip if stock too expensive for collateral
        if price * 100 > cash_available:
            continue

        # Skip very cheap stocks (poor premium quality)
        if price < 10:
            continue

        put = find_best_put(
            sym, cash_available,
            min_yield=STOCK_MIN_YIELD,
            min_oi=STOCK_MIN_OI,
            min_vol=STOCK_MIN_VOL,
            delta_min=STOCK_DELTA_MIN,
            delta_max=STOCK_DELTA_MAX,
            dte_min=STOCK_DTE_MIN,
            dte_max=STOCK_DTE_MAX,
            strike_min_pct=0.87,
            strike_max_pct=0.99,
        )
        if put:
            put["category"] = "stock"
            opportunities.append(put)

        time.sleep(0.15)

    return sorted(opportunities, key=lambda x: x["ann_yield"], reverse=True)


# ── SCANNER 2: ETF Options (roadmap building) ─────────────────────────────────

def scan_etf_options_live(cash_available: float, positions: list = None) -> list:
    """
    Finds best ETF options using live scanner results from roadmap.
    Priority:
    1. Covered calls on ETFs you already own (100+ shares)
    2. Cash secured puts on ETFs near 100 shares (building roadmap)
    3. Cash secured puts on any cheap optionable ETF from live scanner
    Budget: 10% of swing cash.
    """
    opportunities = []
    ledger        = load_ledger()

    # Get live ETF opportunities from roadmap scanner
    live_etfs   = ledger.get("live_etf_opportunities", [])
    owned_etfs  = ledger.get("owned_etfs", {})

    # 1. Covered calls on owned ETFs (100+ shares) — highest priority
    if positions:
        for p in positions:
            inst   = p.get("instrument", {})
            sym    = inst.get("symbol", "")
            asset  = inst.get("assetType", "")
            if asset != "EQUITY":
                continue
            shares   = int(p.get("longQuantity", 0))
            avg_cost = p.get("averagePrice", 0)
            if shares < 100 or avg_cost <= 0:
                continue

            call = find_best_call(
                sym, shares, avg_cost,
                min_yield=ETF_MIN_YIELD,
                min_oi=ETF_MIN_OI,
                min_vol=ETF_MIN_VOL,
                delta_min=ETF_DELTA_MIN,
                delta_max=ETF_DELTA_MAX,
                dte_min=ETF_DTE_MIN,
                dte_max=ETF_DTE_MAX,
            )
            if call:
                call["category"]     = "etf_call"
                call["roadmap_note"] = "covered call — " + str(shares) + " shares owned"
                opportunities.append(call)

            time.sleep(0.15)

    # 2. Puts on ETFs from live scanner — prioritize cheapest/closest to 100
    scanned = {o["symbol"] for o in opportunities}

    # Sort live ETFs by ROI score (set by roadmap)
    sorted_live = sorted(live_etfs, key=lambda x: x.get("score", 0), reverse=True)

    for etf in sorted_live[:10]:
        sym   = etf.get("symbol", "")
        price = etf.get("price", 0)

        if not sym or price <= 0 or sym in scanned:
            continue

        # Check how many shares we own (roadmap progress)
        shares_owned = owned_etfs.get(sym, {}).get("shares", 0)
        shares_needed = max(0, 100 - shares_owned)
        pct_to_call   = int(shares_owned / 100 * 100)

        # Only sell puts on ETFs we want to own more of
        collat_needed = price * 0.97 * 100  # 3% OTM put
        if collat_needed > cash_available:
            continue

        put = find_best_put(
            sym, cash_available,
            min_yield=ETF_MIN_YIELD,
            min_oi=ETF_MIN_OI,
            min_vol=ETF_MIN_VOL,
            delta_min=ETF_DELTA_MIN,
            delta_max=ETF_DELTA_MAX,
            dte_min=ETF_DTE_MIN,
            dte_max=ETF_DTE_MAX,
            strike_min_pct=0.90,
            strike_max_pct=0.99,
        )
        if put:
            put["category"]     = "etf_put"
            put["roadmap_note"] = str(pct_to_call) + "% to call unlock"
            put["shares_owned"] = shares_owned
            put["shares_needed"] = shares_needed
            opportunities.append(put)
            scanned.add(sym)

        time.sleep(0.15)

    # Sort: covered calls first (already own shares), then puts by yield
    calls = [o for o in opportunities if o["type"] == "call"]
    puts  = sorted([o for o in opportunities if o["type"] == "put"],
                   key=lambda x: x["ann_yield"], reverse=True)

    return calls + puts


# ── COMBINED SCAN (for testing) ───────────────────────────────────────────────

def scan_options(cash_available: float, positions: list = None,
                 extra_symbols: list = None) -> list:
    """Legacy combined scan — kept for backtest compatibility."""
    stock_budget = cash_available * 0.60
    etf_budget   = cash_available * 0.40
    results      = scan_stock_options(stock_budget)
    results     += scan_etf_options_live(etf_budget, positions)
    return sorted(results, key=lambda x: x["ann_yield"], reverse=True)


if __name__ == "__main__":
    print("\n[ CIRCUIT ] OPTIONS SCANNER TEST")
    print("="*50)

    CASH = 835  # 15% of $3,340 for stock options test

    print(f"\nSTOCK OPTIONS (${CASH:,.0f} budget, 15% monthly):")
    stock_results = scan_stock_options(cash_available=CASH)
    if stock_results:
        for r in stock_results[:5]:
            print(f"  {r['symbol']} PUT | strike ${r['strike']} | {r['dte']}d | delta {r['delta']}")
            print(f"    yield {r['ann_yield']}%/yr | net ${r['total_prem']:.2f}/mo | collat ${r['collateral']:,.0f}")
    else:
        print("  No qualifying stock options")

    ETF_CASH = 334  # 10% of $3,340 for ETF options test
    print(f"\nETF OPTIONS (${ETF_CASH:,.0f} budget, 10% roadmap):")
    etf_results = scan_etf_options_live(cash_available=ETF_CASH)
    if etf_results:
        for r in etf_results[:5]:
            typ = r["type"].upper()
            print(f"  {r['symbol']} {typ} | strike ${r['strike']} | {r['dte']}d | delta {r['delta']}")
            print(f"    yield {r['ann_yield']}%/yr | net ${r['total_prem']:.2f}/mo | {r.get('roadmap_note','')}")
    else:
        print("  No qualifying ETF options")

    print("\n" + "="*50)
