import requests
import time
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"

# ── Dynamic Tier System (bot capital only) ────────────────────────────────────
# Tiers blend smoothly — no hard walls. Settings interpolate between tiers.
BOT_TIERS = {
    "Tier 1": {"min_price": 2.0,  "min_adx": 18, "rsi_low": 25, "rsi_high": 75, "min_volume": 300_000,   "top_n": 3,  "pos_pct": 0.30, "min_score": 35, "max_trades": 3,  "label": "Building (<$5k)",      "threshold": 0},
    "Tier 2": {"min_price": 2.0,  "min_adx": 20, "rsi_low": 28, "rsi_high": 72, "min_volume": 500_000,   "top_n": 5,  "pos_pct": 0.25, "min_score": 40, "max_trades": 5,  "label": "Growth ($5k-$15k)",    "threshold": 5000},
    "Tier 3": {"min_price": 5.0,  "min_adx": 23, "rsi_low": 30, "rsi_high": 70, "min_volume": 750_000,   "top_n": 7,  "pos_pct": 0.20, "min_score": 50, "max_trades": 7,  "label": "Scaling ($15k-$50k)",  "threshold": 15000},
    "Tier 4": {"min_price": 10.0, "min_adx": 25, "rsi_low": 32, "rsi_high": 68, "min_volume": 1_000_000, "top_n": 10, "pos_pct": 0.15, "min_score": 60, "max_trades": 10, "label": "Income ($50k+)",       "threshold": 50000},
}

ETF_LEVELS = {
    "Level 1": {"bot_feed": 0.60, "cash": 0.30, "reinvest": 0.10, "label": "Accumulating (<$10k)"},
    "Level 2": {"bot_feed": 0.50, "cash": 0.30, "reinvest": 0.20, "label": "Growing ($10k-$30k)"},
    "Level 3": {"bot_feed": 0.30, "cash": 0.40, "reinvest": 0.30, "label": "Mature ($30k+)"},
}

ETF_CATEGORIES = {
    "growth":        {"symbols": ["VOO", "QQQ", "SCHG", "SCHB", "VTI"],                    "reinvest": 1.0,  "cash": 0.0},
    "income":        {"symbols": ["SCHD", "JEPI", "JEPQ", "VYM", "HDV", "DGRO", "DIVO"],  "reinvest": 0.5,  "cash": 0.5},
    "stable":        {"symbols": ["SGOV", "USFR", "JPST", "FLOT"],                         "reinvest": 0.0,  "cash": 1.0},
    "international": {"symbols": ["VEA", "VWO", "SCHF"],                                   "reinvest": 0.75, "cash": 0.25},
}

ETF_DIVIDEND_RULES = {cat: {"reinvest": v["reinvest"], "cash": v["cash"]} for cat, v in ETF_CATEGORIES.items()}


def get_tier(bot_capital: float) -> tuple:
    """Smooth tier — blends settings as capital grows, no hard jumps."""
    tiers = list(BOT_TIERS.items())
    for i, (name, cfg) in enumerate(reversed(tiers)):
        if bot_capital >= cfg["threshold"]:
            # Blend toward next tier if within 20% of next threshold
            tier_idx = len(tiers) - 1 - i
            if tier_idx < len(tiers) - 1:
                next_name, next_cfg = tiers[tier_idx + 1]
                next_threshold = next_cfg["threshold"]
                progress = (bot_capital - cfg["threshold"]) / (next_threshold - cfg["threshold"])
                if progress > 0.8:
                    # 80%+ of the way to next tier — start blending
                    blend = (progress - 0.8) / 0.2  # 0 to 1
                    blended = {}
                    for key in ["min_adx", "rsi_low", "rsi_high", "min_score", "max_trades"]:
                        blended[key] = cfg[key] + (next_cfg[key] - cfg[key]) * blend
                    blended.update({k: cfg[k] for k in ["min_price", "min_volume", "top_n", "pos_pct", "label"]})
                    return name, blended
            return name, cfg
    return "Tier 1", BOT_TIERS["Tier 1"]


def get_etf_level(etf_capital: float) -> tuple:
    if etf_capital < 10000:
        return "Level 1", ETF_LEVELS["Level 1"]
    elif etf_capital < 30000:
        return "Level 2", ETF_LEVELS["Level 2"]
    else:
        return "Level 3", ETF_LEVELS["Level 3"]


def get_etf_category(symbol: str) -> str:
    for cat, data in ETF_CATEGORIES.items():
        if symbol in data["symbols"]:
            return cat
    return "growth"


# ── Schwab API helpers ────────────────────────────────────────────────────────

def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_quote(symbol: str) -> dict | None:
    try:
        resp = requests.get(f"{BASE_URL}/quotes", headers=headers(), params={"symbols": symbol}, timeout=10)
        resp.raise_for_status()
        return resp.json().get(symbol)
    except Exception:
        return None


def get_price_history(symbol: str, period: int = 10, frequency: int = 5) -> list:
    try:
        resp = requests.get(
            f"{BASE_URL}/pricehistory", headers=headers(),
            params={"symbol": symbol, "periodType": "day", "period": period,
                    "frequencyType": "minute", "frequency": frequency, "needExtendedHoursData": False},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("candles", [])
    except Exception:
        return []


def get_movers(index: str = "$SPX", direction: str = "up", top: int = 40) -> list:
    try:
        resp = requests.get(f"{BASE_URL}/movers/{index}", headers=headers(),
                            params={"sort": direction, "frequency": 1}, timeout=10)
        resp.raise_for_status()
        return [s["symbol"] for s in resp.json().get("screeners", []) if s.get("symbol")][:top]
    except Exception:
        return []


def get_dynamic_universe() -> list:
    """Live movers from Schwab API — S&P 500, Nasdaq, Dow. Deduped."""
    symbols = get_movers("$SPX", "up", 40) + get_movers("$COMPX", "up", 40) + get_movers("$DJI", "up", 20)
    seen, unique = set(), []
    for s in symbols:
        if s not in seen and not s.startswith("$"):
            seen.add(s)
            unique.append(s)
    print(f"Dynamic universe: {len(unique)} live movers")
    return unique


def get_market_pulse() -> str:
    """One-line market summary using live Schwab movers. No overlap between hot/cold."""
    try:
        gainers = list(dict.fromkeys(get_movers("$SPX", "up", 5) + get_movers("$COMPX", "up", 5)))[:5]
        losers  = [s for s in get_movers("$SPX", "down", 3) if s not in gainers][:3]
        return f"🔥 {' '.join(gainers) or 'none'} | 📉 {' '.join(losers) or 'quiet'}"
    except Exception:
        return ""


# ── Technical indicators ──────────────────────────────────────────────────────

def ema(prices: list, period: int) -> float | None:
    if len(prices) < period:
        return None
    k, val = 2 / (period + 1), sum(prices[:period]) / period
    for p in prices[period:]:
        val = p * k + val * (1 - k)
    return val


def rsi(prices: list, period: int = 14) -> float | None:
    if len(prices) < period + 1:
        return None
    gains = [max(prices[i] - prices[i-1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i-1] - prices[i], 0) for i in range(1, len(prices))]
    ag, al = sum(gains[-period:]) / period, sum(losses[-period:]) / period
    return 100 if al == 0 else 100 - (100 / (1 + ag / al))


def macd_hist(prices: list) -> float | None:
    if len(prices) < 35:
        return None
    e12, e26 = ema(prices, 12), ema(prices, 26)
    if not e12 or not e26:
        return None
    macd_vals = [ema(prices[:i], 12) - ema(prices[:i], 26)
                 for i in range(26, len(prices) + 1)
                 if ema(prices[:i], 12) and ema(prices[:i], 26)]
    if len(macd_vals) < 9:
        return None
    sig = ema(macd_vals, 9)
    return (e12 - e26) - sig if sig else None


def calc_adx(candles: list, period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    try:
        H = [c["high"] for c in candles]
        L = [c["low"] for c in candles]
        C = [c["close"] for c in candles]
        tr_list, dmp, dmm = [], [], []
        for i in range(1, len(candles)):
            tr_list.append(max(H[i]-L[i], abs(H[i]-C[i-1]), abs(L[i]-C[i-1])))
            hd, ld = H[i]-H[i-1], L[i-1]-L[i]
            dmp.append(hd if hd > ld and hd > 0 else 0)
            dmm.append(ld if ld > hd and ld > 0 else 0)
        atr = sum(tr_list[-period:]) / period
        if atr == 0:
            return None
        dip = (sum(dmp[-period:]) / period) / atr * 100
        dim = (sum(dmm[-period:]) / period) / atr * 100
        return abs(dip - dim) / (dip + dim) * 100 if (dip + dim) > 0 else 0
    except Exception:
        return None


def calc_atr(candles: list, period: int = 14) -> float | None:
    """Average True Range — measures volatility as % of price."""
    if len(candles) < period + 1:
        return None
    try:
        trs = [max(candles[i]["high"] - candles[i]["low"],
                   abs(candles[i]["high"] - candles[i-1]["close"]),
                   abs(candles[i]["low"]  - candles[i-1]["close"]))
               for i in range(1, len(candles))]
        return sum(trs[-period:]) / period
    except Exception:
        return None


def volume_ok(candles: list, period: int = 20) -> bool:
    if len(candles) < period + 1:
        return True
    vols = [c["volume"] for c in candles]
    return vols[-1] >= sum(vols[-period-1:-1]) / period * 0.8


# ── Stock scoring ─────────────────────────────────────────────────────────────

def score_stock(symbol: str, max_price: float, tier_cfg: dict) -> dict | None:
    """Score a stock using live Schwab data. All filters dynamic via tier_cfg."""
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
    if price < tier_cfg["min_price"]:
        return None
    if volume < tier_cfg["min_volume"]:
        return None
    # Hard minimums regardless of tier — avoid restricted/halted securities
    if price < 1.50 or volume < 200_000:
        return None

    candles = get_price_history(symbol)
    if len(candles) < 30:
        return None

    closes = [c["close"] for c in candles]
    ema9, ema21 = ema(closes, 9), ema(closes, 21)
    rsi14 = rsi(closes, 14)
    if not ema9 or not ema21 or not rsi14:
        return None
    if ema9 <= ema21:
        return None
    if rsi14 >= tier_cfg["rsi_high"] or rsi14 <= tier_cfg["rsi_low"]:
        return None

    adx_val = calc_adx(candles)
    if adx_val is not None and adx_val < tier_cfg["min_adx"]:
        return None

    hist = macd_hist(closes)
    if hist is not None and hist < 0:
        return None

    # ATR filter — dynamic: stock must have ATR between 0.5% and 6% of price
    atr_val = calc_atr(candles)
    if atr_val and price > 0:
        atr_pct = atr_val / price * 100
        if atr_pct < 0.5 or atr_pct > 6.0:
            return None  # too flat or too volatile

    trend_strength = ((ema9 - ema21) / ema21) * 100
    rsi_score      = 100 - abs(rsi14 - 55)
    adx_bonus      = min(adx_val, 50) * 0.3 if adx_val else 0
    macd_bonus     = min(hist * 100, 10) if hist and hist > 0 else 0
    vol_bonus      = 5 if volume_ok(candles) else 0

    total_score = (trend_strength * 35) + (rsi_score * 0.35) + (change_pct * 15) + adx_bonus + macd_bonus + vol_bonus

    # Hard minimum score — nothing under 6-7 passes regardless of tier
    if total_score < max(tier_cfg.get("min_score", 35), 6):
        return None

    return {
        "symbol": symbol, "price": price, "volume": volume,
        "rsi": rsi14, "adx": adx_val, "macd_hist": hist,
        "atr_pct": round(atr_pct, 2) if atr_val and price > 0 else None,
        "score": total_score,
    }


def scan_best_stocks(cash: float, bot_capital: float = 2400) -> list:
    """Scan live movers, score with dynamic tier filters, return top N."""
    tier_name, tier_cfg = get_tier(bot_capital)
    position_size = cash * tier_cfg["pos_pct"]
    top_n         = tier_cfg["top_n"]

    universe = get_dynamic_universe()
    if not universe:
        return []

    print(f"\n-- Scanning {len(universe)} movers | {tier_name} | Budget: ${position_size:,.2f} --")

    results = []
    for symbol in universe:
        r = score_stock(symbol, position_size, tier_cfg)
        if r:
            results.append(r)
        time.sleep(0.1)

    results.sort(key=lambda x: x["score"], reverse=True)
    seen, unique = set(), []
    for r in results:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            unique.append(r)
    top = unique[:top_n]

    for r in top:
        print(f"  {r['symbol']}: score={r['score']:.1f} RSI={r['rsi']:.1f} ADX={r['adx'] or 0:.1f} ATR={r['atr_pct'] or 0:.1f}% price=${r['price']:.2f}")

    return top


# ── ETF scanning ──────────────────────────────────────────────────────────────

def score_etf(symbol: str, max_price: float, category: str) -> dict | None:
    quote = get_quote(symbol)
    if not quote:
        return None
    try:
        price      = quote["quote"]["lastPrice"]
        volume     = quote["quote"]["totalVolume"]
        change_pct = quote["quote"].get("netPercentChangeInDouble", 0)
    except (KeyError, TypeError):
        return None

    if price <= 0 or price > max_price or volume < 100000:
        return None

    candles = get_price_history(symbol, period=20)
    if len(candles) < 20:
        return None

    closes = [c["close"] for c in candles]
    ema9, ema21, rsi14 = ema(closes, 9), ema(closes, 21), rsi(closes, 14)
    if not ema9 or not ema21 or not rsi14:
        return None
    if rsi14 >= 78 or rsi14 <= 25:
        return None
    if category != "stable" and ema9 <= ema21:
        return None

    trend_score = ((ema9 - ema21) / ema21) * 100 if ema21 > 0 else 0
    rsi_score   = 100 - abs(rsi14 - 55)
    weight = (20, 0.5, 5) if category in ("income", "stable") else (35, 0.35, 15)
    total  = trend_score * weight[0] + rsi_score * weight[1] + change_pct * weight[2]

    return {
        "symbol": symbol, "price": price, "rsi": rsi14,
        "category": category, "score": total,
        "dividend_rule": ETF_DIVIDEND_RULES.get(category, ETF_DIVIDEND_RULES["growth"])
    }


def scan_best_etfs(profit_amount: float, top_n: int = 1) -> list:
    all_etfs = []
    for cat, data in ETF_CATEGORIES.items():
        for symbol in data["symbols"]:
            r = score_etf(symbol, profit_amount, cat)
            if r:
                all_etfs.append(r)
            time.sleep(0.1)

    all_etfs.sort(key=lambda x: x["score"], reverse=True)
    seen, unique = set(), []
    for r in all_etfs:
        if r["symbol"] not in seen:
            seen.add(r["symbol"])
            unique.append(r)
    top = unique[:top_n]

    print(f"\n-- ETF scan | ${profit_amount:,.2f} available --")
    for r in top:
        print(f"  {r['symbol']} ({r['category']}): score={r['score']:.1f} price=${r['price']:.2f}")
    return top
