import requests
import time
from auth import get_valid_token
from datetime import datetime, timedelta

BASE_URL = "https://api.schwabapi.com/marketdata/v1"
TRADER_URL = "https://api.schwabapi.com/trader/v1"


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def trader_headers():
    return {"Authorization": f"Bearer {get_valid_token()}", "Content-Type": "application/json"}


def get_option_chain(symbol: str, strike_count: int = 10) -> dict | None:
    """Fetch the full option chain for a symbol."""
    try:
        resp = requests.get(
            f"{BASE_URL}/chains",
            headers=headers(),
            params={
                "symbol":                symbol,
                "strikeCount":           strike_count,
                "includeUnderlyingQuote": True,
                "strategy":              "SINGLE",
                "optionType":            "CALL",
            },
            timeout=15
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Option chain error for {symbol}: {e}")
        return None


def find_best_covered_call(symbol: str, shares_owned: int) -> dict | None:
    """
    Find the best covered call to sell.
    Looks for OTM calls 14-30 DTE with best premium/risk ratio.
    Needs at least 100 shares for 1 contract.
    """
    if shares_owned < 100:
        return None

    chain = get_option_chain(symbol)
    if not chain:
        return None

    underlying_price = chain.get("underlyingPrice", 0)
    if underlying_price <= 0:
        return None

    call_map   = chain.get("callExpDateMap", {})
    contracts  = shares_owned // 100
    best       = None

    for expiry, strikes in call_map.items():
        try:
            dte = int(expiry.split(":")[1])
        except Exception:
            continue

        # Target 14-45 DTE for best premium decay
        if not (14 <= dte <= 45):
            continue

        for strike_str, options in strikes.items():
            strike = float(strike_str)

            # OTM calls only — 2% to 8% above current price
            if not (underlying_price * 1.02 <= strike <= underlying_price * 1.08):
                continue

            opt = options[0] if options else None
            if not opt:
                continue

            bid     = opt.get("bid", 0)
            ask     = opt.get("ask", 0)
            volume  = opt.get("totalVolume", 0)
            oi      = opt.get("openInterest", 0)
            delta   = abs(opt.get("delta", 0))
            premium = (bid + ask) / 2

            # Minimum filters
            if premium < 0.05:
                continue
            if bid <= 0:
                continue
            if volume < 5 and oi < 10:
                continue

            total_premium = premium * 100 * contracts

            # Score = premium relative to stock price × liquidity
            score = (premium / underlying_price) * 100 + (volume * 0.01)

            if best is None or score > best["score"]:
                best = {
                    "symbol":          symbol,
                    "option_symbol":   opt.get("symbol", ""),
                    "strike":          strike,
                    "expiry":          expiry.split(":")[0],
                    "dte":             dte,
                    "bid":             bid,
                    "ask":             ask,
                    "premium":         premium,
                    "total_premium":   total_premium,
                    "contracts":       contracts,
                    "delta":           delta,
                    "volume":          volume,
                    "open_interest":   oi,
                    "underlying_price": underlying_price,
                    "score":           score,
                    "description":     opt.get("description", ""),
                }

    return best


def place_covered_call(encrypted: str, option_symbol: str, contracts: int, premium: float) -> dict:
    """
    Sell to open a covered call at limit price (mid of bid/ask).
    Returns order response.
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
            "quantity":    contracts,
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
    """Get all open option positions."""
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


def check_covered_call_already_open(encrypted: str, symbol: str) -> bool:
    """Check if we already have an open covered call on this stock."""
    open_options = get_open_options(encrypted)
    for opt in open_options:
        opt_symbol = opt["instrument"].get("symbol", "")
        # Option symbols start with the underlying symbol
        if opt_symbol.startswith(symbol) and opt.get("shortQuantity", 0) > 0:
            return True
    return False
