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


# ── Cash secured puts ─────────────────────────────────────────────────────────

def find_best_cash_secured_put(symbol: str, cash_available: float) -> dict | None:
    """
    Find best cash secured put to sell.
    OTM puts 14-45 DTE, 3-8% below current price.
    Requires enough cash to buy 100 shares at strike price.
    """
    chain = get_option_chain(symbol, option_type="PUT")
    if not chain:
        return None

    underlying_price = chain.get("underlyingPrice", 0)
    if underlying_price <= 0:
        return None

    put_map = chain.get("putExpDateMap", {})
    best    = None

    for expiry, strikes in put_map.items():
        try:
            dte = int(expiry.split(":")[1])
        except Exception:
            continue
        if not (14 <= dte <= 45):
            continue

        for strike_str, options in strikes.items():
            strike = float(strike_str)

            # OTM puts only — 3% to 8% below current price
            if not (underlying_price * 0.92 <= strike <= underlying_price * 0.97):
                continue

            # Must have enough cash to cover assignment (100 shares at strike)
            cash_required = strike * 100
            if cash_required > cash_available:
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

            total_premium = premium * 100
            score         = (premium / underlying_price) * 100 + (volume * 0.01)

            if best is None or score > best["score"]:
                best = {
                    "type":             "cash_secured_put",
                    "symbol":           symbol,
                    "option_symbol":    opt.get("symbol", ""),
                    "strike":           strike,
                    "expiry":           expiry.split(":")[0],
                    "dte":              dte,
                    "bid":              bid,
                    "ask":              ask,
                    "premium":          premium,
                    "total_premium":    total_premium,
                    "contracts":        1,
                    "cash_required":    cash_required,
                    "underlying_price": underlying_price,
                    "score":            score,
                    "description":      opt.get("description", ""),
                }
    return best


# ── Place orders ──────────────────────────────────────────────────────────────

def place_covered_call(encrypted: str, option_symbol: str, contracts: int, premium: float):
    """Sell to open a covered call at limit (mid price)."""
    order = {
        "orderType":         "LIMIT",
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


def place_cash_secured_put(encrypted: str, option_symbol: str, premium: float):
    """Sell to open a cash secured put at limit (mid price)."""
    order = {
        "orderType":         "LIMIT",
        "session":           "NORMAL",
        "duration":          "DAY",
        "price":             round(premium, 2),
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": "SELL_TO_OPEN",
            "quantity":    1,
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


# ── Check for open options ────────────────────────────────────────────────────

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


def check_option_already_open(encrypted: str, symbol: str, option_type: str) -> bool:
    """Check if we already have an open option on this symbol."""
    open_options = get_open_options(encrypted)
    for opt in open_options:
        opt_symbol = opt["instrument"].get("symbol", "")
        if opt_symbol.startswith(symbol) and opt.get("shortQuantity", 0) > 0:
            desc = opt["instrument"].get("description", "").upper()
            if option_type == "CALL" and "CALL" in desc:
                return True
            if option_type == "PUT" and "PUT" in desc:
                return True
    return False


# ── Cash Secured Puts ────────────────────────────────────────────────────────

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


def place_cash_secured_put(encrypted: str, option_symbol: str, premium: float) -> dict:
    """
    Sell to open a cash secured put at limit price.
    """
    limit_price = round(premium, 2)

    order = {
        "orderType":         "LIMIT",
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


def check_put_already_open(encrypted: str, symbol: str) -> bool:
    """Check if we already have an open cash secured put on this stock."""
    open_options = get_open_options(encrypted)
    for opt in open_options:
        opt_symbol = opt["instrument"].get("symbol", "")
        if opt_symbol.startswith(symbol) and opt.get("shortQuantity", 0) > 0:
            if "P" in opt_symbol[-10:]:
                return True
    return False
