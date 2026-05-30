import os
import requests
from auth import get_valid_token
from dotenv import load_dotenv

load_dotenv()

BASE_URL    = "https://api.schwabapi.com/marketdata/v1"
TRADE_STOCKS = [s.strip() for s in os.getenv("TRADE_STOCKS", "AAPL,AMZN,MSFT,GOOGL,NVDA").split(",")]

# ── Market data ─────────────────────────────────────────────────────────────

def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_price_history(symbol: str, period: int = 1, frequency: int = 5) -> list:
    """Get recent price candles for a symbol. Returns list of close prices."""
    resp = requests.get(
        f"{BASE_URL}/pricehistory",
        headers=headers(),
        params={
            "symbol":          symbol,
            "periodType":      "day",
            "period":          period,
            "frequencyType":   "minute",
            "frequency":       frequency,
            "needExtendedHoursData": False,
        }
    )
    resp.raise_for_status()
    candles = resp.json().get("candles", [])
    return [c["close"] for c in candles]


def get_quote(symbol: str) -> dict:
    """Get current quote for a symbol."""
    resp = requests.get(
        f"{BASE_URL}/quotes",
        headers=headers(),
        params={"symbols": symbol}
    )
    resp.raise_for_status()
    return resp.json().get(symbol, {})


def get_option_chain(symbol: str, strike_count: int = 5) -> dict:
    """Get option chain for a symbol."""
    resp = requests.get(
        f"{BASE_URL}/chains",
        headers=headers(),
        params={
            "symbol":      symbol,
            "strikeCount": strike_count,
            "includeUnderlyingQuote": True,
            "strategy":    "SINGLE",
            "optionType":  "ALL",
        }
    )
    resp.raise_for_status()
    return resp.json()


# ── Technical indicators ─────────────────────────────────────────────────────

def ema(prices: list, period: int) -> float:
    """Calculate EMA for a list of prices."""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(prices[:period]) / period
    for price in prices[period:]:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val


def rsi(prices: list, period: int = 14) -> float:
    """Calculate RSI for a list of prices."""
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ── Signal generation ────────────────────────────────────────────────────────

def get_signal(symbol: str) -> dict:
    """
    Returns a trading signal for a symbol.
    Signal: BUY, SELL, or HOLD
    Uses EMA9 > EMA21 for trend + RSI for entry/exit timing.
    """
    try:
        prices = get_price_history(symbol, period=5, frequency=5)
        if len(prices) < 25:
            return {"symbol": symbol, "signal": "HOLD", "reason": "Not enough data"}

        ema9  = ema(prices, 9)
        ema21 = ema(prices, 21)
        rsi14 = rsi(prices, 14)
        price = prices[-1]

        reason = f"EMA9={ema9:.2f} EMA21={ema21:.2f} RSI={rsi14:.1f} Price={price:.2f}"

        # BUY: uptrend (EMA9 > EMA21) and RSI not overbought
        if ema9 > ema21 and rsi14 < 65:
            return {"symbol": symbol, "signal": "BUY", "reason": reason, "price": price}

        # SELL: downtrend (EMA9 < EMA21) or RSI overbought
        if ema9 < ema21 or rsi14 > 75:
            return {"symbol": symbol, "signal": "SELL", "reason": reason, "price": price}

        return {"symbol": symbol, "signal": "HOLD", "reason": reason, "price": price}

    except Exception as e:
        return {"symbol": symbol, "signal": "HOLD", "reason": f"Error: {e}"}


# ── Options: best covered call to sell ──────────────────────────────────────

def find_best_covered_call(symbol: str, shares_owned: int) -> dict | None:
    """
    Find the best covered call to sell on a stock we own.
    Looks for OTM calls expiring in 14-30 days with decent premium.
    Returns None if no good option found.
    """
    if shares_owned < 100:
        return None  # Need at least 100 shares for 1 contract

    try:
        chain = get_option_chain(symbol)
        underlying_price = chain.get("underlyingPrice", 0)
        call_map = chain.get("callExpDateMap", {})

        best = None
        for expiry, strikes in call_map.items():
            # Parse days to expiration from key like "2024-01-19:30"
            try:
                dte = int(expiry.split(":")[1])
            except Exception:
                continue

            # Only look at 14-30 DTE
            if not (14 <= dte <= 30):
                continue

            for strike_str, options in strikes.items():
                strike = float(strike_str)
                # Only OTM calls (strike > current price by 2-5%)
                if not (underlying_price * 1.02 <= strike <= underlying_price * 1.06):
                    continue

                opt = options[0] if options else None
                if not opt:
                    continue

                bid     = opt.get("bid", 0)
                ask     = opt.get("ask", 0)
                premium = (bid + ask) / 2
                volume  = opt.get("totalVolume", 0)

                # Minimum $0.50 premium and some volume
                if premium < 0.50 or volume < 10:
                    continue

                contracts = shares_owned // 100
                total_premium = premium * 100 * contracts

                if best is None or premium > best["premium"]:
                    best = {
                        "symbol":        symbol,
                        "strike":        strike,
                        "expiry":        expiry.split(":")[0],
                        "dte":           dte,
                        "premium":       premium,
                        "total_premium": total_premium,
                        "contracts":     contracts,
                        "description":   opt.get("description", ""),
                    }

        return best

    except Exception as e:
        print(f"Option chain error for {symbol}: {e}")
        return None


if __name__ == "__main__":
    print("Scanning signals for:", TRADE_STOCKS)
    for sym in TRADE_STOCKS:
        sig = get_signal(sym)
        print(f"{sym}: {sig['signal']} — {sig['reason']}")
