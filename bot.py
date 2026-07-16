"""
Schwab Auto-Trading Bot
Figure-8 Capital System | Star Rating | Room-Based Ceiling | Delta Trail | Green-Day Protect
"""

import os
import sys
import signal
import schedule
import time
import requests
from datetime import datetime
import pytz
from dotenv import load_dotenv
from auth import get_valid_token
from scanner import (
    scan_best_stocks, scan_best_etfs, get_market_pulse,
    get_tier, get_etf_level, ETF_DIVIDEND_RULES, get_etf_category,
    get_day_conviction, get_swing_conviction
)
from options import (
    find_best_covered_call, place_covered_call, check_covered_call_already_open,
    find_best_cash_secured_put, place_cash_secured_put, check_put_already_open
)
from dividends import get_recent_dividends
from telegram import send_alert
from token_manager import check_token_health
from ledger import (
    sync_ledger_from_schwab, load_ledger, save_ledger,
    record_buy, record_sell_and_split,
    get_profit_bucket, get_trading_capital,
    get_etf_bucket, get_cash_bucket,
    deduct_etf_bucket,
    record_dividend, get_dividend_stats, mark_dividend_seen,
    update_high_price, get_trailing_stop_info, get_dynamic_stop,
    get_pressure_trail,
    BOT_STOCKS, ETF_MIN_SWEEP
)
from tax import record_taxable_event, send_tax_alert, sync_schwab_tax_history

load_dotenv()

BASE_URL          = "https://api.schwabapi.com/trader/v1"
MARKET_URL        = "https://api.schwabapi.com/marketdata/v1"
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", 0.07))
CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL_MINUTES", 30))

# ── Profit splits ─────────────────────────────────────────────────────────────
STOCK_SPLIT   = {"etf": 0.60, "cash": 0.30, "bot": 0.10}
OPTIONS_SPLIT = {"etf": 0.60, "cash": 0.40, "bot": 0.00}
ETF_SPLITS    = {
    "Level 1": {"bot": 0.60, "cash": 0.30, "etf": 0.10},
    "Level 2": {"bot": 0.50, "cash": 0.30, "etf": 0.20},
    "Level 3": {"bot": 0.30, "cash": 0.40, "etf": 0.30},
}

# ── Capital bucket split ───────────────────────────────────────────────────────
DAY_PCT   = 0.25   # 25% for day trading (30/15/5/1m)
SWING_PCT = 0.75   # 75% for swing + options (Daily/30m/5m)

# PDT rule — max 3 day trades in 5 days under $25k
MAX_DAY_TRADES_PER_WEEK = 3

# ── Safety floor ──────────────────────────────────────────────────────────────
BOT_FLOOR = 500.0  # never let bot capital drop below this


def handle_shutdown(signum, frame):
    print("Shutdown signal received — bot stopping cleanly.")
    sys.exit(0)

signal.signal(signal.SIGINT,  handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


# ── Schwab API helpers ────────────────────────────────────────────────────────

def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_account_numbers() -> list:
    resp = requests.get(f"{BASE_URL}/accounts/accountNumbers", headers=headers())
    resp.raise_for_status()
    return resp.json()


def get_account(encrypted: str) -> dict:
    resp = requests.get(f"{BASE_URL}/accounts/{encrypted}",
                        headers=headers(), params={"fields": "positions"})
    resp.raise_for_status()
    return resp.json()


def get_cash_balance(account: dict) -> float:
    try:
        return max(account["securitiesAccount"]["currentBalances"].get("cashBalance", 0.0), 0.0)
    except KeyError:
        return 0.0


def get_available_cash(account: dict) -> float:
    try:
        b = account["securitiesAccount"]["currentBalances"]
        a = b.get("cashAvailableForTrading")
        return max(a if a is not None else b.get("cashBalance", 0.0), 0.0)
    except KeyError:
        return 0.0


def get_portfolio_value(account: dict) -> float:
    try:
        return account["securitiesAccount"]["currentBalances"]["liquidationValue"]
    except KeyError:
        return 0.0


def get_positions(account: dict) -> list:
    try:
        return account["securitiesAccount"]["positions"]
    except KeyError:
        return []


def get_position_for(positions: list, symbol: str) -> dict | None:
    for p in positions:
        if p["instrument"]["symbol"] == symbol:
            return p
    return None


def get_cash_on_hold(account: dict) -> float:
    try:
        return account["securitiesAccount"]["currentBalances"].get("cashEquityPut", 0.0)
    except KeyError:
        return 0.0


def get_live_bid(symbol: str) -> float | None:
    """Get real-time bid price from Schwab for delta trail."""
    try:
        resp = requests.get(f"{MARKET_URL}/quotes", headers=headers(),
                            params={"symbols": symbol}, timeout=5)
        resp.raise_for_status()
        data = resp.json().get(symbol, {})
        return data.get("quote", {}).get("bidPrice")
    except Exception:
        return None


# ── Market hours ──────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    try:
        from datetime import date as _d
        resp = requests.get(
            f"{MARKET_URL}/markets", headers=headers(),
            params={"markets": "equity", "date": _d.today().strftime("%Y-%m-%d")},
            timeout=10
        )
        if not resp.ok:
            raise Exception(f"API {resp.status_code}")
        equity = resp.json().get("equity", {})
        for key in ["EQ", "equity"]:
            if key in equity:
                mkt = equity[key]
                if not mkt.get("isOpen", False):
                    print("Market closed — Schwab API says closed today")
                    return False
                et  = pytz.timezone("America/New_York")
                now = datetime.now(et)
                session = mkt.get("sessionHours", {}).get("regularMarket", [{}])[0]
                s, e = session.get("start", ""), session.get("end", "")
                if s and e:
                    from datetime import datetime as _dt
                    if not (_dt.fromisoformat(s).astimezone(et) <= now <= _dt.fromisoformat(e).astimezone(et)):
                        print(f"Market closed — {now.strftime('%H:%M')} ET")
                        return False
                return True
        return False
    except Exception as ex:
        print(f"Market hours API error: {ex} — fallback")
        et  = pytz.timezone("America/New_York")
        now = datetime.now(et)
        if now.weekday() > 4:
            return False
        return now.replace(hour=9, minute=30) <= now <= now.replace(hour=16, minute=0)


# ── Star Rating (1-10) ────────────────────────────────────────────────────────

def get_star_rating(stock: dict) -> int:
    """Grade signal quality 1-10 for notification only. Does not affect entry or sizing."""
    stars = 0
    sweep = stock.get("sweep_bonus", 0)
    if sweep >= 10:  stars += 3
    elif sweep >= 5: stars += 2
    elif sweep >= 1: stars += 1
    adx = stock.get("adx") or 0
    if adx >= 30:   stars += 2
    elif adx >= 22: stars += 1
    hist = stock.get("macd_hist") or 0
    if hist >= 0.05:   stars += 2
    elif hist >= 0.01: stars += 1
    flow = stock.get("flow", 0)
    if flow >= 60: stars += 1
    vol = stock.get("volume", 0)
    if vol > 0: stars += 1
    return min(max(stars, 1), 10)


def get_day_position_size(symbol: str, capital: float, fvg_confirmed: bool = False, stars: int = 0) -> float:
    """
    Day trading bucket (25% of capital).
    Conviction only for sizing. Stars 7+ required to qualify.
    Frames: 30m/15m/5m/1m

    4/4        → full ceiling
    3/4 + FVG  → 2.5 fires at 70% ceiling
    3/4 no FVG → 50% ceiling (still trades)
    2/4        → no trade always
    Stars 7+   → required to qualify
    """
    if stars < 7:
        return 0  # stars 7+ required for day trades

    day_capital = capital * DAY_PCT
    room        = day_capital - 200
    if room <= 0:
        return 0
    ceiling = min(room * 0.40, 200)

    conviction = get_day_conviction(symbol)

    if conviction >= 4:
        return ceiling              # 4/4 full
    elif conviction == 3 and fvg_confirmed:
        return ceiling * 0.70       # 2.5 with FVG → 70% ceiling
    elif conviction == 3:
        return ceiling * 0.50       # 3/4 no FVG → 50% ceiling (still trades)
    else:
        return 0                    # 2/4 or less → no trade


def get_swing_position_size(symbol: str, capital: float, stock: dict = None) -> tuple:
    """
    Swing bucket (75% of capital).
    Frames: Daily/30m/5m
    Minimum to fire: 3.0/4 conviction
    Stars LOCK IN the size (7+ required):
    Stars 7  → 50% ceiling
    Stars 8  → 75% ceiling
    Stars 9  → 90% ceiling
    Stars 10 → full ceiling
    Stars <7 → no trade
    Returns (size, direction, conviction_info)
    """
    swing_capital = capital * SWING_PCT
    room          = swing_capital - 300
    if room <= 0:
        return 0, "flat", {}
    ceiling = get_ceiling(swing_capital)

    info = get_swing_conviction(symbol)
    conv = info["conviction"]

    # Minimum 3.0/4 to fire — no FVG shortcut for swing
    if conv < 3:
        return 0, info.get("direction", "flat"), info

    # Stars lock in size — 7+ required
    stars = get_star_rating(stock) if stock else 0
    if stars < 7:
        return 0, info.get("direction", "flat"), info

    if stars >= 10:    size = ceiling
    elif stars >= 9:   size = ceiling * 0.90
    elif stars >= 8:   size = ceiling * 0.75
    else:              size = ceiling * 0.50  # stars 7

    return size, info["direction"], info


# ── Room-based ceiling ────────────────────────────────────────────────────────

def get_ceiling(bot_capital: float) -> float:
    """
    Position ceiling based on room (capital above safety floor).
    Protects thin accounts, opens up as capital grows.
    """
    room = bot_capital - BOT_FLOOR
    if room <= 0:      return 0
    if room < 500:     return 50
    if room < 1000:    return 100
    if room < 2000:    return 200
    if room < 5000:    return 400
    if room < 10000:   return 600
    return 1000





# ── Daily stats ───────────────────────────────────────────────────────────────

def get_daily_stats() -> dict:
    ledger = load_ledger()
    today  = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    if ledger.get("daily_stats_date") != today:
        # Reset weekly PDT counter on Monday
        now = datetime.now(pytz.timezone("America/New_York"))
        if now.weekday() == 0:  # Monday
            ledger["day_trades_this_week"] = 0
        ledger.update({
            "daily_stats_date":   today,
            "daily_loss_stock":   0.0,
            "daily_loss_options": 0.0,
            "daily_profit":       0.0,
            "daily_peak":         0.0,
            "trades_today":       0,
            "consecutive_losses": 0,
        })
        save_ledger(ledger)
    return {
        "trades_today":       ledger.get("trades_today", 0),
        "consecutive_losses": ledger.get("consecutive_losses", 0),
        "daily_loss_stock":   ledger.get("daily_loss_stock", 0.0),
        "daily_loss_options": ledger.get("daily_loss_options", 0.0),
        "daily_profit":       ledger.get("daily_profit", 0.0),
        "daily_peak":         ledger.get("daily_peak", 0.0),
    }


def record_conviction_count(conviction: int):
    """Track how many times each conviction level fired today."""
    ledger = load_ledger()
    key = f"conviction_{conviction}_count"
    ledger[key] = ledger.get(key, 0) + 1
    save_ledger(ledger)


def record_trade_result(profit: float, trade_type: str = "stock"):
    """Update daily stats after every trade."""
    ledger = load_ledger()
    ledger["trades_today"] = ledger.get("trades_today", 0) + 1
    if profit > 0:
        ledger["daily_profit"]       = ledger.get("daily_profit", 0.0) + profit
        ledger["daily_peak"]         = max(ledger.get("daily_peak", 0.0), ledger["daily_profit"])
        ledger["consecutive_losses"] = 0
    else:
        if trade_type == "options":
            ledger["daily_loss_options"] = ledger.get("daily_loss_options", 0.0) + abs(profit)
        else:
            ledger["daily_loss_stock"] = ledger.get("daily_loss_stock", 0.0) + abs(profit)
        ledger["consecutive_losses"] = ledger.get("consecutive_losses", 0) + 1
    save_ledger(ledger)


def update_win_rate(profit: float):
    ledger  = load_ledger()
    history = ledger.get("win_rate_history", [])
    history.append(1 if profit > 0 else 0)
    ledger["win_rate_history"]  = history[-10:]
    ledger["current_win_rate"]  = sum(ledger["win_rate_history"]) / len(ledger["win_rate_history"])
    save_ledger(ledger)


def get_win_rate() -> float:
    ledger  = load_ledger()
    history = ledger.get("win_rate_history", [])
    return sum(history) / len(history) if history else 1.0


# ── Can trade gatekeeper ──────────────────────────────────────────────────────

def can_trade(capital: float, stats: dict, _unused: int = 0) -> tuple:
    """
    Dynamic gatekeeper — all checks before any buy.
    MTF conviction handles sizing. This handles daily limits.
    Returns (bool, reason).
    """
    # Warmup — skip 9:30-9:45 ET
    et  = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.replace(hour=9, minute=30) <= now <= now.replace(hour=9, minute=45):
        return False, "warmup"

    # Cooldown — 2 consecutive losses
    if stats["consecutive_losses"] >= 2:
        return False, "cooldown_2_losses"

    # Win rate gate
    wr = get_win_rate()
    if wr < 0.40:
        return False, f"win_rate_{wr:.0%}"

    # Stock daily loss limit — 2% of bot capital
    stock_limit = capital * 0.02
    if stats["daily_loss_stock"] >= stock_limit:
        return False, f"stock_daily_cap_${stock_limit:.0f}"

    # Green-day protect — if up on day, check 30% giveback rule
    peak = stats["daily_peak"]
    if peak > 0:
        profit = stats["daily_profit"]
        if profit < peak * 0.70:
            return False, "green_day_70pct_trail"

    return True, "ok"


# ── Green-day scale-to-fit ────────────────────────────────────────────────────

def green_day_scale(position_size: float, stats: dict) -> float:
    """
    Shrink position size once up on the day so max loss ≤ 30% of peak.
    """
    peak = stats["daily_peak"]
    if peak <= 0:
        return position_size
    # Max we can lose = 30% of peak
    max_loss    = peak * 0.30
    stop_pct    = TRAILING_STOP_PCT
    # position × stop_pct ≤ max_loss → position ≤ max_loss / stop_pct
    max_size    = max_loss / stop_pct
    return min(position_size, max_size)


# ── Order helpers ─────────────────────────────────────────────────────────────

def place_equity_order(encrypted: str, symbol: str, quantity: int, instruction: str):
    order = {
        "orderType": "MARKET", "session": "NORMAL", "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{"instruction": instruction, "quantity": quantity,
                                "instrument": {"symbol": symbol, "assetType": "EQUITY"}}]
    }
    resp = requests.post(f"{BASE_URL}/accounts/{encrypted}/orders",
                         headers={**headers(), "Content-Type": "application/json"}, json=order)
    resp.raise_for_status()
    return resp


def check_order_filled(encrypted: str, location: str) -> dict:
    time.sleep(1.5)
    try:
        resp = requests.get(location, headers=headers(), timeout=10)
        resp.raise_for_status()
        d = resp.json()
        return {"status": d.get("status", "UNKNOWN"),
                "description": d.get("statusDescription", ""),
                "filled": d.get("status") == "FILLED"}
    except Exception as ex:
        return {"status": "UNKNOWN", "description": str(ex), "filled": False}


# ── Delta trail (live bid/ask) ────────────────────────────────────────────────

def check_delta_trail(symbol: str, entry_px: float, high_px: float,
                      current_px: float, atr_pct: float) -> dict:
    """
    Once position up 5% — switch to live bid trail.
    Trail = best bid - 1 ATR.
    Books profits near peak before giveback.
    """
    profit_pct = (current_px - entry_px) / entry_px
    if profit_pct < 0.05:
        return {"active": False, "stop_price": None}

    bid = get_live_bid(symbol)
    if bid is None:
        bid = current_px

    atr_dollar  = current_px * (atr_pct / 100) if atr_pct else current_px * 0.02
    trail_price = bid - atr_dollar

    return {
        "active":      True,
        "stop_price":  trail_price,
        "bid":         bid,
        "atr_dollar":  round(atr_dollar, 2),
        "profit_pct":  round(profit_pct * 100, 1),
    }


# ── Spike protection ─────────────────────────────────────────────────────────

def detect_spike(candles: list, spike_mult: float = 2.5) -> dict:
    """
    Detects abnormal price bars using ATR(14) x SpikeMult.
    Dynamic — adjusts to current market pace automatically.
    Quiet day: small bar triggers it. Busy day: takes a big bar.

    Returns:
        is_spike: bool
        direction: "up" | "down" | None
        bar_range: current bar range
        atr: current ATR(14)
        threshold: bar_range needed to be a spike
    """
    if len(candles) < 15:
        return {"is_spike": False, "direction": None}

    try:
        # ATR(14) — true range average
        true_ranges = []
        for i in range(1, min(15, len(candles))):
            c     = candles[-i]
            prev  = candles[-i-1]
            tr    = max(
                c["high"] - c["low"],
                abs(c["high"] - prev["close"]),
                abs(c["low"]  - prev["close"])
            )
            true_ranges.append(tr)
        atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0

        if atr == 0:
            return {"is_spike": False, "direction": None}

        # Current bar range
        c         = candles[-1]
        bar_range = c["high"] - c["low"]
        threshold = atr * spike_mult
        is_spike  = bar_range > threshold

        # Spike direction
        direction = None
        if is_spike:
            if c["close"] > c["open"]:
                direction = "up"
            else:
                direction = "down"

        return {
            "is_spike":  is_spike,
            "direction": direction,
            "bar_range": round(bar_range, 4),
            "atr":       round(atr, 4),
            "threshold": round(threshold, 4),
        }
    except Exception:
        return {"is_spike": False, "direction": None}


def check_wick_exit(symbol: str, pos_price: float, buy_price: float,
                    candles: list, bucket: str = "swing") -> dict:
    """
    Wick exit — pressure flip signal.
    Day:   fires in profit OR loss (cheap insurance, fast exits)
    Swing: only fires when in profit (let stop handle losses)

    Armed after entry — checks for upper wick rejection (sellers taking control).
    """
    if len(candles) < 3:
        return {"action": None}
    try:
        c   = candles[-1]
        rng = c["high"] - c["low"]
        if rng == 0:
            return {"action": None}

        upper_wick = c["high"] - max(c["open"], c["close"])
        in_profit  = pos_price > buy_price

        # Upper wick > 45% of range = sellers rejecting highs = pressure flip
        wick_pct = upper_wick / rng
        if wick_pct < 0.45:
            return {"action": None}

        # Day: fires in profit OR loss
        if bucket == "day":
            return {
                "action": "wick_exit",
                "reason": "WICK EXIT (day)",
                "wick_pct": round(wick_pct * 100, 1),
                "in_profit": in_profit
            }

        # Swing: only fires when in profit
        if bucket == "swing" and in_profit:
            return {
                "action": "wick_exit",
                "reason": "WICK EXIT (swing)",
                "wick_pct": round(wick_pct * 100, 1),
                "in_profit": in_profit
            }

        return {"action": None}
    except Exception:
        return {"action": None}


def check_spike_on_position(symbol: str, pos_price: float, buy_price: float,
                             candles: list) -> dict:
    """
    Check spike protection on an open position.
    1. PROFIT GRAB — spike in your favor while in profit → book immediately
    2. LOSS EXIT  — spike against you → emergency exit before full stop
    Returns action: "profit_grab" | "loss_exit" | None
    """
    spike = detect_spike(candles)
    if not spike["is_spike"]:
        return {"action": None}

    in_profit = pos_price > buy_price

    if spike["direction"] == "up" and in_profit:
        return {
            "action":  "profit_grab",
            "reason":  "SPIKE PROFIT GRAB",
            "bar_range": spike["bar_range"],
            "atr":     spike["atr"],
        }
    elif spike["direction"] == "down":
        return {
            "action":  "loss_exit",
            "reason":  "SPIKE LOSS EXIT",
            "bar_range": spike["bar_range"],
            "atr":     spike["atr"],
        }

    return {"action": None}


# ── Execute sell ──────────────────────────────────────────────────────────────

def execute_sell(encrypted: str, symbol: str, quantity: int, price: float,
                 cash: float, reason: str = "signal", star: int = 0) -> float:
    try:
        place_equity_order(encrypted, symbol, quantity, "SELL")
        proceeds = quantity * price
        split    = record_sell_and_split(symbol, quantity, price, proceeds,
                                         STOCK_SPLIT["etf"], STOCK_SPLIT["cash"], STOCK_SPLIT["bot"])
        cash    += proceeds
        profit   = split["profit"]

        record_trade_result(profit, "stock")
        update_win_rate(profit)

        # Record for tax tracking
        try:
            ledger_t = load_ledger()
            trade_t  = ledger_t.get("closed_trades", [])
            hold_days = 180  # default
            for ct in trade_t:
                if ct.get("symbol") == symbol:
                    try:
                        from datetime import datetime as _dt
                        b = _dt.strptime(ct.get("bought_at","2000-01-01")[:10], "%Y-%m-%d")
                        s = _dt.strptime(ct.get("closed_at","2000-01-01")[:10], "%Y-%m-%d")
                        hold_days = (s - b).days
                    except Exception:
                        pass
                    break
            record_taxable_event(symbol, profit, hold_days, "stock")
        except Exception:
            pass

        if profit > 0:
            msg  = f"[ OUT ] {symbol} +{profit:,.2f}\n"
            msg += "━━━━━━━━━━━━━━━━━━\n"
            msg += f"PRICE  {price:.2f}\n"
            msg += f"QTY    {quantity}\n"
            msg += f"ETF    +{split['etf_cut']:,.2f}\n"
            msg += f"CASH   +{split['cash_cut']:,.2f}\n"
            msg += f"BOT    +{split['bot_cut']:,.2f}"
            send_alert(msg)
        else:
            msg  = f"[ CUT ] {symbol} {profit:,.2f}\n"
            msg += "━━━━━━━━━━━━━━━━━━\n"
            msg += f"PRICE  {price:.2f}\n"
            msg += f"QTY    {quantity}\n"
            msg += f"EXIT   {reason.upper()}"
            send_alert(msg)
        print(f"  Sold {quantity} {symbol} @ ${price:.2f} | P&L ${profit:+,.2f} | {reason}")
    except Exception as ex:
        send_alert(f"Sell error {symbol}: {ex}")
    return cash


# ── Daily summary ─────────────────────────────────────────────────────────────

def send_premarket_summary():
    """9:00 AM ET — morning brief before market opens."""
    ledger  = load_ledger()
    capital = get_trading_capital()
    cash_b  = get_cash_bucket()
    etf_b   = get_etf_bucket()
    day_cap = capital * DAY_PCT
    swg_cap = capital * SWING_PCT

    msg  = "Good morning — Pre-Market Brief\n"
    msg += f"Capital: ${capital:,.0f} | Day: ${day_cap:,.0f} | Swing: ${swg_cap:,.0f}\n"
    msg += f"ETF bucket: ${etf_b:,.0f} | Cash ready: ${cash_b:,.0f}\n"
    msg += f"PDT trades used: {ledger.get('day_trades_this_week', 0)}/3 this week"
    send_alert(msg)


def send_session_summary():
    """4:05 PM ET — end of session summary after market close."""
    ledger = load_ledger()
    stats  = get_daily_stats()
    wr_history = ledger.get("win_rate_history", [])
    win_rate   = sum(wr_history) / len(wr_history) * 100 if wr_history else 0

    daily_history = ledger.get("daily_pnl_history", [])
    daily_history.append(stats["daily_profit"])
    ledger["daily_pnl_history"] = daily_history[-10:]
    consistency = sum(1 for d in daily_history if d > 0) / len(daily_history) * 100 if daily_history else 0

    c4 = ledger.get("conviction_4_count", 0)
    c3 = ledger.get("conviction_3_count", 0)
    c2 = ledger.get("conviction_2_count", 0)
    c1 = ledger.get("conviction_1_count", 0)

    for k in ["conviction_4_count", "conviction_3_count", "conviction_2_count", "conviction_1_count"]:
        ledger[k] = 0
    save_ledger(ledger)

    capital    = get_trading_capital()
    stock_cap  = capital * 0.02
    stock_used = stats["daily_loss_stock"] / stock_cap * 100 if stock_cap > 0 else 0

    trades_today = stats["trades_today"]
    daily_profit = stats["daily_profit"]
    daily_peak   = stats["daily_peak"]

    msg  = "[ CIRCUIT ] CLOSE 16:05\n"
    msg += "━━━━━━━━━━━━━━━━━━\n"
    msg += f"TRADES {trades_today}\n"
    msg += f"P&L    {daily_profit:+,.2f}\n"
    msg += f"PEAK   {daily_peak:,.2f}\n"
    msg += f"WIN    {win_rate:.0f}%\n"
    msg += f"CONS   {consistency:.0f}%\n"
    msg += f"CAP    {stock_used:.0f}% used\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"4/4 x{c4}  3/4 x{c3}  2/4 x{c2}"
    send_alert(msg)


def send_eod_summary():
    """5:00 PM ET — end of day full summary with tax YTD."""
    ledger  = load_ledger()
    capital = get_trading_capital()
    cash_b  = get_cash_bucket()
    etf_b   = get_etf_bucket()
    ytd_tax = ledger.get("ytd_tax_owed", 0.0)

    msg  = "[ CIRCUIT ] EOD 17:00\n"
    msg += "━━━━━━━━━━━━━━━━━━\n"
    msg += f"CAP    {capital:,.2f}\n"
    msg += f"DAY    {capital*DAY_PCT:,.2f}\n"
    msg += f"SWING  {capital*SWING_PCT:,.2f}\n"
    msg += f"ETF    {etf_b:,.2f}\n"
    msg += f"CASH   {cash_b:,.2f}\n"
    msg += f"TAX YTD {ytd_tax:,.2f}"
    send_alert(msg)


def send_daily_summary():
    """Legacy — calls session summary."""
    send_session_summary()


# ── Main strategy ─────────────────────────────────────────────────────────────

def run_strategy():
    if not is_market_open():
        return

    print("\n=== Strategy check ===")
    try:
        accounts      = get_account_numbers()
        encrypted     = accounts[0]["hashValue"]
        account       = get_account(encrypted)
        cash          = get_available_cash(account)
        account_value = get_portfolio_value(account)
        positions     = get_positions(account)

        sync_ledger_from_schwab(encrypted)
        check_token_health()

        # Sync Schwab tax history in January
        from datetime import datetime as _dt2
        if _dt2.now().month == 1:
            try:
                sync_schwab_tax_history(encrypted)
            except Exception:
                pass

        capital   = get_trading_capital()
        ceiling   = get_ceiling(capital)
        stats     = get_daily_stats()
        tier_name, tier_cfg = get_tier(capital)

        print(f"Account: ${account_value:,.2f} | Capital: ${capital:,.2f} | Ceiling: ${ceiling:,.0f} | {tier_name}")

        check_dividends(encrypted)

        # ── Sell signals + dynamic stops + delta trail ──
        sold_this_run = set()
        for pos in positions:
            sym = pos["instrument"]["symbol"]
            if sym not in BOT_STOCKS or sym in sold_this_run:
                continue

            qty    = int(pos.get("longQuantity", 0))
            price  = pos.get("marketValue", 0) / max(qty, 1)
            if qty < 1 or price <= 0:
                continue

            update_high_price(sym, price)
            trail_info = get_trailing_stop_info(sym)
            trigger    = None

            # Signal sell handled by dynamic stop and delta trail below
            # Check cooldown (30 min after loss or spike exit)
            ledger_cd = load_ledger()
            last_exit = ledger_cd.get("last_loss_exit_time", 0)
            if time.time() - last_exit < 1800:  # 30 min cooldown
                print(f"  {sym}: COOLDOWN active — {int((1800-(time.time()-last_exit))/60)}min remaining")
                continue

            if not trigger and trail_info:
                buy_px  = trail_info["buy_price"]
                high_px = trail_info["high_price"]

                # Delta trail check (live bid/ask)
                atr_pct = trail_info.get("atr_pct", 2.0) if trail_info else 2.0
                delta   = check_delta_trail(sym, buy_px, high_px, price, atr_pct)
                if delta["active"] and price <= delta["stop_price"]:
                    trigger = "delta_trail"
                    send_alert(f"🎯 Trail exit {sym} | Bid ${delta['bid']:.2f} | +{delta['profit_pct']}%")
                else:
                    # Dynamic stop fallback
                    # Pull recent candles for spike + candle-strength stop
                    try:
                        from scanner import get_price_history as _gph
                        _candles = _gph(sym)
                    except Exception:
                        _candles = None

                    # Get bucket type for this position
                    _bucket = trail_info.get("bucket", "swing") if trail_info else "swing"

                    # Wick exit check
                    if _candles:
                        wick_check = check_wick_exit(sym, price, buy_px, _candles, _bucket)
                        if wick_check["action"] == "wick_exit":
                            trigger = "wick_exit"
                            _side = "PROFIT" if wick_check["in_profit"] else "LOSS"
                            msg  = f"[ WICK ] {sym} — {_bucket.upper()}\n"
                            msg += f"WICK {wick_check['wick_pct']}% — {_side}"
                            send_alert(msg)
                            # Record cooldown
                            _l = load_ledger()
                            _l["last_loss_exit_time"] = time.time()
                            save_ledger(_l)

                    # Spike protection check
                    if not trigger and _candles:
                        spike_check = check_spike_on_position(sym, price, buy_px, _candles)
                        if spike_check["action"] == "profit_grab":
                            trigger = "profit_grab"
                            msg  = "[ GRAB ] " + sym + "\n"
                            msg += "BAR " + str(round(spike_check['bar_range'],2)) + " ATR " + str(round(spike_check['atr'],2))
                            send_alert(msg)
                            _l = load_ledger()
                            _l["last_loss_exit_time"] = time.time()
                            save_ledger(_l)
                        elif spike_check["action"] == "loss_exit":
                            trigger = "loss_exit"
                            msg  = "[ SPIKE CUT ] " + sym + "\n"
                            msg += "BAR " + str(round(spike_check['bar_range'],2)) + " ATR " + str(round(spike_check['atr'],2))
                            send_alert(msg)
                            _l = load_ledger()
                            _l["last_loss_exit_time"] = time.time()
                            save_ledger(_l)

                    # Scale-out TP check (1:3 R/R)
                    if not trigger and trail_info:
                        tp1_pct = trail_info.get("tp1_pct", 0.105)
                        tp2_pct = trail_info.get("tp2_pct", 0.175)
                        tp1_hit = trail_info.get("tp1_hit", False)
                        tp2_hit = trail_info.get("tp2_hit", False)
                        profit_pct = (price - buy_px) / buy_px if buy_px > 0 else 0

                        if not tp1_hit and profit_pct >= tp1_pct:
                            # Sell ⅓ at TP1
                            tp1_qty = max(1, qty // 3)
                            try:
                                place_equity_order(encrypted, sym, tp1_qty, "SELL")
                                ledger_tp = load_ledger()
                                if sym in ledger_tp["open_trades"]:
                                    ledger_tp["open_trades"][sym]["tp1_hit"]  = True
                                    ledger_tp["open_trades"][sym]["quantity"] -= tp1_qty
                                save_ledger(ledger_tp)
                                msg  = f"[ TP1 ] {sym} 1/3\n"
                                msg += "━━━━━━━━━━━━━━━━━━\n"
                                msg += f"PRICE  {price:.2f}\n"
                                msg += f"+{profit_pct*100:.1f}%"
                                send_alert(msg)
                            except Exception as _e:
                                print(f"  TP1 error {sym}: {_e}")

                        elif tp1_hit and not tp2_hit and profit_pct >= tp2_pct:
                            # Sell ⅓ at TP2
                            tp2_qty = max(1, qty // 3)
                            try:
                                place_equity_order(encrypted, sym, tp2_qty, "SELL")
                                ledger_tp = load_ledger()
                                if sym in ledger_tp["open_trades"]:
                                    ledger_tp["open_trades"][sym]["tp2_hit"]  = True
                                    ledger_tp["open_trades"][sym]["quantity"] -= tp2_qty
                                save_ledger(ledger_tp)
                                msg  = f"[ TP2 ] {sym} 2/3\n"
                                msg += "━━━━━━━━━━━━━━━━━━\n"
                                msg += f"PRICE  {price:.2f}\n"
                                msg += f"+{profit_pct*100:.1f}%"
                                send_alert(msg)
                            except Exception as _e:
                                print(f"  TP2 error {sym}: {_e}")

                    if not trigger:
                        # Use pressure trail for exits — more aggressive
                        stop_info = get_pressure_trail(sym, buy_px, high_px, price, _candles, TRAILING_STOP_PCT)
                    else:
                        stop_info = None
                    if price <= stop_info["stop_price"]:
                        trigger = stop_info["reason"]
                        if stop_info["reason"] == "breakeven":
                            send_alert(f"[ LOCK ] {sym} — BREAKEVEN")

            if trigger:
                if check_covered_call_already_open(encrypted, sym):
                    print(f"  {sym}: skip sell — covered call open")
                    continue
                sold_this_run.add(sym)
                cash = execute_sell(encrypted, sym, qty, price, cash, trigger)

        # ── Buy signals ──
        if cash >= 10:
            top_stocks = scan_best_stocks(cash, bot_capital=capital)
            bought_this_run = set()

            for stock in top_stocks:
                symbol = stock["symbol"]
                price  = stock["price"]
                if symbol in bought_this_run or get_position_for(positions, symbol):
                    continue
                if price > cash:
                    continue

                ok, reason = can_trade(capital, stats, 5)
                if not ok:
                    print(f"  {symbol}: skip — {reason}")
                    continue

                # Volatility guard — skip entry if current bar is a spike
                try:
                    from scanner import get_price_history as _gph2
                    _entry_candles = _gph2(symbol)
                    _spike = detect_spike(_entry_candles)
                    if _spike["is_spike"]:
                        print(f"  {symbol}: skip — VOLATILITY GUARD spike detected")
                        continue
                except Exception:
                    pass

                # MTF conviction sizing
                position_size = get_mtf_position_size(symbol, ceiling)
                if position_size == 0:
                    print(f"  {symbol}: skip — MTF conviction too low")
                    continue
                position_size = green_day_scale(position_size, stats)
                position_size = min(position_size, cash)

                quantity = int(position_size // price)
                if quantity < 1:
                    continue

                try:
                    resp     = place_equity_order(encrypted, symbol, quantity, "BUY")
                    location = resp.headers.get("Location", "")
                    cost     = quantity * price

                    if location:
                        chk = check_order_filled(encrypted, location)
                        if not chk["filled"] and chk["status"] in ("REJECTED", "CANCELED"):
                            send_alert(f"❌ {symbol} canceled — {chk['description'][:50]}")
                            bought_this_run.add(symbol)
                            continue

                    cash -= cost
                    bought_this_run.add(symbol)
                    record_buy(symbol, quantity, price, cost, bucket=bucket)
                    from scanner import get_mtf_conviction
                    conv  = get_mtf_conviction(symbol)
                    stars = get_star_rating(stock)
                    record_conviction_count(conv)
                    send_alert(f"📈 Bought {symbol} x{quantity} @ ${price:.2f} | {conv}/4 | ⭐{stars} | ${cost:,.0f}")
                    print(f"  Bought {quantity} {symbol} @ ${price:.2f} | star={star}")
                except Exception as ex:
                    send_alert(f"Buy error {symbol}: {ex}")

        # ── Options ──
        run_options(encrypted, positions, cash, stats, capital)

        # ── ETF sweep ──
        run_etf_sweep(encrypted)

        # ── Cash ready reminder — once per day ──
        cash_bucket = get_cash_bucket()
        if cash_bucket > 20:
            ledger2 = load_ledger()
            last_time = ledger2.get("last_cash_reminder_time", 0)
            last_amt  = ledger2.get("last_cash_reminder_amount", 0)
            if abs(cash_bucket - last_amt) > 5 or time.time() - last_time > 86400:
                send_alert(f"💵 ${cash_bucket:,.0f} profit cash ready to withdraw")
                ledger2["last_cash_reminder_time"]   = time.time()
                ledger2["last_cash_reminder_amount"] = cash_bucket
                save_ledger(ledger2)

    except Exception as ex:
        msg = f"Bot error: {ex}"
        print(msg)
        send_alert(msg)


# ── Options ───────────────────────────────────────────────────────────────────

def run_options(encrypted: str, positions: list, cash: float, stats: dict, capital: float):
    # Options daily cap check — 1% of bot capital
    options_limit = capital * 0.01
    if stats["daily_loss_options"] >= options_limit:
        print(f"  Options daily cap hit (${options_limit:.0f}) — skipping")
        return

    # Covered calls
    print("\n-- Covered calls --")
    for pos in positions:
        sym    = pos["instrument"]["symbol"]
        shares = int(pos.get("longQuantity", 0))
        if pos["instrument"].get("assetType") != "EQUITY" or shares < 100:
            continue
        if check_covered_call_already_open(encrypted, sym):
            print(f"  {sym}: call already open")
            continue
        call = find_best_covered_call(sym, shares)
        if not call:
            continue
        try:
            resp = place_covered_call(encrypted, call["option_symbol"],
                                      call["contracts"], call["premium"])
            loc  = resp.headers.get("Location", "")
            if loc:
                chk = check_order_filled(encrypted, loc)
                if not chk["filled"] and chk["status"] in ("REJECTED", "CANCELED"):
                    send_alert(f"❌ {sym} call canceled — {chk['description'][:50]}")
                    continue
            total = call["total_premium"]
            _record_options_income(total)
            send_alert(f"📝 {sym} call ${call['strike']:.2f} {call['expiry']} | +${total:,.2f}")
        except Exception as ex:
            print(f"  Call error {sym}: {ex}")

    # Cash secured puts — use swing direction (down bias = better put opportunities)
    if cash < 200:
        return
    print(f"\n-- Cash secured puts | ${cash:,.2f} --")
    candidates = scan_best_stocks(cash, bot_capital=capital)
    for stock in candidates:
        sym   = stock["symbol"]
        price = stock["price"]
        star  = get_star_rating(stock)

        # Check swing direction for puts
        try:
            swing_info = get_swing_conviction(sym)
            swing_dir  = swing_info.get("direction", "flat")
            swing_conv = swing_info.get("conviction", 0)
            # Puts work in any direction but prefer flat/down bias
            # Skip puts when strong uptrend (better to buy stock instead)
            if swing_dir == "up" and swing_conv >= 4:
                continue  # strong uptrend — buy stock not put
        except Exception:
            swing_dir = "flat"

        if star < 7:  # 7+ stars required for puts
            continue
        if check_put_already_open(encrypted, sym):
            continue
        put = find_best_cash_secured_put(sym, price, cash)
        if not put:
            continue
        try:
            resp = place_cash_secured_put(encrypted, put["option_symbol"], put["premium"])
            loc  = resp.headers.get("Location", "")
            if loc:
                chk = check_order_filled(encrypted, loc)
                if not chk["filled"] and chk["status"] in ("REJECTED", "CANCELED"):
                    send_alert(f"❌ {sym} put canceled — {chk['description'][:50]}")
                    continue
            total = put["total_premium"]
            _record_options_income(total)
            send_alert(f"📝 {sym} put ${put['strike']:.2f} {put['expiry']} | +${total:,.2f}")
        except Exception as ex:
            print(f"  Put error {sym}: {ex}")


def _record_options_income(total: float):
    """Options premium split: 60% ETF, 40% cash."""
    ledger = load_ledger()
    etf_cut  = total * OPTIONS_SPLIT["etf"]
    cash_cut = total * OPTIONS_SPLIT["cash"]
    ledger["profit_bucket"]  = ledger.get("profit_bucket", 0.0) + total
    ledger["etf_bucket"]     = ledger.get("etf_bucket", 0.0) + etf_cut
    ledger["cash_bucket"]    = ledger.get("cash_bucket", 0.0) + cash_cut
    save_ledger(ledger)


# ── ETF sweep ─────────────────────────────────────────────────────────────────

def run_etf_sweep(encrypted: str):
    etf_bucket = get_etf_bucket()
    probe      = scan_best_etfs(etf_bucket, top_n=1)
    threshold  = probe[0]["price"] if probe else ETF_MIN_SWEEP
    print(f"\n-- ETF bucket: ${etf_bucket:,.2f} | Threshold: ${threshold:,.2f} --")
    if etf_bucket < threshold:
        return
    best = scan_best_etfs(etf_bucket, top_n=2)
    if not best:
        return
    per_etf = etf_bucket / len(best)
    for etf in best:
        qty = int(per_etf // etf["price"])
        if qty < 1:
            continue
        try:
            place_equity_order(encrypted, etf["symbol"], qty, "BUY")
            cost = qty * etf["price"]
            deduct_etf_bucket(cost)
            send_alert(f"📊 Bought {qty} {etf['symbol']} @ ${etf['price']:.2f} from profits")
        except Exception as ex:
            print(f"  ETF sweep error {etf['symbol']}: {ex}")


# ── Dividends ─────────────────────────────────────────────────────────────────

def check_dividends(encrypted: str):
    dividends = get_recent_dividends(encrypted, days_back=2)
    seen_ids  = get_dividend_stats()["seen_dividend_ids"]

    capital    = get_trading_capital()
    etf_capital = load_ledger().get("etf_portfolio_value", 8000)
    etf_level_name, etf_level = get_etf_level(etf_capital)
    split = ETF_SPLITS.get(etf_level_name, ETF_SPLITS["Level 1"])

    for div in dividends:
        txn_id = div["transaction_id"]
        if txn_id in seen_ids:
            continue
        # ETF dividend routing per figure-8 level
        bot_amt = div["amount"] * split["bot"]
        cash_amt = div["amount"] * split["cash"]
        etf_amt = div["amount"] * split["etf"]

        ledger = load_ledger()
        ledger["trading_capital"] = ledger.get("trading_capital", 0.0) + bot_amt
        ledger["cash_bucket"]     = ledger.get("cash_bucket", 0.0) + cash_amt
        ledger["etf_bucket"]      = ledger.get("etf_bucket", 0.0) + etf_amt
        save_ledger(ledger)

        record_dividend(div["symbol"], div["amount"], div["reinvested"])
        mark_dividend_seen(txn_id)

        cat  = get_etf_category(div["symbol"])
        rule = ETF_DIVIDEND_RULES.get(cat, {"reinvest": 0.5, "cash": 0.5})
        r_amt = div["amount"] * rule["reinvest"]
        c_amt = div["amount"] * rule["cash"]

        if r_amt > 0 and c_amt > 0:
            send_alert(f"💵 {div['symbol']} div ${div['amount']:,.2f} → ${r_amt:,.2f} reinvested | ${c_amt:,.2f} cash")
        elif r_amt > 0:
            send_alert(f"🔄 {div['symbol']} div ${div['amount']:,.2f} reinvested")
        else:
            send_alert(f"💵 {div['symbol']} div ${div['amount']:,.2f} to cash")


# ── 24/7 balance monitor ──────────────────────────────────────────────────────

def get_schwab_transactions(encrypted: str, days_back: int = 1) -> list:
    from datetime import timedelta
    end   = datetime.utcnow()
    start = end - timedelta(days=days_back)
    try:
        resp = requests.get(
            f"{BASE_URL}/accounts/{encrypted}/transactions", headers=headers(),
            params={"startDate": start.strftime("%Y-%m-%dT00:00:00.000Z"),
                    "endDate":   end.strftime("%Y-%m-%dT23:59:59.000Z"),
                    "types":     "CASH_IN_OR_CASH_OUT,CHECKING,JOURNAL"},
            timeout=15
        )
        return resp.json() if resp.ok and isinstance(resp.json(), list) else []
    except Exception:
        return []


def check_balance_24_7():
    try:
        accounts  = get_account_numbers()
        encrypted = accounts[0]["hashValue"]
        account   = get_account(encrypted)
        cash      = get_cash_balance(account)
        on_hold   = get_cash_on_hold(account)

        ledger   = load_ledger()
        seen_ids = set(ledger.get("seen_cash_txn_ids", []))
        txns     = get_schwab_transactions(encrypted, days_back=1)

        for txn in txns:
            txn_id = str(txn.get("activityId", txn.get("transactionId", "")))
            if txn_id in seen_ids:
                continue
            amount = txn.get("netAmount", 0.0)
            seen_ids.add(txn_id)
            if amount > 1:
                ledger["deposits"]        = ledger.get("deposits", 0.0) + amount
                ledger["trading_capital"] = ledger.get("trading_capital", 0.0) + amount
                send_alert(f"💵 +${amount:,.2f} deposited")
            elif amount < -1:
                w = abs(amount)
                ledger.setdefault("withdrawal_history", []).append(
                    {"amount": w, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                )
                ledger["total_withdrawn"] = ledger.get("total_withdrawn", 0.0) + w
                send_alert(f"🏦 -${w:,.2f} withdrawn")

        ledger["seen_cash_txn_ids"] = list(seen_ids)[-200:]
        ledger["last_known_cash"]   = cash

        # Cash on hold changes
        last_hold = ledger.get("last_cash_on_hold", on_hold)
        if abs(on_hold - last_hold) > 1:
            if on_hold > last_hold:
                send_alert(f"🔒 +${on_hold - last_hold:,.0f} on hold")
            else:
                send_alert(f"🔓 ${last_hold - on_hold:,.0f} released")
        ledger["last_cash_on_hold"] = on_hold

        # Options P&L daily change
        positions = account["securitiesAccount"].get("positions", [])
        opts_pl   = sum(p.get("currentDayProfitLoss", 0) for p in positions
                        if p.get("instrument", {}).get("assetType") == "OPTION")
        last_pl   = ledger.get("last_options_pl", opts_pl)
        last_time = ledger.get("last_options_notify_time", 0)
        if abs(opts_pl - last_pl) > 5 and time.time() - last_time > 86400:
            send_alert(f"{'📈' if opts_pl > last_pl else '📉'} Options ${opts_pl:,.0f}")
            ledger["last_options_notify_time"] = time.time()
        ledger["last_options_pl"] = opts_pl

        save_ledger(ledger)
        print(f"Balance check — cash: ${cash:.2f} | hold: ${on_hold:.2f}")
    except Exception as ex:
        print(f"Balance check error: {ex}")


# ── Main ──────────────────────────────────────────────────────────────────────

_last_run = 0


def run_strategy_safe():
    global _last_run
    if time.time() - _last_run < 60:
        return
    _last_run = time.time()
    run_strategy()


def main():
    check_balance_24_7()

    try:
        accounts      = get_account_numbers()
        encrypted     = accounts[0]["hashValue"]
        account       = get_account(encrypted)
        cash          = get_cash_balance(account)
        account_value = get_portfolio_value(account)

        sync_ledger_from_schwab(encrypted)

        capital    = get_trading_capital()
        ceiling    = get_ceiling(capital)
        cash_ready = get_cash_bucket()
        on_hold    = get_cash_on_hold(account)
        tier_name, _ = get_tier(capital)

        ledger  = load_ledger()
        now_ts  = time.time()
        p24h    = sum(t.get("profit", 0) for t in ledger.get("closed_trades", [])
                      if now_ts - time.mktime(time.strptime(
                          t.get("sold_at", t.get("closed_at", "2000-01-01T00:00:00Z")),
                          "%Y-%m-%dT%H:%M:%SZ")) < 86400)

        pulse    = get_market_pulse() if is_market_open() else ""
        hold_str = f"\nHOLD   {on_hold:,.0f}" if on_hold > 0 else ""
        msg  = "[ CIRCUIT ] LIVE\n"
        msg += "━━━━━━━━━━━━━━━━━━\n"
        msg += f"CAP    {capital:,.2f}\n"
        msg += f"DAY    {capital*DAY_PCT:,.2f}\n"
        msg += f"SWING  {capital*SWING_PCT:,.2f}\n"
        msg += f"CASH   {cash_ready:,.2f}"
        msg += hold_str
        if pulse:
            msg += f"\n━━━━━━━━━━━━━━━━━━\n{pulse}"
        send_alert(msg)

    except Exception as ex:
        print(f"Startup error: {ex}")

    if is_market_open():
        run_strategy_safe()

    schedule.every(CHECK_INTERVAL).minutes.do(run_strategy_safe)
    schedule.every(5).minutes.do(check_balance_24_7)
    # Pre-market brief at 9:00 AM ET
    schedule.every().day.at("07:30").do(send_premarket_summary)

    # Session summary at 4:05 PM ET (after market close)
    schedule.every().day.at("16:05").do(send_session_summary)

    # End of day full summary at 5:00 PM ET
    schedule.every().day.at("17:00").do(send_eod_summary)

    # Backtest after session at 4:30 PM ET on weekdays
    def run_post_session_backtest():
        et  = pytz.timezone("America/New_York")
        now = datetime.now(et)
        if now.weekday() < 5:  # Mon-Fri only
            try:
                from backtest import run_backtest
                run_backtest(days=14, bot_capital=get_trading_capital())
            except Exception as ex:
                print(f"Backtest error: {ex}")

    schedule.every().day.at("16:30").do(run_post_session_backtest)

    # April tax alert daily at 9:05 AM
    def maybe_send_tax_alert():
        from datetime import datetime as _dt
        import pytz as _pytz
        now = _dt.now(_pytz.timezone("America/New_York"))
        if now.month == 4 and 1 <= now.day <= 15:
            try:
                accts     = get_account_numbers()
                enc       = accts[0]["hashValue"]
                send_tax_alert(enc)
            except Exception as ex:
                print(f"Tax alert error: {ex}")

    schedule.every().day.at("09:05").do(maybe_send_tax_alert)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
