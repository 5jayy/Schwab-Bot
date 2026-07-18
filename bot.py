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
    get_tier, get_etf_level, ETF_DIVIDEND_RULES, get_etf_category
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
    BOT_STOCKS, ETF_MIN_SWEEP
)

load_dotenv()

BASE_URL          = "https://api.schwabapi.com/trader/v1"
MARKET_URL        = "https://api.schwabapi.com/marketdata/v1"
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", 0.07))
CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL_MINUTES", 30))

# ── Profit splits ─────────────────────────────────────────────────────────────
STOCK_SPLIT  = {"etf": 0.60, "cash": 0.30, "bot": 0.10}
OPTIONS_SPLIT     = {"etf": 0.60, "cash": 0.40, "bot": 0.00}
ETF_OPTIONS_SPLIT = {"etf": 0.20, "cash": 0.50, "bot": 0.30}
ETF_SPLITS   = {
    "Level 1": {"bot": 0.60, "cash": 0.30, "etf": 0.10},
    "Level 2": {"bot": 0.50, "cash": 0.30, "etf": 0.20},
    "Level 3": {"bot": 0.30, "cash": 0.40, "etf": 0.30},
}

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
    rsi = stock.get("rsi", 50)
    if 50 <= rsi <= 65: stars += 1
    vol = stock.get("volume", 0)
    if vol > 0: stars += 1
    return min(max(stars, 1), 10)


def get_mtf_position_size(symbol: str, ceiling: float) -> float:
    """
    4-frame MA conviction sizing.
    4/4 → full ceiling
    3/4 → 50% ceiling
    2/4 → 35% ceiling (~$70 at $200 ceiling)
    1/4 or less → no trade (returns 0)
    """
    from scanner import get_mtf_conviction
    conviction = get_mtf_conviction(symbol)
    if conviction >= 4:
        return ceiling          # 4/4 full
    elif conviction == 3:
        return ceiling * 0.50   # 3/4 half
    elif conviction == 2:
        return ceiling * 0.35   # 2/4 small but protected by features
    else:
        return 0                # 1/4 or less — no trade


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

        if profit > 0:
            send_alert(
                f"💰 Sold {symbol} x{quantity} @ ${price:.2f} | +${profit:,.2f}\n"
                f"→ ETF ${split['etf_cut']:,.0f} | Cash ${split['cash_cut']:,.0f} | Bot ${split['bot_cut']:,.0f}"
            )
        else:
            tag = "🛑" if "stop" in reason or "trail" in reason or "breakeven" in reason else "📉"
            send_alert(f"{tag} Sold {symbol} x{quantity} @ ${price:.2f} | ${profit:,.2f}")
        print(f"  Sold {quantity} {symbol} @ ${price:.2f} | P&L ${profit:+,.2f} | {reason}")
    except Exception as ex:
        send_alert(f"Sell error {symbol}: {ex}")
    return cash


# ── Daily summary ─────────────────────────────────────────────────────────────

def send_daily_summary():
    """Send 4 PM daily summary with consistency tracking."""
    ledger = load_ledger()
    stats  = get_daily_stats()
    wr_history = ledger.get("win_rate_history", [])
    win_rate   = sum(wr_history) / len(wr_history) * 100 if wr_history else 0

    # Consistency % — how many of last 10 days were profitable
    daily_history = ledger.get("daily_pnl_history", [])
    daily_history.append(stats["daily_profit"])
    ledger["daily_pnl_history"] = daily_history[-10:]
    consistency = sum(1 for d in daily_history if d > 0) / len(daily_history) * 100 if daily_history else 0
    save_ledger(ledger)

    capital    = get_trading_capital()
    stock_cap  = capital * 0.02
    stock_used = stats["daily_loss_stock"] / stock_cap * 100 if stock_cap > 0 else 0

    # Conviction breakdown
    c4 = ledger.get("conviction_4_count", 0)
    c3 = ledger.get("conviction_3_count", 0)
    c2 = ledger.get("conviction_2_count", 0)
    c1 = ledger.get("conviction_1_count", 0)

    # Reset conviction counts for tomorrow
    for k in ["conviction_4_count", "conviction_3_count", "conviction_2_count", "conviction_1_count"]:
        ledger[k] = 0
    save_ledger(ledger)

    trades_today = stats['trades_today']
    daily_profit = stats['daily_profit']
    daily_peak   = stats['daily_peak']
    msg = "Daily Summary\n"
    msg += f"Trades: {trades_today} | P&L: ${daily_profit:+,.0f} | Peak: ${daily_peak:,.0f}\n"
    msg += f"Win rate: {win_rate:.0f}% | Consistency: {consistency:.0f}%\n"
    msg += f"Stock cap used: {stock_used:.0f}%\n"
    msg += f"Signals: 4/4={c4} | 3/4={c3} | 2/4={c2} | 1/4={c1}"
    send_alert("📊 " + msg)


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
                    # Pull recent candles for candle-strength based stop
                    try:
                        from scanner import get_price_history as _gph
                        _candles = _gph(sym)
                    except Exception:
                        _candles = None
                    stop_info = get_dynamic_stop(buy_px, high_px, price, TRAILING_STOP_PCT, _candles)
                    if price <= stop_info["stop_price"]:
                        trigger = stop_info["reason"]
                        if stop_info["reason"] == "breakeven":
                            send_alert(f"🛡️ Breakeven exit {sym} | Protected")

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
                    record_buy(symbol, quantity, price, cost)
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

def run_etf_redirect(encrypted: str, cash: float, capital: float):
    """
    When roadmap says ETF options beats swing trading,
    use a portion of swing capital to buy ETF shares directly.
    Accelerates roadmap compounding — self-chasing system.

    Only fires when:
    - Roadmap priority ETF identified
    - Swing capital has room
    - ETF not already at 100 shares
    - Market is open
    """
    try:
        from roadmap import calculate_roadmap, get_priority_etf
        ledger    = load_ledger()
        routing   = ledger.get("roadmap_swing_vs_etf", "swing")
        priority  = ledger.get("roadmap_priority_etf", "SCHB")
        sweep_spl = ledger.get("roadmap_sweep_split", {priority: 0.70})

        if routing != "etf_options":
            return  # swing trading wins — don't redirect

        if not priority:
            return

        # Get current shares of priority ETF
        accts     = get_account_numbers()
        encrypted_acc = accts[0]["hashValue"]
        resp = requests.get(
            f"{BASE_URL}/accounts/{encrypted_acc}?fields=positions",
            headers={"Authorization": f"Bearer {get_valid_token()}",
                     "Content-Type": "application/json"},
            timeout=15
        )
        positions = resp.json()["securitiesAccount"].get("positions", [])
        current_shares = 0
        for p in positions:
            if p["instrument"]["symbol"] == priority:
                current_shares = p.get("longQuantity", 0)
                break

        if current_shares >= 100:
            print(f"  {priority}: already at 100 shares — roadmap redirecting to next ETF")
            ledger["roadmap_priority_etf"] = None  # trigger recalculate
            save_ledger(ledger)
            return

        # Use 15% of swing capital for ETF redirect
        redirect_amount = capital * SWING_PCT * 0.15
        redirect_amount = min(redirect_amount, cash * 0.20)  # max 20% of available cash

        if redirect_amount < 50:
            return  # not enough to matter

        # Get ETF price
        quote_resp = requests.get(
            f"https://api.schwabapi.com/marketdata/v1/quotes/{priority}",
            headers={"Authorization": f"Bearer {get_valid_token()}"},
            timeout=10
        )
        if not quote_resp.ok:
            return

        price = quote_resp.json().get(priority, {}).get("quote", {}).get("lastPrice", 0)
        if price <= 0:
            return

        shares_to_buy = int(redirect_amount // price)
        if shares_to_buy < 1:
            return

        # Check if buying would exceed 100 shares
        shares_to_buy = min(shares_to_buy, int(100 - current_shares))
        if shares_to_buy < 1:
            return

        cost = shares_to_buy * price

        # Place order
        resp = place_equity_order(encrypted, priority, shares_to_buy, "BUY")
        if resp and resp.headers.get("Location"):
            print(f"  ETF redirect: bought {shares_to_buy} {priority} @ ${price:.2f} = ${cost:.2f}")
            new_shares = int(current_shares + shares_to_buy)
            days_left  = int((100 - new_shares) * price / max(capital * 0.60 * 0.70 / 30, 1))
            msg  = "[ CIRCUIT ] ETF REDIRECT\n"
            msg += priority + " x" + str(shares_to_buy) + " @ $" + f"{price:.2f}" + "\n"
            msg += "ROADMAP: " + str(new_shares) + "/100 shares\n"
            msg += "NEXT: covered call in ~" + str(days_left) + "d"
            send_alert(msg)

    except Exception as ex:
        print(f"ETF redirect error: {ex}")


def run_etf_options(encrypted: str, positions: list, cash: float):
    """Scan ETF options. Profits: 20% ETF / 50% cash / 30% bot."""
    try:
        from options import scan_etf_options, place_covered_call, place_cash_secured_put
        from options import check_covered_call_already_open, check_put_already_open
    except ImportError:
        return

    opps = scan_etf_options(positions, cash)
    if not opps:
        return

    print(f"-- ETF options | {len(opps)} opps --")

    for opp in opps[:2]:
        sym   = opp["symbol"]
        typ   = opp["type"]
        prem  = opp["premium"]
        total = opp["total_premium"]
        try:
            if typ == "etf_covered_call":
                if check_covered_call_already_open(encrypted, sym):
                    continue
                place_covered_call(encrypted, opp["option_symbol"], opp["contracts"], prem)
                send_alert("[ IN ] " + sym + " ETF CALL\nSTRIKE " + str(opp["strike"]) + "\nPREM   " + f"{prem:.2f}" + "\nTOTAL  " + f"{total:.2f}" + "\nSPLIT  ETF20 CASH50 BOT30")
            elif typ == "etf_put":
                if check_put_already_open(encrypted, sym):
                    continue
                place_cash_secured_put(encrypted, opp["option_symbol"], prem)
                send_alert("[ IN ] " + sym + " ETF PUT\nSTRIKE " + str(opp["strike"]) + "\nPREM   " + f"{prem:.2f}" + "\nTOTAL  " + f"{total:.2f}" + "\nSPLIT  ETF20 CASH50 BOT30")

            ledger = load_ledger()
            ledger["etf_bucket"]  = ledger.get("etf_bucket", 0)  + total * ETF_OPTIONS_SPLIT["etf"]
            ledger["cash_bucket"] = ledger.get("cash_bucket", 0) + total * ETF_OPTIONS_SPLIT["cash"]
            ledger["bot_bucket"]  = ledger.get("bot_bucket", 0)  + total * ETF_OPTIONS_SPLIT["bot"]
            save_ledger(ledger)
        except Exception as ex:
            print(f"  ETF options error {sym}: {ex}")


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

    # Cash secured puts
    if cash < 200:
        return
    print(f"\n-- Cash secured puts | ${cash:,.2f} --")
    candidates = scan_best_stocks(cash, bot_capital=capital)
    for stock in candidates:
        sym   = stock["symbol"]
        price = stock["price"]
        star  = get_star_rating(stock)
        if star < 6:
            continue  # options need strong signal
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
        hold_str = f" | 🔒 ${on_hold:,.0f}" if on_hold > 0 else ""
        msg = f"✅ Bot online | 💵 ${cash_ready:,.0f} ready{hold_str} | 24h ${p24h:,.0f}"
        if pulse:
            msg += f"\n{pulse}"
        send_alert(msg)

    except Exception as ex:
        print(f"Startup error: {ex}")

    if is_market_open():
        run_strategy_safe()

    schedule.every(CHECK_INTERVAL).minutes.do(run_strategy_safe)
    schedule.every(5).minutes.do(check_balance_24_7)
    # Daily summary at 4 PM ET
    schedule.every().day.at("16:00").do(send_daily_summary)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
