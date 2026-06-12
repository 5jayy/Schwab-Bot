import requests
import time
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"

# Universe of stocks to scan — covers all tiers
SCAN_UNIVERSE = [
    # Tier 1 — cheap, high volume
    "SOFI", "F", "BAC", "VALE", "PLUG", "AAL", "RIOT", "MARA", "NIO", "PLTR",
    "SNAP", "TLRY", "SIRI", "NOK", "SPCE", "CLSK", "HIMS", "JOBY", "OPEN", "HOOD",
    # Tier 2 — mid cap
    "AAPL", "GOOGL", "AMD", "PYPL", "DIS", "INTC", "UBER", "SQ", "COIN", "RBLX",
    "DKNG", "PENN", "LYFT", "ABNB", "DASH", "RIVN", "LCID", "AFRM", "UPST", "SOFI",
    # Tier 3 — large cap
    "AMZN", "NVDA", "MSFT", "META", "TSLA", "NFLX", "CRM", "SHOP", "BABA", "SPY",
    "QQQ", "ARKK", "XLF", "XLE", "GLD", "SLV", "TQQQ", "SQQQ", "SPXL", "SPXS"
]

# ETF candidates to scan for best dividend/growth
ETF_UNIVERSE = [
    "SCHD", "JEPI", "JEPQ", "VYM", "HDV", "DGRO", "VIG", "DIVO",
    "VOO", "VTI", "QQQ", "SCHB", "SCHG", "SCHF", "VEA", "VWO"
]


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_quote(symbol: str) -> dict | None:
    try:
        resp = requests.get(
            f"{BASE_URL}/quotes",
            headers=headers(),
            params={"symbols": symbol},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get(symbol, None)
    except Exception:
        return None


def get_price_history(symbol: str, period: int = 5, frequency: int = 5) -> list:
    try:
        resp = requests.get(
            f"{BASE_URL}/pricehistory",
            headers=headers(),
            params={
                "symbol":        symbol,
                "periodType":    "day",
                "period":        period,
                "frequencyType": "minute",
                "frequency":     frequency,
                "needExtendedHoursData": False,
            },
            timeout=10
        )
        resp.raise_for_status()
        candles = resp.json().get("candles", [])
        return [c["close"] for c in candles]
    except Exception:
        return []


def ema(prices: list, period: int) -> float | None:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    val = sum(prices[:period]) / period
    for p in prices[period:]:
        val = p * k + val * (1 - k)
    return val


def rsi(prices: list, period: int = 14) -> float | None:
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100
    return 100 - (100 / (1 + ag / al))


def score_stock(symbol: str, max_price: float = 99999) -> dict | None:
    """
    Score a stock based on trend strength, RSI, volume and momentum.
    Returns a score dict or None if not scoreable.
    """
    quote = get_quote(symbol)
    if not quote:
        return None

    try:
        price  = quote["quote"]["lastPrice"]
        volume = quote["quote"]["totalVolume"]
        change_pct = quote["quote"].get("netPercentChangeInDouble", 0)
    except (KeyError, TypeError):
        return None

    if price <= 0 or price > max_price:
        return None

    # Minimum volume filter — avoid illiquid stocks
    if volume < 500000:
        return None

    prices = get_price_history(symbol)
    if len(prices) < 25:
        return None

    ema9  = ema(prices, 9)
    ema21 = ema(prices, 21)
    rsi14 = rsi(prices, 14)

    if not ema9 or not ema21 or not rsi14:
        return None

    # Only score stocks in uptrend with healthy RSI
    if ema9 <= ema21:
        return None
    if rsi14 >= 70 or rsi14 <= 30:
        return None

    # Score = trend strength + RSI sweet spot + momentum
    trend_strength = ((ema9 - ema21) / ema21) * 100
    rsi_score      = 100 - abs(rsi14 - 55)   # sweet spot around 55
    momentum       = change_pct               # today's % change

    total_score = (trend_strength * 40) + (rsi_score * 0.4) + (momentum * 20)

    return {
        "symbol":         symbol,
        "price":          price,
        "volume":         volume,
        "ema9":           ema9,
        "ema21":          ema21,
        "rsi":            rsi14,
        "trend_strength": trend_strength,
        "change_pct":     change_pct,
        "score":          total_score,
    }


def scan_best_stocks(cash: float, top_n: int = 5) -> list:
    """
    Scan universe and return top N stocks by score that fit within position size.
    Position size is 30% of cash.
    """
    position_size = cash * 0.30
    print(f"\n-- Scanning {len(SCAN_UNIVERSE)} stocks | Budget: ${position_size:,.2f} per trade --")

    results = []
    for symbol in SCAN_UNIVERSE:
        result = score_stock(symbol, max_price=position_size)
        if result:
            results.append(result)
        time.sleep(0.1)  # rate limit protection

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:top_n]

    print(f"Top {len(top)} stocks found:")
    for r in top:
        print(f"  {r['symbol']}: score={r['score']:.1f} RSI={r['rsi']:.1f} trend={r['trend_strength']:.3f}% price=${r['price']:.2f}")

    return top


def scan_best_etfs(profit_amount: float, top_n: int = 2) -> list:
    """
    Scan ETF universe and return best ETFs to buy with profit.
    Ranks by trend strength and momentum.
    """
    print(f"\n-- Scanning {len(ETF_UNIVERSE)} ETFs | Profit available: ${profit_amount:,.2f} --")

    results = []
    for symbol in ETF_UNIVERSE:
        result = score_stock(symbol, max_price=profit_amount)
        if result:
            results.append(result)
        time.sleep(0.1)

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:top_n]

    print(f"Top {len(top)} ETFs found:")
    for r in top:
        print(f"  {r['symbol']}: score={r['score']:.1f} price=${r['price']:.2f}")

    return top


if __name__ == "__main__":
    print("Running stock scan...")
    stocks = scan_best_stocks(500)
    print(f"\nBest stocks right now: {[s['symbol'] for s in stocks]}")

    print("\nRunning ETF scan...")
    etfs = scan_best_etfs(1000)
    print(f"Best ETFs right now: {[e['symbol'] for e in etfs]}")
