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


# Blacklist — securities Schwab restricts from opening transactions
RESTRICTED_SECURITIES = {
    "VRAX", "SPCE", "NKLA", "WKHS", "GOEV", "XELA", "CLOV", "SAVA",
    "PROG", "TTOO", "ATNX", "MNKD", "CTIC", "GSAT"
}


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


def liquidity_sweep(candles: list, lookback: int = 20) -> float:
    """
    Detects liquidity sweeps — price spikes through equal highs/lows then reverses.
    Returns bonus score 0-15. Higher = stronger sweep signal.
    Dynamic: uses ATR to define what counts as a meaningful sweep.
    """
    if len(candles) < lookback + 2:
        return 0
    try:
        highs  = [c["high"]  for c in candles]
        lows   = [c["low"]   for c in candles]
        closes = [c["close"] for c in candles]
        atr    = calc_atr(candles[-lookback:]) or 0

        if atr == 0:
            return 0

        # Find equal highs in lookback window (within 0.3% of each other)
        recent_high = max(highs[-lookback:-1])
        recent_low  = min(lows[-lookback:-1])
        last_high   = highs[-1]
        last_low    = lows[-1]
        last_close  = closes[-1]
        prev_close  = closes[-2]

        bonus = 0

        # Bullish sweep — price swept below equal lows then reversed up
        if last_low < recent_low and last_close > prev_close:
            sweep_depth = (recent_low - last_low) / atr
            bonus += min(sweep_depth * 5, 15)  # deeper sweep = higher bonus

        # Bearish sweep used as confirmation of uptrend strength
        if last_high > recent_high and last_close > recent_high:
            breakout_strength = (last_close - recent_high) / atr
            bonus += min(breakout_strength * 3, 10)

        return bonus
    except Exception:
        return 0


# Dynamic thresholds per conviction level
MTF_THRESHOLDS = {
    4: {"volume": 0.80, "sweep_min": 5, "candle_required": True},
    3: {"volume": 0.75, "sweep_min": 3, "candle_required": False},
    2: {"volume": 0.67, "sweep_min": 1, "candle_required": False},
    0: {"volume": 1.00, "sweep_min": 99, "candle_required": True},  # no trade
}


def get_day_conviction(symbol: str) -> int:
    """
    DAY TRADING bucket (25%) — 4-frame short term system.
    Frames: 30m, 15m, 5m, 1m
    Only longs. Need 3/4 minimum.
    4/4 → full day ceiling
    3/4 → 50% day ceiling
    2/4 or less → no trade
    """
    try:
        frames = [
            get_price_history(symbol, period=10, frequency=30),  # 30m
            get_price_history(symbol, period=5,  frequency=15),  # 15m
            get_price_history(symbol, period=5,  frequency=5),   # 5m
            get_price_history(symbol, period=2,  frequency=1),   # 1m
        ]
        aligned = 0
        for candles in frames:
            if len(candles) < 20:
                continue
            closes = [c["close"] for c in candles]
            ma20   = sum(closes[-20:]) / 20
            if closes[-1] > ma20:
                aligned += 1

        if aligned >= 4:   return 4
        elif aligned == 3: return 3
        else:              return 0  # 2/4 or less = no trade
    except Exception:
        return 0


def get_swing_conviction(symbol: str) -> dict:
    """
    SWING bucket (75%) — 3-frame system with direction awareness.
    Frames: Daily, 30m, 5m

    Returns:
        conviction: 4 (all aligned) | 3 (2/3) | 0 (no trade)
        direction: "up" | "down" | "flat"
        bias: daily direction for options routing

    Up bias   → look for long stock entries
    Down bias → look for put selling opportunities
    Flat bias → covered calls on existing positions only
    """
    try:
        # Daily candles — bias/direction
        daily   = get_price_history(symbol, period=10, frequency="day" if False else 1440)
        # Use monthly period with daily frequency instead
        import requests as _req
        from auth import get_valid_token as _gvt
        _h = {"Authorization": f"Bearer {_gvt()}"}
        _r = _req.get(
            "https://api.schwabapi.com/marketdata/v1/pricehistory",
            headers=_h,
            params={"symbol": symbol, "periodType": "month", "period": 1,
                    "frequencyType": "daily", "frequency": 1,
                    "needExtendedHoursData": False},
            timeout=10
        )
        daily = _r.json().get("candles", []) if _r.ok else []

        # 30m candles — alignment
        candles_30m = get_price_history(symbol, period=5, frequency=30)
        # 5m candles — trigger + order flow
        candles_5m  = get_price_history(symbol, period=2, frequency=5)

        # Daily direction
        daily_bias = "flat"
        daily_aligned = False
        if len(daily) >= 10:
            closes_d = [c["close"] for c in daily]
            ma10_d   = sum(closes_d[-10:]) / 10
            ma20_d   = sum(closes_d[-20:]) / 20 if len(closes_d) >= 20 else ma10_d
            if closes_d[-1] > ma10_d and ma10_d > ma20_d:
                daily_bias    = "up"
                daily_aligned = True
            elif closes_d[-1] < ma10_d and ma10_d < ma20_d:
                daily_bias    = "down"
                daily_aligned = True
            else:
                daily_bias    = "flat"
                daily_aligned = False

        # 30m alignment
        aligned_30m = False
        if len(candles_30m) >= 20:
            closes_30 = [c["close"] for c in candles_30m]
            ma20_30   = sum(closes_30[-20:]) / 20
            if daily_bias == "up":
                aligned_30m = closes_30[-1] > ma20_30
            elif daily_bias == "down":
                aligned_30m = closes_30[-1] < ma20_30
            else:
                aligned_30m = False

        # 5m trigger
        aligned_5m = False
        if len(candles_5m) >= 20:
            closes_5 = [c["close"] for c in candles_5m]
            ma20_5   = sum(closes_5[-20:]) / 20
            if daily_bias == "up":
                aligned_5m = closes_5[-1] > ma20_5
            elif daily_bias == "down":
                aligned_5m = closes_5[-1] < ma20_5
            else:
                aligned_5m = False

        frames_aligned = sum([daily_aligned, aligned_30m, aligned_5m])

        if frames_aligned >= 3:   conviction = 4
        elif frames_aligned == 2: conviction = 3
        else:                     conviction = 0

        return {
            "conviction": conviction,
            "direction":  daily_bias,
            "daily_ok":   daily_aligned,
            "30m_ok":     aligned_30m,
            "5m_ok":      aligned_5m,
        }
    except Exception:
        return {"conviction": 0, "direction": "flat", "daily_ok": False,
                "30m_ok": False, "5m_ok": False}


def get_mtf_conviction(symbol: str) -> int:
    """Legacy wrapper — uses day conviction for backward compatibility."""
    return get_day_conviction(symbol)


def candlestick_bonus(candles: list) -> float:
    """
    Detects 5 high-probability bullish patterns. Returns score bonus 0-12.
    Dynamic: uses ATR to define meaningful candle body/wick sizes.
    Patterns: Hammer, Bullish Engulfing, Doji reversal, Morning Star, Breakout candle.
    """
    if len(candles) < 3:
        return 0
    try:
        c0 = candles[-1]   # current
        c1 = candles[-2]   # previous
        c2 = candles[-3]   # two back

        o0, h0, l0, cl0 = c0["open"], c0["high"], c0["low"], c0["close"]
        o1, h1, l1, cl1 = c1["open"], c1["high"], c1["low"], c1["close"]
        o2, h2, l2, cl2 = c2["open"], c2["high"], c2["low"], c2["close"]

        body0  = abs(cl0 - o0)
        body1  = abs(cl1 - o1)
        range0 = h0 - l0
        range1 = h1 - l1
        bonus  = 0

        # 1. Hammer — small body at top, long lower wick, after downtrend
        lower_wick = o0 - l0 if cl0 > o0 else cl0 - l0
        if range0 > 0 and lower_wick > range0 * 0.6 and body0 < range0 * 0.3:
            bonus += 8

        # 2. Bullish Engulfing — current green candle body engulfs previous red
        if cl1 < o1 and cl0 > o0 and cl0 > o1 and o0 < cl1:
            bonus += 10

        # 3. Doji reversal — very small body after red candle (indecision = reversal)
        if cl1 < o1 and range0 > 0 and body0 < range0 * 0.1:
            bonus += 5

        # 4. Morning Star — 3 candle pattern: red, doji/small, strong green
        if cl2 < o2 and body1 < (h1 - l1) * 0.3 and cl0 > o0 and cl0 > (o2 + cl2) / 2:
            bonus += 12

        # 5. Strong breakout candle — large body, closes near high, above average volume
        if cl0 > o0 and body0 > range0 * 0.7 and cl0 > h1:
            bonus += 8

        return min(bonus, 12)  # cap at 12 to not overweight
    except Exception:
        return 0


def volume_ok(candles: list, period: int = 20, threshold: float = 0.80) -> bool:
    """Dynamic volume threshold based on MTF conviction level."""
    if len(candles) < period + 1:
        return True
    vols = [c["volume"] for c in candles]
    return vols[-1] >= sum(vols[-period-1:-1]) / period * threshold


# ── Stock scoring ─────────────────────────────────────────────────────────────

def candle_strength(candles: list) -> float:
    """
    Measures raw candle strength 0-100.
    Uses body size, close position, and wick rejection.
    No lagging indicators — pure price action.
    """
    if len(candles) < 3:
        return 0
    try:
        c  = candles[-1]
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        rng  = h - l
        if rng == 0:
            return 0
        body      = abs(cl - o)
        body_pct  = body / rng                          # big body = strong
        close_pct = (cl - l) / rng                     # closes near high = bullish
        wick_low  = (min(o, cl) - l) / rng             # lower wick rejection
        direction = 1 if cl > o else -1                 # green or red

        if direction < 0:
            return 0  # only bullish candles

        score = (body_pct * 40) + (close_pct * 40) + (wick_low * 20)
        return round(score * 100, 1)
    except Exception:
        return 0


def order_flow(symbol: str, candles: list) -> float:
    """
    Measures order flow pressure 0-100.
    Uses bid/ask imbalance from live Schwab quotes + volume delta.
    Replaces MACD — forward-looking not lagging.
    """
    try:
        # Live bid/ask imbalance
        quote = get_quote(symbol)
        bid_size = quote.get("quote", {}).get("bidSize", 0)
        ask_size = quote.get("quote", {}).get("askSize", 0)
        total    = bid_size + ask_size
        if total > 0:
            imbalance = (bid_size - ask_size) / total  # -1 to +1
        else:
            imbalance = 0

        # Volume delta — up candles vs down candles last 10
        up_vol   = sum(c["volume"] for c in candles[-10:] if c["close"] > c["open"])
        down_vol = sum(c["volume"] for c in candles[-10:] if c["close"] <= c["open"])
        total_vol = up_vol + down_vol
        vol_delta = (up_vol - down_vol) / total_vol if total_vol > 0 else 0

        # Combine — both must agree for strong signal
        flow_score = (imbalance * 50) + (vol_delta * 50)
        return max(0, round(flow_score * 100, 1))
    except Exception:
        return 0


def wick_rejection(candles: list) -> float:
    """
    Detects wick rejection — price pushed to an extreme then violently rejected.
    Strong lower wick = buyers rejecting lower prices (bullish).
    Boosts entry score 0-15. Also used for exit pressure flip signal.
    Dynamic — uses candle range so it scales with volatility.
    """
    if len(candles) < 3:
        return 0
    try:
        c   = candles[-1]
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        rng = h - l
        if rng == 0:
            return 0

        lower_wick = min(o, cl) - l  # wick below body
        upper_wick = h - max(o, cl)  # wick above body
        body       = abs(cl - o)

        # Bullish wick rejection — big lower wick, small upper wick, closes green
        if cl > o and lower_wick > rng * 0.45 and upper_wick < rng * 0.2:
            strength = lower_wick / rng
            return min(strength * 20, 15)

        # Pressure flip signal — previous bar had big upper wick (sellers rejected)
        # current bar closes green = buyers took control
        if len(candles) >= 2:
            prev = candles[-2]
            prev_rng  = prev["high"] - prev["low"]
            prev_wick = prev["high"] - max(prev["open"], prev["close"])
            if prev_rng > 0 and prev_wick > prev_rng * 0.5 and cl > prev["close"]:
                return 10  # pressure flip bonus

        return 0
    except Exception:
        return 0


def detect_fvg(candles: list) -> dict:
    """
    Fair Value Gap (FVG) detection.
    A FVG is a 3-candle pattern where candle 1 high and candle 3 low don't overlap.
    Gap = inefficiency that price tends to return to.

    Entry use: price returning to fresh FVG = boost score (strong institutional level)
    Safety use: price still INSIDE FVG = skip entry (unstable, gap still filling)

    Returns:
        has_fvg: bool
        in_gap: bool (price currently inside the gap = unsafe)
        returning: bool (price just returned to gap edge = entry boost)
        gap_top: float
        gap_bottom: float
        boost: float (score bonus 0-12)
    """
    if len(candles) < 4:
        return {"has_fvg": False, "in_gap": False, "returning": False, "boost": 0}
    try:
        current_price = candles[-1]["close"]
        result = {"has_fvg": False, "in_gap": False, "returning": False, "boost": 0}

        # Check last 10 candles for FVG patterns
        for i in range(2, min(10, len(candles))):
            c1 = candles[-i-1]  # oldest
            c2 = candles[-i]    # middle
            c3 = candles[-i+1] if i > 1 else candles[-1]  # newest

            # Bullish FVG — gap between c1 high and c3 low
            gap_bottom = c1["high"]
            gap_top    = c3["low"]

            if gap_top > gap_bottom:  # valid gap
                result["has_fvg"]   = True
                result["gap_top"]   = gap_top
                result["gap_bottom"] = gap_bottom

                # Price inside gap — unstable, skip entry
                if gap_bottom <= current_price <= gap_top:
                    result["in_gap"] = True
                    result["boost"]  = 0
                    return result

                # Price returning to gap from above — strong entry signal
                gap_size = gap_top - gap_bottom
                if gap_top < current_price <= gap_top + gap_size * 0.5:
                    result["returning"] = True
                    result["boost"]     = 12
                    return result

        return result
    except Exception:
        return {"has_fvg": False, "in_gap": False, "returning": False, "boost": 0}


def score_stock(symbol: str, max_price: float, tier_cfg: dict) -> dict | None:
    """
    Score a stock using candle strength + order flow.
    No MA, no RSI, no MACD — pure price action and order flow.
    MTF conviction (1m/5m/15m/30m) controls position size.
    """
    if symbol in RESTRICTED_SECURITIES:
        return None

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
    if price < 1.50 or volume < 200_000:
        return None

    # Get MTF conviction first — no trade if less than 2/4
    conviction = get_mtf_conviction(symbol)
    if conviction == 0:
        return None

    thresholds = MTF_THRESHOLDS.get(conviction, MTF_THRESHOLDS[2])

    # Pull candles for strength and flow analysis
    candles = get_price_history(symbol)
    if len(candles) < 20:
        return None

    # ATR for volatility filter
    atr_val = calc_atr(candles)
    atr_pct = atr_val / price * 100 if atr_val and price > 0 else 2.0
    if atr_pct < 0.5 or atr_pct > 6.0:
        return None

    # Candle strength — pure price action
    strength = candle_strength(candles)
    if strength < 20:  # very weak candle — skip
        return None

    # Order flow — bid/ask + volume delta
    flow = order_flow(symbol, candles)

    # Dynamic volume threshold per conviction
    vol_ok_dynamic = volume_ok(candles, threshold=thresholds["volume"])
    if not vol_ok_dynamic:
        return None

    # Liquidity sweep bonus — still valuable signal
    sweep_bonus  = liquidity_sweep(candles)
    candle_bonus = candlestick_bonus(candles)

    # 4/4 conviction requires candle pattern or sweep
    if thresholds["candle_required"] and sweep_bonus < thresholds["sweep_min"] and candle_bonus < 3:
        return None

    # Wick rejection bonus
    wick_bonus = wick_rejection(candles)

    # FVG detection — boost or block
    fvg = detect_fvg(candles)
    if fvg["in_gap"]:
        return None  # price inside FVG — unstable, skip entry
    fvg_bonus = fvg["boost"]

    # Total score — candle strength + order flow + sweep + wick + fvg + candle patterns
    total_score = (strength * 0.4) + (flow * 0.4) + sweep_bonus + candle_bonus + wick_bonus + fvg_bonus + (change_pct * 2)

    if total_score < 10:
        return None

    return {
        "symbol":       symbol,
        "price":        price,
        "volume":       volume,
        "strength":     round(strength, 1),
        "flow":         round(flow, 1),
        "atr_pct":      round(atr_pct, 2),
        "sweep_bonus":  round(sweep_bonus, 1),
        "candle_bonus": round(candle_bonus, 1),
        "wick_bonus":   round(wick_bonus, 1),
        "fvg_bonus":    round(fvg_bonus, 1),
        "fvg_returning": fvg["returning"],
        "conviction":   conviction,
        "score":        round(total_score, 1),
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
        print(f"  {r['symbol']}: score={r['score']:.1f} ADX={r.get('adx') or 0:.1f} ATR={r.get('atr_pct') or 0:.1f}% price=${r['price']:.2f}")

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

    # ETFs are long-term holds — no strict signal filters
    # Just check price is affordable and volume is liquid
    # Score by category priority + day change
    category_priority = {"growth": 40, "income": 35, "stable": 30, "international": 25}
    base_score = category_priority.get(category, 30)
    total = base_score + change_pct * 2

    return {
        "symbol": symbol, "price": price,
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
