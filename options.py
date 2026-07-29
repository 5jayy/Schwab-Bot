import requests
import time
from auth import get_valid_token

BASE_URL   = "https://api.schwabapi.com/marketdata/v1"
TRADER_URL = "https://api.schwabapi.com/trader/v1"


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def trader_headers():
    return {"Authorization": f"Bearer {get_valid_token()}", "Content-Type": "application/json"}


def get_option_chain(symbol: str, option_type: str = "CALL", strike_count: int = 10) -> dict | None:
    try:
        resp = requests.get(
            f"{BASE_URL}/chains",
            headers=headers(),
            params={
                "symbol":                 symbol,
                "strikeCount":            strike_count,
                "includeUnderlyingQuote": True,
                "strategy":               "SINGLE",
                "optionType":             option_type,
            },
            timeout=15
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Option chain error for {symbol}: {e}")
        return None


# ── Covered calls ─────────────────────────────────────────────────────────────

def find_best_covered_call(symbol: str, shares_owned: int) -> dict | None:
    """
    Find best covered call to sell on a stock we own.
    OTM calls 14-45 DTE, 2-8% above current price.
    """
    if shares_owned < 100:
        return None

    chain = get_option_chain(symbol, option_type="CALL")
    if not chain:
        return None

    underlying_price = chain.get("underlyingPrice", 0)
    if underlying_price <= 0:
        return None

    call_map  = chain.get("callExpDateMap", {})
    contracts = shares_owned // 100
    best      = None

    for expiry, strikes in call_map.items():
        try:
            dte = int(expiry.split(":")[1])
        except Exception:
            continue
        if not (14 <= dte <= 45):
            continue

        for strike_str, options in strikes.items():
            strike = float(strike_str)
            if not (underlying_price * 1.02 <= strike <= underlying_price * 1.08):
                continue

            opt = options[0] if options else None
            if not opt:
                continue

            bid     = opt.get("bid", 0)
            ask     = opt.get("ask", 0)
            volume  = opt.get("totalVolume", 0)
            oi      = opt.get("openInterest", 0)
            premium = (bid + ask) / 2

            if premium < 0.05 or bid <= 0:
                continue
            if volume < 5 and oi < 10:
                continue

            total_premium = premium * 100 * contracts
            score         = (premium / underlying_price) * 100 + (volume * 0.01)

            if best is None or score > best["score"]:
                best = {
                    "type":             "covered_call",
                    "symbol":           symbol,
                    "option_symbol":    opt.get("symbol", ""),
                    "strike":           strike,
                    "expiry":           expiry.split(":")[0],
                    "dte":              dte,
                    "bid":              bid,
                    "ask":              ask,
                    "premium":          premium,
                    "total_premium":    total_premium,
                    "contracts":        contracts,
                    "underlying_price": underlying_price,
                    "score":            score,
                    "description":      opt.get("description", ""),
                }
    return best


# ── ETF options scanning ──────────────────────────────────────────────────────

ETF_OPTIONS_SYMBOLS = ["SCHD", "SOXS", "JEPI", "JEPQ", "ARKK", "XLF", "XLE", "GDX"]

def find_best_etf_covered_call(symbol: str, shares_owned: int) -> dict | None:
    """Find best covered call on ETF positions we own."""
    if shares_owned < 100:
        return None
    return find_best_covered_call(symbol, shares_owned)


def find_best_etf_cash_secured_put(symbol: str, current_price: float,
                                    cash_available: float) -> dict | None:
    """
    Find best cash secured put on liquid ETFs.
    Same structure as stock puts but ETF-specific symbols.
    Only on ETFs we want to own more of at a discount.
    """
    if symbol not in ETF_OPTIONS_SYMBOLS:
        return None
    return find_best_cash_secured_put(symbol, current_price, cash_available)


def scan_etf_options(cash_available: float, positions: list) -> list:
    """
    Scan all ETF options opportunities.
    Returns list of best covered calls and puts on ETFs.
    """
    opportunities = []

    # Check covered calls on ETF positions we own
    for pos in positions:
        sym = pos["instrument"]["symbol"]
        if sym in ETF_OPTIONS_SYMBOLS:
            qty = pos.get("longQuantity", 0)
            if qty >= 100:
                call = find_best_etf_covered_call(sym, qty)
                if call:
                    call["etf_option"] = True
                    call["strategy"]   = "covered_call"
                    opportunities.append(call)

    # Check cash secured puts on ETF universe
    for sym in ETF_OPTIONS_SYMBOLS:
        try:
            quote = requests.get(
                f"https://api.schwabapi.com/marketdata/v1/quotes/{sym}",
                headers=headers(), timeout=10
            )
            if not quote.ok:
                continue
            price = quote.json().get(sym, {}).get("quote", {}).get("lastPrice", 0)
            if price <= 0:
                continue

            cash_needed = price * 100 * 0.95  # 5% OTM put
            if cash_needed > cash_available:
                continue

            put = find_best_etf_cash_secured_put(sym, price, cash_available)
            if put:
                put["etf_option"] = True
                put["strategy"]   = "cash_secured_put"
                opportunities.append(put)
        except Exception:
            continue

    return opportunities


# ── ETF options scanning ──────────────────────────────────────────────────────

ETF_OPTIONS_UNIVERSE = {
    "SCHD":  {"min_shares": 100, "max_strike_pct": 0.97, "min_premium": 0.10},
    "JEPI":  {"min_shares": 100, "max_strike_pct": 0.97, "min_premium": 0.10},
    "SOXS":  {"min_shares": 100, "max_strike_pct": 0.95, "min_premium": 0.08},
    "ARKK":  {"min_shares": 100, "max_strike_pct": 0.95, "min_premium": 0.10},
    "TQQQ":  {"min_shares": 100, "max_strike_pct": 0.95, "min_premium": 0.15},
    "VOO":   {"min_shares": 100, "max_strike_pct": 0.97, "min_premium": 0.20},
    "QQQ":   {"min_shares": 100, "max_strike_pct": 0.97, "min_premium": 0.25},
}


def find_best_etf_covered_call(symbol: str, shares_owned: int) -> dict | None:
    """Find best covered call on an ETF position."""
    if symbol not in ETF_OPTIONS_UNIVERSE:
        return None
    cfg = ETF_OPTIONS_UNIVERSE[symbol]
    if shares_owned < cfg["min_shares"]:
        return None

    chain = get_option_chain(symbol, option_type="CALL")
    if not chain:
        return None

    underlying = chain.get("underlyingPrice", 0)
    if underlying <= 0:
        return None

    call_map  = chain.get("callExpDateMap", {})
    contracts = shares_owned // 100
    best      = None

    for expiry, strikes in call_map.items():
        try:
            dte = int(expiry.split(":")[1])
        except Exception:
            continue
        if not (14 <= dte <= 45):
            continue

        for strike_str, options in strikes.items():
            strike = float(strike_str)
            if not (underlying * 1.01 <= strike <= underlying * 1.06):
                continue

            opt     = options[0] if options else None
            if not opt:
                continue

            bid     = opt.get("bid", 0)
            ask     = opt.get("ask", 0)
            premium = (bid + ask) / 2

            if premium < cfg["min_premium"] or bid <= 0:
                continue

            total   = premium * 100 * contracts
            score   = (premium / underlying) * 100

            if best is None or score > best["score"]:
                best = {
                    "type":           "etf_covered_call",
                    "symbol":         symbol,
                    "option_symbol":  opt.get("symbol", ""),
                    "strike":         strike,
                    "expiry":         expiry.split(":")[0],
                    "dte":            dte,
                    "premium":        premium,
                    "total_premium":  total,
                    "contracts":      contracts,
                    "underlying":     underlying,
                    "score":          score,
                }
    return best


def find_best_etf_put(symbol: str, current_price: float, cash_available: float) -> dict | None:
    """Find best cash secured put on an ETF — sell at strike you want to own more."""
    if symbol not in ETF_OPTIONS_UNIVERSE:
        return None
    cfg = ETF_OPTIONS_UNIVERSE[symbol]

    chain = get_option_chain(symbol, option_type="PUT")
    if not chain:
        return None

    underlying = chain.get("underlyingPrice", current_price)
    put_map    = chain.get("putExpDateMap", {})
    best       = None

    for expiry, strikes in put_map.items():
        try:
            dte = int(expiry.split(":")[1])
        except Exception:
            continue
        if not (14 <= dte <= 45):
            continue

        for strike_str, options in strikes.items():
            strike = float(strike_str)
            if not (underlying * cfg["max_strike_pct"] <= strike <= underlying * 0.99):
                continue

            cash_needed = strike * 100
            if cash_needed > cash_available:
                continue

            opt     = options[0] if options else None
            if not opt:
                continue

            bid     = opt.get("bid", 0)
            ask     = opt.get("ask", 0)
            premium = (bid + ask) / 2

            if premium < cfg["min_premium"] or bid <= 0:
                continue

            score = (premium / underlying) * 100

            if best is None or score > best["score"]:
                best = {
                    "type":          "etf_put",
                    "symbol":        symbol,
                    "option_symbol": opt.get("symbol", ""),
                    "strike":        strike,
                    "expiry":        expiry.split(":")[0],
                    "dte":           dte,
                    "premium":       premium,
                    "total_premium": premium * 100,
                    "cash_needed":   cash_needed,
                    "underlying":    underlying,
                    "score":         score,
                }
    return best


def scan_etf_options(positions: list, cash_available: float) -> list:
    """
    Scan all ETF positions for covered call opportunities.
    Also scan ETF universe for put selling opportunities.
    Returns list of best opportunities sorted by score.
    """
    opportunities = []

    # Covered calls on existing ETF positions
    etf_symbols = {p["instrument"]["symbol"] for p in positions
                   if p["instrument"]["symbol"] in ETF_OPTIONS_UNIVERSE}

    for sym in etf_symbols:
        for p in positions:
            if p["instrument"]["symbol"] == sym:
                shares = int(p.get("longQuantity", 0))
                result = find_best_etf_covered_call(sym, shares)
                if result:
                    opportunities.append(result)
                break

    # Cash secured puts on ETF universe
    for sym, cfg in ETF_OPTIONS_UNIVERSE.items():
        if cash_available >= cfg["min_shares"] * 20:  # rough min
            try:
                resp = requests.get(
                    f"{BASE_URL}/quotes/{sym}",
                    headers=headers(), timeout=10
                )
                if resp.ok:
                    price = resp.json().get(sym, {}).get("quote", {}).get("lastPrice", 0)
                    if price > 0:
                        result = find_best_etf_put(sym, price, cash_available)
                        if result:
                            opportunities.append(result)
            except Exception:
                pass

    return sorted(opportunities, key=lambda x: x["score"], reverse=True)


# ── The Wheel Strategy ────────────────────────────────────────────────────────
# Spins continuously: sell put → get assigned → sell call → called away → repeat
# Uses swing cash + bot bucket profits — never touches trading capital

# $0.65/contract commission — min premium must clear this meaningfully
# Min $0.20 premium = $20/contract, commission = $0.65 = 3.25% cost (acceptable)
# Min $0.15 premium = $15/contract, commission = $0.65 = 4.3% cost (borderline)
COMMISSION_PER_CONTRACT = 0.65

def get_wheel_etfs(cash_available: float = 5000) -> dict:
    """
    Get wheel ETF universe dynamically from live scanner results.
    Falls back to positions already owned if scanner hasn't run yet.
    Never hardcoded — always from live data.
    """
    import json, os
    ledger_path = "/data/trade_ledger.json" if os.path.exists("/data") else "trade_ledger.json"
    try:
        with open(ledger_path) as f:
            ledger = json.load(f)
    except Exception:
        ledger = {}

    # Get live scanner results stored by roadmap.py
    live_opps = ledger.get("live_etf_opportunities", [])
    wheel_etfs = {}

    for etf in live_opps:
        sym   = etf.get("symbol", "")
        price = etf.get("price", 0)
        if not sym or price <= 0:
            continue
        max_cost = price * 100
        if max_cost > cash_available * 1.5:
            continue  # too expensive for current cash
        wheel_etfs[sym] = {
            "max_cost":      max_cost,
            "target_delta":  0.25,
            "min_yield":     0.008,
            "min_premium":   max(0.10, price * 0.005),  # 0.5% of price
        }

    # Also include any ETFs already owned (from positions)
    owned_etfs = ledger.get("owned_etfs", {})
    for sym, data in owned_etfs.items():
        if sym not in wheel_etfs:
            price = data.get("avg_price", 50)
            wheel_etfs[sym] = {
                "max_cost":     price * 100,
                "target_delta": 0.25,
                "min_yield":    0.008,
                "min_premium":  max(0.10, price * 0.005),
            }

    return wheel_etfs


# Keep WHEEL_ETFS as empty — always use get_wheel_etfs() instead
WHEEL_ETFS = {}

# Wheel phases
PHASE_PUT  = "put"   # selling cash secured puts
PHASE_CALL = "call"  # own shares, selling covered calls
PHASE_NONE = "none"  # not in wheel for this ETF


def get_wheel_state() -> dict:
    """Load wheel state from ledger."""
    from ledger import load_ledger
    ledger = load_ledger()
    return ledger.get("wheel_state", {})


def save_wheel_state(state: dict):
    """Save wheel state to ledger."""
    from ledger import load_ledger, save_ledger
    ledger = load_ledger()
    ledger["wheel_state"] = state
    save_ledger(ledger)


def get_annualized_yield(premium: float, strike: float, dte: int) -> float:
    """
    Calculate annualized yield for option.
    Formula: (premium / strike) × (365 / dte)
    Higher = better return per dollar at risk.
    """
    if strike <= 0 or dte <= 0:
        return 0
    return (premium / strike) * (365 / dte)


def find_wheel_put(symbol: str, cash_available: float) -> dict | None:
    """
    Find best cash secured put for wheel.
    Uses delta filter (0.20-0.30) and annualized yield scoring.
    Delta 0.25 = ~25% chance of assignment — sweet spot.
    """
    cfg = WHEEL_ETFS.get(symbol)
    if not cfg:
        return None

    # Check cash available
    if cash_available < cfg["max_cost"]:
        return None

    chain = get_option_chain(symbol, option_type="PUT")
    if not chain:
        return None

    underlying = chain.get("underlyingPrice", 0)
    if underlying <= 0:
        return None

    put_map = chain.get("putExpDateMap", {})
    best    = None
    best_yield = 0

    for expiry, strikes in put_map.items():
        try:
            dte = int(expiry.split(":")[1])
        except Exception:
            continue
        if not (21 <= dte <= 45):  # 3-6 weeks out
            continue

        for strike_str, options in strikes.items():
            strike = float(strike_str)
            cash_needed = strike * 100
            if cash_needed > cash_available:
                continue

            opt = options[0] if options else None
            if not opt:
                continue

            # Delta filter — only 0.15-0.35
            delta = abs(opt.get("delta", 0) or 0)
            if not (0.15 <= delta <= 0.35):
                continue

            bid     = opt.get("bid", 0)
            ask     = opt.get("ask", 0)
            premium = (bid + ask) / 2

            min_prem = cfg.get("min_premium", 0.20)
            if premium < min_prem or bid <= 0:
                continue

            # Net premium after commission
            net_premium = premium - (COMMISSION_PER_CONTRACT / 100)
            if net_premium <= 0:
                continue

            oi  = opt.get("openInterest", 0)
            vol = opt.get("totalVolume", 0)
            if oi < 50 or vol < 10:
                continue

            # Score by annualized yield (using net premium after commission)
            ann_yield = get_annualized_yield(net_premium, strike, dte)
            if ann_yield < cfg["min_yield"]:
                continue

            if ann_yield > best_yield:
                best_yield = ann_yield
                best = {
                    "type":          "wheel_put",
                    "symbol":        symbol,
                    "option_symbol": opt.get("symbol", ""),
                    "strike":        strike,
                    "expiry":        expiry.split(":")[0],
                    "dte":           dte,
                    "delta":         round(delta, 3),
                    "bid":           bid,
                    "ask":           ask,
                    "premium":       premium,
                    "total_premium": premium * 100,
                    "cash_needed":   cash_needed,
                    "underlying":    underlying,
                    "ann_yield":     round(ann_yield * 100, 2),
                    "phase":         PHASE_PUT,
                }
    return best


def find_wheel_call(symbol: str, shares_owned: int, avg_cost: float) -> dict | None:
    """
    Find best covered call for wheel.
    Strike above avg cost to ensure profit if called away.
    Delta 0.20-0.30 — collect premium without giving away too much upside.
    """
    if shares_owned < 100:
        return None

    chain = get_option_chain(symbol, option_type="CALL")
    if not chain:
        return None

    underlying = chain.get("underlyingPrice", 0)
    if underlying <= 0:
        return None

    # Only sell calls above our cost basis
    min_strike = max(avg_cost * 1.01, underlying * 1.005)

    call_map  = chain.get("callExpDateMap", {})
    contracts = shares_owned // 100
    best      = None
    best_yield = 0

    for expiry, strikes in call_map.items():
        try:
            dte = int(expiry.split(":")[1])
        except Exception:
            continue
        if not (21 <= dte <= 45):
            continue

        for strike_str, options in strikes.items():
            strike = float(strike_str)
            if strike < min_strike:
                continue

            opt = options[0] if options else None
            if not opt:
                continue

            delta = abs(opt.get("delta", 0) or 0)
            if not (0.15 <= delta <= 0.35):
                continue

            bid     = opt.get("bid", 0)
            ask     = opt.get("ask", 0)
            premium = (bid + ask) / 2

            cfg2      = WHEEL_ETFS.get(symbol, {})
            min_prem2 = cfg2.get("min_premium", 0.20)
            if premium < min_prem2 or bid <= 0:
                continue

            # Net after commission
            net_premium2 = premium - (COMMISSION_PER_CONTRACT / 100)
            if net_premium2 <= 0:
                continue

            oi  = opt.get("openInterest", 0)
            vol = opt.get("totalVolume", 0)
            if oi < 50 or vol < 10:
                continue

            ann_yield = get_annualized_yield(net_premium2, strike, dte)

            if ann_yield > best_yield:
                best_yield = ann_yield
                best = {
                    "type":          "wheel_call",
                    "symbol":        symbol,
                    "option_symbol": opt.get("symbol", ""),
                    "strike":        strike,
                    "expiry":        expiry.split(":")[0],
                    "dte":           dte,
                    "delta":         round(delta, 3),
                    "bid":           bid,
                    "ask":           ask,
                    "premium":       premium,
                    "total_premium": premium * 100 * contracts,
                    "contracts":     contracts,
                    "underlying":    underlying,
                    "avg_cost":      avg_cost,
                    "ann_yield":     round(ann_yield * 100, 2),
                    "phase":         PHASE_CALL,
                }
    return best


def check_roll_needed(encrypted: str) -> list:
    """
    Check if any wheel options need rolling.
    Roll when: within 7 days of expiry AND still OTM AND premium available.
    Rolling captures more premium instead of letting expire worthless.
    """
    try:
        resp = requests.get(
            f"{TRADER_URL}/accounts/{encrypted}?fields=positions",
            headers=headers(), timeout=15
        )
        resp.raise_for_status()
        positions = resp.json()["securitiesAccount"].get("positions", [])
    except Exception:
        return []

    rolls_needed = []
    today        = __import__("datetime").datetime.now()

    for pos in positions:
        inst = pos.get("instrument", {})
        if inst.get("assetType") != "OPTION":
            continue

        sym        = inst.get("symbol", "")
        underlying = inst.get("underlyingSymbol", "")
        short_qty  = pos.get("shortQuantity", 0)

        if short_qty <= 0 or underlying not in get_wheel_etfs():
            continue

        # Parse expiry from option symbol
        try:
            # Option symbol format: SYM YYMMDD C/P STRIKE
            exp_str   = sym[len(underlying):len(underlying)+6]
            exp_date  = __import__("datetime").datetime.strptime(exp_str, "%y%m%d")
            days_left = (exp_date - today).days
        except Exception:
            continue

        if days_left <= 7:
            opt_type = "CALL" if "C" in sym[len(underlying)+6:len(underlying)+7] else "PUT"
            rolls_needed.append({
                "symbol":     underlying,
                "opt_symbol": sym,
                "opt_type":   opt_type,
                "days_left":  days_left,
                "short_qty":  int(short_qty),
            })

    return rolls_needed


def run_wheel(encrypted: str, positions: list, cash_available: float):
    """
    Main wheel runner — called every strategy check.
    Determines phase for each ETF and executes appropriate option.
    Completely separate from day/swing trading.
    """
    from ledger import load_ledger, save_ledger
    from telegram import send_alert

    wheel_state = get_wheel_state()
    etf_symbols = {p["instrument"]["symbol"] for p in positions
                   if p["instrument"].get("assetType") == "EQUITY"}

    # Check rolls first
    rolls = check_roll_needed(encrypted)
    for roll in rolls:
        msg = "[ WHEEL ] ROLL NEEDED\n" + roll["symbol"] + " " + roll["opt_type"] + "\n" + str(roll["days_left"]) + "d left"
        send_alert(msg)

    # Determine phase for each wheel ETF
    wheel_etfs_dynamic = get_wheel_etfs(cash_available)
    for sym, cfg in wheel_etfs_dynamic.items():
        state = wheel_state.get(sym, {"phase": PHASE_NONE})

        # Check current positions
        shares = 0
        avg_cost = 0
        for p in positions:
            if p["instrument"]["symbol"] == sym:
                shares   = p.get("longQuantity", 0)
                avg_cost = p.get("averagePrice", 0)
                break

        # Update phase based on actual positions
        if shares >= 100:
            state["phase"] = PHASE_CALL
            state["shares"] = shares
            state["avg_cost"] = avg_cost
        elif state["phase"] == PHASE_NONE:
            state["phase"] = PHASE_PUT

        wheel_state[sym] = state

        # Execute based on phase
        if state["phase"] == PHASE_PUT:
            # Check if put already open
            if check_put_already_open(encrypted, sym):
                continue

            put = find_wheel_put(sym, cash_available)
            if not put:
                continue

            try:
                place_cash_secured_put(encrypted, put["option_symbol"], put["premium"])
                msg = "[ WHEEL ] PUT " + sym + "\nSTRIKE " + str(put["strike"]) + " delta " + str(put["delta"]) + "\nPREM   $" + f"{put['total_premium']:.2f}" + "\nYIELD  " + str(put["ann_yield"]) + "% ann\nDTE    " + str(put["dte"]) + "d"
                send_alert(msg)
            except Exception as ex:
                print(f"  Wheel put error {sym}: {ex}")

        elif state["phase"] == PHASE_CALL:
            if check_covered_call_already_open(encrypted, sym):
                continue

            call = find_wheel_call(sym, int(shares), avg_cost)
            if not call:
                continue

            try:
                place_covered_call(encrypted, call["option_symbol"],
                                   call["contracts"], call["premium"])
                msg = "[ WHEEL ] CALL " + sym + "\nSTRIKE " + str(call["strike"]) + " delta " + str(call["delta"]) + "\nPREM   $" + f"{call['total_premium']:.2f}" + "\nYIELD  " + str(call["ann_yield"]) + "% ann\nDTE    " + str(call["dte"]) + "d"
                send_alert(msg)
            except Exception as ex:
                print(f"  Wheel call error {sym}: {ex}")

    save_wheel_state(wheel_state)


# ── Cash secured puts ─────────────────────────────────────────────────────────

def find_best_cash_secured_put(symbol: str, current_price: float, cash_available: float) -> dict | None:
    """
    Find the best cash secured put to sell.
    Looks for OTM puts 5-8% below current price, 14-45 DTE.
    Only considers puts we have enough cash to secure.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/chains",
            headers=headers(),
            params={
                "symbol":                symbol,
                "strikeCount":           10,
                "includeUnderlyingQuote": True,
                "strategy":              "SINGLE",
                "optionType":            "PUT",
            },
            timeout=15
        )
        resp.raise_for_status()
        chain = resp.json()
    except Exception as e:
        print(f"Put chain error for {symbol}: {e}")
        return None

    underlying_price = chain.get("underlyingPrice", current_price)
    put_map          = chain.get("putExpDateMap", {})
    best             = None

    for expiry, strikes in put_map.items():
        try:
            dte = int(expiry.split(":")[1])
        except Exception:
            continue

        if not (14 <= dte <= 45):
            continue

        for strike_str, options in strikes.items():
            strike = float(strike_str)

            # OTM puts only — 5% to 10% below current price
            if not (underlying_price * 0.90 <= strike <= underlying_price * 0.95):
                continue

            # Need enough cash to secure the put (strike × 100)
            cash_needed = strike * 100
            if cash_needed > cash_available:
                continue

            opt = options[0] if options else None
            if not opt:
                continue

            bid     = opt.get("bid", 0)
            ask     = opt.get("ask", 0)
            volume  = opt.get("totalVolume", 0)
            oi      = opt.get("openInterest", 0)
            premium = (bid + ask) / 2

            if premium < 0.05:
                continue
            if bid <= 0:
                continue
            if volume < 5 and oi < 10:
                continue

            total_premium = premium * 100
            score         = (premium / underlying_price) * 100 + (volume * 0.01)

            if best is None or score > best["score"]:
                best = {
                    "symbol":           symbol,
                    "option_symbol":    opt.get("symbol", ""),
                    "strike":           strike,
                    "expiry":           expiry.split(":")[0],
                    "dte":              dte,
                    "bid":              bid,
                    "ask":              ask,
                    "premium":          premium,
                    "total_premium":    total_premium,
                    "cash_needed":      cash_needed,
                    "underlying_price": underlying_price,
                    "score":            score,
                    "description":      opt.get("description", ""),
                }

    return best


def place_covered_call(encrypted: str, option_symbol: str, contracts: int, premium: float):
    """Sell to open a covered call at limit (mid price)."""
    order = {
        "orderType":          "LIMIT",
        "session":           "NORMAL",
        "duration":          "DAY",
        "price":             round(premium, 2),
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": "SELL_TO_OPEN",
            "quantity":    contracts,
            "instrument":  {"symbol": option_symbol, "assetType": "OPTION"}
        }]
    }
    resp = requests.post(
        f"{TRADER_URL}/accounts/{encrypted}/orders",
        headers=trader_headers(),
        json=order,
        timeout=15
    )
    resp.raise_for_status()
    return resp


def place_cash_secured_put(encrypted: str, option_symbol: str, premium: float) -> dict:
    """
    Sell to open a cash secured put at limit price.
    """
    limit_price = round(premium, 2)

    order = {
        "orderType":          "LIMIT",
        "session":           "NORMAL",
        "duration":          "DAY",
        "price":             limit_price,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": "SELL_TO_OPEN",
            "quantity":    1,
            "instrument":  {
                "symbol":    option_symbol,
                "assetType": "OPTION"
            }
        }]
    }

    resp = requests.post(
        f"{TRADER_URL}/accounts/{encrypted}/orders",
        headers=trader_headers(),
        json=order,
        timeout=15
    )
    resp.raise_for_status()
    return resp


def get_open_options(encrypted: str) -> list:
    try:
        resp = requests.get(
            f"{TRADER_URL}/accounts/{encrypted}?fields=positions",
            headers=headers(),
            timeout=15
        )
        resp.raise_for_status()
        positions = resp.json()["securitiesAccount"].get("positions", [])
        return [p for p in positions if p["instrument"].get("assetType") == "OPTION"]
    except Exception as e:
        print(f"Error getting options: {e}")
        return []


def check_put_already_open(encrypted: str, symbol: str) -> bool:
    """Check if we already have an open cash secured put on this stock."""
    open_options = get_open_options(encrypted)
    for opt in open_options:
        opt_symbol = opt["instrument"].get("symbol", "")
        if opt_symbol.startswith(symbol) and opt.get("shortQuantity", 0) > 0:
            # Check if it's a put
            if "P" in opt_symbol[-10:]:
                return True
    return False


def check_covered_call_already_open(encrypted: str, symbol: str) -> bool:
    """Check if we already have an open covered call on this stock."""
    open_options = get_open_options(encrypted)
    for opt in open_options:
        opt_symbol = opt["instrument"].get("symbol", "")
        if opt_symbol.startswith(symbol) and opt.get("shortQuantity", 0) > 0:
            return True
    return False


