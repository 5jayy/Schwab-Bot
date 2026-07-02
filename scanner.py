import requests
import time
from auth import get_valid_token

BASE_URL    = "https://api.schwabapi.com/marketdata/v1"
TRADER_URL  = "https://api.schwabapi.com/trader/v1"

# ETF candidates to scan for best dividend/growth
ETF_UNIVERSE = [
    "SCHD", "JEPI", "JEPQ", "VYM", "HDV", "DGRO", "VIG", "DIVO",
    "VOO", "VTI", "QQQ", "SCHB", "SCHG", "SCHF", "VEA", "VWO"
]


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_movers(index: str = "$SPX", direction: str = "up", top: int = 50) -> list:
    """
    Pull today's top movers from Schwab's live market data.
    index options: $SPX, $COMPX, $DJI
    Returns list of symbols actively moving today.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/movers/{index}",
            headers=headers(),
            params={
                "sort":      direction,
                "frequency": 1
            },
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        screeners = data.get("screeners", [])
        symbols = [s["symbol"] for s in screeners if s.get("symbol")]
        return symbols[:top]
    except Exception as e:
        print(f"Movers fetch error ({index}): {e}")
        return []


def get_dynamic_universe() -> list:
    """
    Build a fresh scan universe every cycle from Schwab's live movers.
    Pulls top movers from S&P 500, Nasdaq, and Dow — both up and down movers.
    Deduplicates and returns unique symbols.
    """
    symbols = []

    # S&P 500 top movers
    symbols += get_movers("$SPX", "up", 40)
    time.sleep(0.2)

    # Nasdaq top movers
    symbols += get_movers("$COMPX", "up", 40)
    time.sleep(0.2)

    # Also check Dow movers
    symbols += get_movers("$DJI", "up", 20)
    time.sleep(0.2)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for s in symbols:
        if s not in seen and not s.startswith("$"):
            seen.add(s)
            unique.append(s)

    print(f"Dynamic universe: {len(unique)} live movers pulled from Schwab")
    return unique


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


def get_price_history(symbol: str, period: int = 10, frequency: int = 5) -> list:
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
        return resp.json().get("candles", [])
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


def macd(prices: list) -> tuple | None:
    if len(prices) < 35:
        return None
    ema12 = ema(prices, 12)
    ema26 = ema(prices, 26)
    if not ema12 or not ema26:
        return None
    macd_line = ema12 - ema26
    macd_values = []
    for i in range(26, len(prices) + 1):
        e12 = ema(prices[:i], 12)
        e26 = ema(prices[:i], 26)
        if e12 and e26:
            macd_values.append(e12 - e26)
    if len(macd_values) < 9:
        return None
    signal = ema(macd_values, 9)
    if not signal:
        return None
    return macd_line, signal, macd_line - signal


def adx(candles: list, period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    try:
        highs  = [c["high"]  for c in candles]
        lows   = [c["low"]   for c in candles]
        closes = [c["close"] for c in candles]
        tr_list, dm_plus, dm_minus = [], [], []
        for i in range(1, len(candles)):
            high_diff = highs[i] - highs[i-1]
            low_diff  = lows[i-1] - lows[i]
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            tr_list.append(tr)
            dm_plus.append(high_diff if high_diff > low_diff and high_diff > 0 else 0)
            dm_minus.append(low_diff if low_diff > high_diff and low_diff > 0 else 0)
        if len(tr_list) < period:
            return None
        atr = sum(tr_list[-period:]) / period
        if atr == 0:
            return None
        di_plus  = (sum(dm_plus[-period:])  / period) / atr * 100
        di_minus = (sum(dm_minus[-period:]) / period) / atr * 100
        dx = abs(di_plus - di_minus) / (di_plus + di_minus) * 100 if (di_plus + di_minus) > 0 else 0
        return dx
    except Exception:
        return None


def volume_confirmation(candles: list, period: int = 20) -> bool:
    if len(candles) < period + 1:
        return True
    try:
        volumes = [c["volume"] for c in candles]
        avg_vol = sum(volumes[-period-1:-1]) / period
        return volumes[-1] >= avg_vol * 0.8
    except Exception:
        return True


def score_stock(symbol: str, max_price: float = 99999) -> dict | None:
    quote = get_quote(symbol)
    if not quote:
        return None
    try:
        price      = quote["quote"]["lastPrice"]
        volume     = quote["quote"]["totalVolume"]
        change_pct = quote["quote"].get("netPercentChangeInDouble", 0)
    except (KeyError, TypeError):
        return None

    if price <= 0 or price > max_price:
        return None
    if volume < 500000:
        return None

    candles = get_price_history(symbol)
    if len(candles) < 30:
        return None

    closes = [c["close"] for c in candles]
    ema9   = ema(closes, 9)
    ema21  = ema(closes, 21)
    rsi14  = rsi(closes, 14)

    if not ema9 or not ema21 or not rsi14:
        return None
    if ema9 <= ema21:
        return None
    if rsi14 >= 72 or rsi14 <= 28:
        return None

    adx_val = adx(candles)
    if adx_val is not None and adx_val < 20:
        return None

    macd_result = macd(closes)
    macd_hist = 0
    if macd_result:
        _, _, macd_hist = macd_result
        if macd_hist < 0:
            return None

    vol_confirmed  = volume_confirmation(candles)
    trend_strength = ((ema9 - ema21) / ema21) * 100
    rsi_score      = 100 - abs(rsi14 - 55)
    adx_bonus      = min(adx_val, 50) * 0.3 if adx_val else 0
    macd_bonus     = min(macd_hist * 100, 10) if macd_hist > 0 else 0
    vol_bonus      = 5 if vol_confirmed else 0

    total_score = (
        (trend_strength * 35) +
        (rsi_score * 0.35) +
        (change_pct * 15) +
        adx_bonus + macd_bonus + vol_bonus
    )

    return {
        "symbol":         symbol,
        "price":          price,
        "volume":         volume,
        "rsi":            rsi14,
        "adx":            adx_val,
        "macd_hist":      macd_hist,
        "vol_confirmed":  vol_confirmed,
        "trend_strength": trend_strength,
        "change_pct":     change_pct,
        "score":          total_score,
    }


def scan_best_stocks(cash: float, top_n: int = 5) -> list:
    """
    Pull live movers from Schwab API, score them, return top N.
    Position size based on available cash.
    """
    position_size = cash * 0.25  # Tier 2 default — bot.py overrides per tier

    # Get dynamic universe from Schwab live data
    universe = get_dynamic_universe()

    if not universe:
        print("No movers found — market may be closed or API issue")
        return []

    print(f"\n-- Scanning {len(universe)} live movers | Budget: ${position_size:,.2f} per trade --")

    results = []
    for symbol in universe:
        result = score_stock(symbol, max_price=position_size)
        if result:
            results.append(result)
        time.sleep(0.1)

    results.sort(key=lambda x: x["score"], reverse=True)
    seen = set()
    unique = []
    for r in results:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            unique.append(r)
    top = unique[:top_n]

    print(f"Top {len(top)} stocks found:")
    for r in top:
        adx_str  = f" ADX={r['adx']:.1f}" if r['adx'] else ""
        macd_str = f" MACD={r['macd_hist']:.3f}"
        print(f"  {r['symbol']}: score={r['score']:.1f} RSI={r['rsi']:.1f}{adx_str}{macd_str} price=${r['price']:.2f}")

    return top


def scan_best_etfs(profit_amount: float, top_n: int = 2) -> list:
    print(f"\n-- Scanning {len(ETF_UNIVERSE)} ETFs | Profit available: ${profit_amount:,.2f} --")
    results = []
    for symbol in ETF_UNIVERSE:
        result = score_stock(symbol, max_price=profit_amount)
        if result:
            results.append(result)
        time.sleep(0.1)

    results.sort(key=lambda x: x["score"], reverse=True)
    seen = set()
    unique = []
    for r in results:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            unique.append(r)
    top = unique[:top_n]

    print(f"Top {len(top)} ETFs found:")
    for r in top:
        print(f"  {r['symbol']}: score={r['score']:.1f} price=${r['price']:.2f}")

    return top


if __name__ == "__main__":
    print("Running live market scan...")
    stocks = scan_best_stocks(500)
    print(f"\nBest stocks right now: {[s['symbol'] for s in stocks]}")
