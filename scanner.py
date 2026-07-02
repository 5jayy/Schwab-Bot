import requests
import time
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"

# Expanded universe — 120 stocks across all market caps
# Tiers used for POSITION SIZING only, not for limiting what gets scanned
SCAN_UNIVERSE = [
    # High volume momentum plays
    "SOFI", "F", "BAC", "VALE", "PLUG", "AAL", "RIOT", "MARA", "NIO", "PLTR",
    "SNAP", "TLRY", "SIRI", "NOK", "SPCE", "CLSK", "HIMS", "JOBY", "OPEN", "HOOD",
    "AFRM", "UPST", "RIVN", "LCID", "DKNG", "PENN", "LYFT", "ABNB", "DASH", "RBLX",
    # Mid cap growth
    "AAPL", "GOOGL", "AMD", "PYPL", "DIS", "INTC", "UBER", "SQ", "COIN", "ROKU",
    "SHOP", "TWLO", "ZM", "DOCU", "BILL", "GTLB", "PATH", "U", "DDOG", "SNOW",
    "NET", "MDB", "CRWD", "ZS", "OKTA", "ESTC", "CFLT", "HUBS", "TEAM", "WDAY",
    # Large cap momentum
    "AMZN", "NVDA", "MSFT", "META", "TSLA", "NFLX", "CRM", "BABA", "ORCL", "IBM",
    "ADBE", "QCOM", "TXN", "MU", "AVGO", "AMAT", "LRCX", "KLAC", "MRVL", "ON",
    # Finance & crypto adjacent
    "JPM", "GS", "MS", "C", "WFC", "HOOD", "MSTR", "COIN", "SQ", "PYPL",
    # Healthcare & biotech
    "MRNA", "BNTX", "NVAX", "TDOC", "HIMS", "ACMR", "KTOS", "RGEN", "IOVA", "FATE",
    # Energy & commodities
    "XOM", "CVX", "OXY", "SLB", "HAL", "FANG", "DVN", "MPC", "VLO", "PSX",
    # Consumer & retail
    "AMZN", "WMT", "TGT", "COST", "HD", "LOW", "NKE", "LULU", "ROST", "TJX"
]

# Remove duplicates while preserving order
seen_universe = set()
SCAN_UNIVERSE = [s for s in SCAN_UNIVERSE if not (s in seen_universe or seen_universe.add(s))]

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
        candles = resp.json().get("candles", [])
        return candles  # return full candles for volume/high/low
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
    """Returns (macd_line, signal_line, histogram). Positive histogram = bullish momentum building."""
    if len(prices) < 35:
        return None
    ema12 = ema(prices, 12)
    ema26 = ema(prices, 26)
    if not ema12 or not ema26:
        return None
    macd_line = ema12 - ema26

    # Signal line = 9-period EMA of MACD
    # Approximate using last 9 values
    macd_values = []
    for i in range(9, len(prices) + 1):
        e12 = ema(prices[:i], 12)
        e26 = ema(prices[:i], 26)
        if e12 and e26:
            macd_values.append(e12 - e26)

    if len(macd_values) < 9:
        return None
    signal = ema(macd_values, 9)
    if not signal:
        return None
    histogram = macd_line - signal
    return macd_line, signal, histogram


def adx(candles: list, period: int = 14) -> float | None:
    """
    Average Directional Index — measures trend STRENGTH not direction.
    ADX > 25 = strong trend (good to trade)
    ADX < 20 = weak/choppy (avoid)
    """
    if len(candles) < period + 1:
        return None
    try:
        highs  = [c["high"] for c in candles]
        lows   = [c["low"]  for c in candles]
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

        atr    = sum(tr_list[-period:]) / period
        if atr == 0:
            return None
        di_plus  = (sum(dm_plus[-period:])  / period) / atr * 100
        di_minus = (sum(dm_minus[-period:]) / period) / atr * 100
        dx = abs(di_plus - di_minus) / (di_plus + di_minus) * 100 if (di_plus + di_minus) > 0 else 0
        return dx
    except Exception:
        return None


def volume_confirmation(candles: list, period: int = 20) -> bool:
    """
    Returns True if current volume is above the average volume of the last N candles.
    Strong moves have above-average volume behind them.
    """
    if len(candles) < period + 1:
        return True  # not enough data, don't penalize
    try:
        volumes = [c["volume"] for c in candles]
        avg_vol = sum(volumes[-period-1:-1]) / period
        curr_vol = volumes[-1]
        return curr_vol >= avg_vol * 0.8  # within 80% of average is acceptable
    except Exception:
        return True


def score_stock(symbol: str, max_price: float = 99999) -> dict | None:
    """
    Score a stock using EMA trend + RSI + MACD momentum + ADX strength + volume.
    Returns a score dict or None if conditions not met.
    """
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

    # Minimum volume filter — avoid illiquid stocks
    if volume < 500000:
        return None

    candles = get_price_history(symbol)
    if len(candles) < 30:
        return None

    closes = [c["close"] for c in candles]

    ema9  = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi14 = rsi(closes, 14)

    if not ema9 or not ema21 or not rsi14:
        return None

    # Must be in uptrend
    if ema9 <= ema21:
        return None

    # RSI must be in healthy range — not overbought or oversold
    if rsi14 >= 72 or rsi14 <= 28:
        return None

    # ADX filter — only trade when trend is strong enough
    adx_val = adx(candles)
    if adx_val is not None and adx_val < 20:
        return None  # trend too weak/choppy

    # MACD confirmation — histogram should be positive (momentum building)
    macd_result = macd(closes)
    macd_hist = 0
    if macd_result:
        _, _, macd_hist = macd_result
        if macd_hist < 0:
            return None  # momentum fading, skip

    # Volume confirmation
    vol_confirmed = volume_confirmation(candles)

    # Scoring
    trend_strength = ((ema9 - ema21) / ema21) * 100
    rsi_score      = 100 - abs(rsi14 - 55)   # sweet spot around 55
    momentum       = change_pct
    adx_bonus      = min(adx_val, 50) * 0.3 if adx_val else 0
    macd_bonus     = min(macd_hist * 100, 10) if macd_hist > 0 else 0
    vol_bonus      = 5 if vol_confirmed else 0

    total_score = (
        (trend_strength * 35) +
        (rsi_score * 0.35) +
        (momentum * 15) +
        adx_bonus +
        macd_bonus +
        vol_bonus
    )

    return {
        "symbol":         symbol,
        "price":          price,
        "volume":         volume,
        "ema9":           ema9,
        "ema21":          ema21,
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
    Scan full universe and return top N stocks by score.
    Position size is 25% of cash for balanced deployment.
    """
    position_size = cash * 0.25
    print(f"\n-- Scanning {len(SCAN_UNIVERSE)} stocks | Budget: ${position_size:,.2f} per trade --")

    results = []
    for symbol in SCAN_UNIVERSE:
        result = score_stock(symbol, max_price=position_size)
        if result:
            results.append(result)
        time.sleep(0.1)  # rate limit protection

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
        macd_str = f" MACD={r['macd_hist']:.3f}" if r['macd_hist'] else ""
        print(f"  {r['symbol']}: score={r['score']:.1f} RSI={r['rsi']:.1f}{adx_str}{macd_str} price=${r['price']:.2f}")

    return top


def scan_best_etfs(profit_amount: float, top_n: int = 2) -> list:
    """
    Scan ETF universe and return best ETFs to buy with profit.
    """
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
    print("Running stock scan...")
    stocks = scan_best_stocks(500)
    print(f"\nBest stocks right now: {[s['symbol'] for s in stocks]}")

    print("\nRunning ETF scan...")
    etfs = scan_best_etfs(1000)
    print(f"Best ETFs right now: {[e['symbol'] for e in etfs]}")
