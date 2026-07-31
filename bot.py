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
    find_best_cash_secured_put, place_cash_secured_put, check_put_already_open,
    get_open_options, run_wheel,
    find_swing_covered_call, get_swing_call_contracts, close_swing_call
)
from options_scanner import scan_stock_options, scan_etf_options_live
from scanner import (
    get_price_history, get_mtf_conviction, score_stock,
    scan_best_stocks
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

DAY_PCT          = 0.00   # No day trades
SWING_PCT        = 0.40   # 40% swing trades (up to 3 positions)

# Pre-bot positions — never apply TP1/TP2/trail to these
# These were held before the bot started trading
PRE_BOT_POSITIONS = {"LCID", "OPEN"}  # hardcoded base; snapshot merged at runtime

def get_pre_bot_positions(held_symbols=None):
    """
    Hands-off positions = pre-bot base + snapshot + ANYTHING the bot didn't buy.
    Whole-portfolio protection: if the bot didn't personally purchase it
    (not in bot_owned), it's protected — never sold, never counts toward max.
    """
    try:
        lg = load_ledger()
        snap = set(lg.get("pre_bot_snapshot", []))
        base = PRE_BOT_POSITIONS | snap
        # If we know current holdings, protect everything not bot-bought
        if held_symbols is not None:
            bot_owned = set(lg.get("bot_owned", []))
            base = base | (set(held_symbols) - bot_owned)
        return base
    except Exception:
        return PRE_BOT_POSITIONS
STOCK_OPT_PCT    = 0.50   # 50% stock options (primary strategy)
ETF_OPT_PCT      = 0.00   # ETF options self-fund from ETF portfolio (not trading cash)
RESERVE_PCT      = 0.10   # 10% SGOV/reserve
MAX_SWING_POSITIONS = 3
MAX_DAY_TRADES_PER_WEEK = 0  # Day trades disabled
ETF_OPTIONS_SPLIT = {"etf": 0.20, "cash": 0.40, "bot": 0.40}
COMMISSION_PER_CONTRACT = 0.65

BASE_URL          = "https://api.schwabapi.com/trader/v1"
MARKET_URL        = "https://api.schwabapi.com/marketdata/v1"
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", 0.07))
CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL_MINUTES", 30))

# ── Profit splits ─────────────────────────────────────────────────────────────
STOCK_SPLIT  = {"etf": 0.20, "cash": 0.40, "bot": 0.40}
OPTIONS_SPLIT = {"etf": 0.20, "cash": 0.40, "bot": 0.40}
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
    # RSI removed from scanner — skip RSI star
    # stars += 0
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
    # imports at top level
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
    if room < 5000:    return round(bot_capital * 0.25, 2)
    if room < 10000:   return round(bot_capital * 0.25, 2)
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
    # No warmup needed — swing trades only

    # Cooldown — 2 consecutive losses
    if stats["consecutive_losses"] >= 2:
        return False, "cooldown_2_losses"

    # Win rate gate — only applies after 10+ trades
    wr = get_win_rate()
    wr_hist = load_ledger().get("win_rate_history", [])
    if len(wr_hist) >= 10 and wr < 0.40:
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
        # CALL FOLLOWS SWING: if a covered call is open on this swing,
        # buy-to-close it FIRST (never leave a naked call) before selling shares.
        call_contracts = get_swing_call_contracts(encrypted, symbol)
        if call_contracts > 0:
            res = close_swing_call(encrypted, symbol, 0)  # close ALL on full exit
            if res["closed"]:
                send_alert("[ CALL CLOSE ] " + symbol + " x" + str(res["contracts"]) + "\nfollows swing exit")

        place_equity_order(encrypted, symbol, quantity, "SELL")
        proceeds = quantity * price
        split    = record_sell_and_split(symbol, quantity, price, proceeds,
                                         STOCK_SPLIT["etf"], STOCK_SPLIT["cash"], STOCK_SPLIT["bot"])
        cash    += proceeds
        profit   = split["profit"]

        record_trade_result(profit, "stock")
        update_win_rate(profit)

        # TAX ACCRUAL (background) — track tax owed on short-term gains.
        # Does NOT touch trading. Just accumulates so we know how much
        # ETF to sell later to cover it. Swings/options run full speed.
        if profit > 0:
            _tl = load_ledger()
            _tl["ytd_tax_owed"] = _tl.get("ytd_tax_owed", 0.0) + profit * 0.3775
            save_ledger(_tl)

        if profit > 0:
            msg_sell = "[ OUT ] " + symbol + " +" + f"{profit:,.2f}" + "\n━━━━━━━━━━━━━━━━━━\nETF    +" + f"{split['etf_cut']:,.2f}" + "\nCASH   +" + f"{split['cash_cut']:,.2f}" + "\nBOT    +" + f"{split['bot_cut']:,.2f}"
            send_alert(msg_sell)
        else:
            tag = "🛑" if "stop" in reason or "trail" in reason or "breakeven" in reason else "📉"
            send_alert("[ CUT ] " + symbol + " " + f"{profit:,.2f}" + "\n━━━━━━━━━━━━━━━━━━\nEXIT   " + reason.upper())
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
        # Whole-portfolio protection: all current holdings (for hands-off check)
        held_syms = {p["instrument"]["symbol"] for p in positions}
        for pos in positions:
            sym = pos["instrument"]["symbol"]
            if sym not in BOT_STOCKS or sym in sold_this_run:
                continue

            qty    = int(pos.get("longQuantity", 0))
            price  = pos.get("marketValue", 0) / max(qty, 1)
            if qty < 1 or price <= 0:
                continue

            update_high_price(sym, price)
            trail_info = get_trailing_stop_info(sym)  # always fresh from ledger
            trigger    = None

            # ── COVERED CALL ON SWING — sell when green + momentum fading ──
            # Extra income on a swing that's profitable but stalling.
            # Only 100+ shares (whole contract), no existing call, weak momentum.
            try:
                if qty >= 100 and sym not in get_pre_bot_positions(held_syms):
                    bt = load_ledger().get("open_trades", {}).get(sym, {})
                    buy_p = bt.get("buy_price", 0)
                    if buy_p > 0 and price > buy_p:  # GREEN only
                        # Momentum check — sell call only if FADING (won't moonshot)
                        conv_now = get_mtf_conviction(sym)
                        if conv_now <= 2:  # weak/fading — safe to cap with a call
                            if not check_covered_call_already_open(encrypted, sym):
                                call = find_swing_covered_call(sym, qty, buy_p)
                                if call:
                                    place_covered_call(encrypted, call["opt_symbol"],
                                                       call["contracts"], call["premium"])
                                    msg  = "[ SWING CALL ] " + sym + "\n"
                                    msg += "STRIKE " + str(call["strike"]) + " x" + str(call["contracts"]) + "\n"
                                    msg += "PREM   $" + f"{call['total_prem']:.2f}" + " income\n"
                                    msg += "DTE    " + str(call["dte"]) + "d | follows swing"
                                    send_alert(msg)
                                    print(f"  Swing call sold: {sym} strike {call['strike']} ${call['total_prem']:.2f}")
            except Exception as ex:
                print(f"Swing call error {sym}: {ex}")

            # ── TP1 / TP2 scale-out — only for bot-entered positions ──
            bot_trade = load_ledger().get("open_trades", {}).get(sym, {})
            # Skip TP1/TP2 for pre-bot positions and positions not in ledger
            if sym in get_pre_bot_positions(held_syms):
                bot_trade = {}
            if trail_info and not trigger and bot_trade:
                buy_px  = trail_info["buy_price"]
                tp1_pct = trail_info.get("tp1_pct", 0.07)
                tp2_pct = trail_info.get("tp2_pct", 0.10)
                tp1_hit = trail_info.get("tp1_hit", False)
                tp2_hit = trail_info.get("tp2_hit", False)
                tp1_px  = buy_px * (1 + tp1_pct)
                tp2_px  = buy_px * (1 + tp2_pct)
                if not tp1_hit and price >= tp1_px:
                    tp1_qty = max(1, qty // 3)
                    try:
                        # CALL FOLLOWS SWING (scale): after selling tp1_qty shares,
                        # remaining shares must still cover the call. Close whole
                        # contracts to stay covered (never naked).
                        call_ct = get_swing_call_contracts(encrypted, sym)
                        if call_ct > 0:
                            shares_after = qty - tp1_qty
                            contracts_still_covered = shares_after // 100
                            contracts_to_close = call_ct - contracts_still_covered
                            if contracts_to_close > 0:
                                res = close_swing_call(encrypted, sym, contracts_to_close)
                                if res["closed"]:
                                    send_alert("[ CALL SCALE ] " + sym + " -" + str(res["contracts"]) + " contract(s)\nstays covered")

                        place_equity_order(encrypted, sym, tp1_qty, "SELL")
                        profit_tp1 = round(tp1_qty * (price - buy_px), 2)
                        pct_gain   = round((price / buy_px - 1) * 100, 1)
                        # Save to ledger immediately and persistently
                        _ld = load_ledger()
                        if sym in _ld.get("open_trades", {}):
                            _ld["open_trades"][sym]["tp1_hit"] = True
                            _ld["open_trades"][sym]["tp2_hit"] = False
                            _ld["open_trades"][sym]["quantity"] = max(0, qty - tp1_qty)
                            _ld["open_trades"][sym]["stop_pct"] = 0.0  # stop at breakeven
                            save_ledger(_ld)
                        # Update trail_info in memory so current cycle knows tp1 is hit
                        if trail_info:
                            trail_info["tp1_hit"] = True
                        send_alert("[ TP1 ] " + sym + " 1/3\nPRICE $" + f"{price:.2f}" + " | +" + f"{pct_gain}" + "%\nPROFIT +$" + f"{profit_tp1:.2f}")
                        print(f"  TP1 hit {sym} — ledger updated, tp1_hit=True")
                    except Exception as ex:
                        print(f"TP1 error {sym}: {ex}")
                    continue
                elif tp1_hit and not tp2_hit and price >= tp2_px:
                    tp2_qty = max(1, qty // 3)
                    try:
                        # CALL FOLLOWS SWING (scale) — keep covered on TP2 too
                        call_ct2 = get_swing_call_contracts(encrypted, sym)
                        if call_ct2 > 0:
                            shares_after2 = qty - tp2_qty
                            covered2 = shares_after2 // 100
                            to_close2 = call_ct2 - covered2
                            if to_close2 > 0:
                                res2 = close_swing_call(encrypted, sym, to_close2)
                                if res2["closed"]:
                                    send_alert("[ CALL SCALE ] " + sym + " -" + str(res2["contracts"]) + " contract(s)\nstays covered")

                        place_equity_order(encrypted, sym, tp2_qty, "SELL")
                        profit_tp2 = round(tp2_qty * (price - buy_px), 2)
                        pct_gain2  = round((price / buy_px - 1) * 100, 1)
                        _ld2 = load_ledger()
                        if sym in _ld2.get("open_trades", {}):
                            _ld2["open_trades"][sym]["tp2_hit"] = True
                            _ld2["open_trades"][sym]["quantity"] = max(0, qty - tp2_qty)
                            save_ledger(_ld2)
                        # Update trail_info in memory
                        if trail_info:
                            trail_info["tp2_hit"] = True
                        send_alert("[ TP2 ] " + sym + " 2/3\nPRICE $" + f"{price:.2f}" + " | +" + f"{pct_gain2}" + "%\nPROFIT +$" + f"{profit_tp2:.2f}")
                        print(f"  TP2 hit {sym} — ledger updated, tp2_hit=True")
                    except Exception as ex:
                        print(f"TP2 error {sym}: {ex}")
                    continue


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
                        # imports at top level
                        _candles = _gph(sym)
                    except Exception:
                        _candles = None
                    bought_at = bot_trade.get("bought_at") if bot_trade else None
                    stop_info = get_dynamic_stop(buy_px, high_px, price, TRAILING_STOP_PCT, _candles, bought_at)
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

                ok, reason = can_trade(capital, stats)
                if not ok:
                    print(f"  {symbol}: skip — {reason}")
                    continue

                # Max 3 swing positions at once
                # Count only ACTIVE bot swings toward max — pre-owned excluded
                _ot = load_ledger().get("open_trades", {})
                open_count = sum(1 for t in _ot.values() if not t.get("pre_owned", False))
                if open_count >= MAX_SWING_POSITIONS:
                    print(f"  {symbol}: skip — max {MAX_SWING_POSITIONS} bot swings open (pre-owned not counted)")
                    break

                # MTF conviction sizing
                # Conviction-based sizing — portfolio-aware, no FVG gate
                _conv = get_mtf_conviction(symbol)
                position_size = get_swing_position_size_v2(_conv, encrypted, cash)
                if position_size == 0:
                    print(f"  {symbol}: skip — conviction {_conv}/4 too low")
                    continue
                position_size = green_day_scale(position_size, stats)
                position_size = min(position_size, cash)

                quantity = int(position_size // price)

                # COVERED-CALL PREP: if we're close to 100 shares (80-99),
                # and cash allows, round up to 100 to enable selling a covered
                # call on the swing later (double income: swing + call premium)
                if 80 <= quantity < 100:
                    cost_for_100 = 100 * price
                    if cost_for_100 <= cash * 0.95:  # leave 5% buffer
                        quantity = 100

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
                    # imports at top level
                    conv  = get_mtf_conviction(symbol)
                    stars = get_star_rating(stock)
                    record_conviction_count(conv)
                    msg_buy  = "[ IN ] " + symbol + "\n━━━━━━━━━━━━━━━━━━\n"
                    msg_buy += "PRICE  " + f"{price:.2f}" + "\nQTY    " + str(quantity) + "\nCONV   " + str(conv) + "/4 S" + str(stars) + "\nCOST   " + f"{cost:,.2f}"
                    send_alert(msg_buy)
                    print(f"  Bought {quantity} {symbol} @ ${price:.2f} | star={star}")
                except Exception as ex:
                    send_alert(f"Buy error {symbol}: {ex}")

        # ── Options ──
        run_options(encrypted, positions, cash, stats, capital)
        # ── Monitor open option positions — exit at 50% profit ──
        try:
            open_opts = get_open_options(encrypted)
            swing_syms = set(load_ledger().get("open_trades", {}).keys())
            for opt_pos in open_opts:
                inst       = opt_pos.get("instrument", {})
                opt_sym    = inst.get("symbol", "")
                short_qty  = opt_pos.get("shortQuantity", 0)
                if short_qty <= 0:
                    continue
                # CALLS ON SWINGS FOLLOW THE SWING — skip the 50% rule for them.
                # If this option's underlying is an open swing AND it's a call,
                # let the swing exit logic handle it, not the standalone monitor.
                underlying = opt_sym[:opt_sym.find(" ")] if " " in opt_sym else opt_sym[:4].strip()
                is_call    = "C" in opt_sym[-10:] and "P" not in opt_sym[-9:]
                if is_call and underlying in swing_syms:
                    continue  # covered call on a swing — follows swing, not 50%
                # Get current mark price
                mkt_val    = abs(opt_pos.get("marketValue", 0))
                avg_price  = abs(opt_pos.get("averagePrice", 0))
                if avg_price <= 0:
                    continue
                # Exit at 50% profit (current value ≤ 50% of entry premium)
                if mkt_val <= avg_price * 0.50:
                    try:
                        resp = requests.post(
                            f"https://api.schwabapi.com/trader/v1/accounts/{encrypted}/orders",
                            headers={"Authorization": f"Bearer {get_valid_token()}", "Content-Type": "application/json"},
                            json={
                                "orderType": "MARKET",
                                "session": "NORMAL",
                                "duration": "DAY",
                                "orderStrategyType": "SINGLE",
                                "orderLegCollection": [{
                                    "instruction": "BUY_TO_CLOSE",
                                    "quantity": short_qty,
                                    "instrument": {
                                        "symbol":            opt_sym,
                                        "assetType":         "OPTION",
                                        "putCall":           "PUT" if "P" in opt_sym[-10:] else "CALL",
                                        "underlyingSymbol":  opt_sym[:4].strip()
                                    }
                                }]
                            },
                            timeout=15
                        )
                        if resp.ok or resp.status_code == 201:
                            profit = (avg_price - mkt_val / 100) * short_qty * 100
                            underlying = opt_sym[:4].strip()
                            is_call = "C" in opt_sym[-10:] and "P" not in opt_sym[-9:]

                            # Route profit: ETF covered calls use 50/50, stock puts use 30/50/20
                            owned_etfs = load_ledger().get("owned_etfs", {})
                            is_etf_call = is_call and underlying in owned_etfs

                            if is_etf_call:
                                split = ETF_OPTIONS_SPLIT
                                label = "ETF OPT EXIT"
                            else:
                                split = OPTIONS_SPLIT
                                label = "STOCK OPT EXIT"

                            # Book profit to buckets
                            lg = load_ledger()
                            lg["etf_bucket"]  = lg.get("etf_bucket", 0)  + profit * split["etf"]
                            lg["cash_bucket"] = lg.get("cash_bucket", 0) + profit * split["cash"]
                            lg["trading_capital"] = lg.get("trading_capital", 0) + profit * split["bot"]
                            save_ledger(lg)

                            # Private-style alert — shows P&L and split, hides totals
                            msg  = "[ " + label + " ] " + underlying + "\n"
                            msg += "PREM $" + f"{avg_price:.2f}" + " → $" + f"{mkt_val/100:.2f}" + "\n"
                            msg += "PROFIT +$" + f"{profit:.2f}" + " (50% hit)\n"
                            msg += "SPLIT  20% ETF | 40% CASH | 40% BOT"
                            send_alert(msg)
                            print(f"  {label}: {opt_sym} at 50% profit ${profit:.2f}")
                    except Exception as ex:
                        print(f"Option exit error {opt_sym}: {ex}")
        except Exception as ex:
            print(f"Options monitor error: {ex}")

        # ── Stock Options (15% = weekly 0-7 DTE, 3 DTE preferred per backtest) ──
        try:
            # imports at top level
            stock_opt_budget = cash * STOCK_OPT_PCT
            stock_opts = scan_stock_options(cash_available=stock_opt_budget)

            # DEPLOY MULTIPLE PUTS until budget is used — don't leave capital idle
            budget_left = stock_opt_budget
            puts_opened = 0
            MAX_STOCK_PUTS = 5  # cap concurrent puts for diversification
            for opp in stock_opts:
                if puts_opened >= MAX_STOCK_PUTS:
                    break
                sym    = opp["symbol"]
                prem   = opp["premium"]
                strike = opp.get("strike", 0)
                collateral = strike * 100  # cash-secured put needs strike × 100

                # Only open if we have collateral left for this put
                if collateral > budget_left:
                    continue
                if check_put_already_open(encrypted, sym):
                    continue

                place_cash_secured_put(encrypted, opp["opt_symbol"], prem)
                budget_left -= collateral
                puts_opened += 1

                msg  = "[ STOCK OPT ] " + sym + " PUT\n"
                msg += "STRIKE " + str(opp["strike"]) + " delta " + str(opp["delta"]) + "\n"
                msg += "PREM   $" + f"{opp['total_prem']:.2f}" + " net\n"
                msg += "YIELD  " + str(opp["ann_yield"]) + "%/yr\n"
                msg += "DTE    " + str(opp["dte"]) + "d weekly\nEXIT @ $" + str(opp.get("exit_at", "50%"))
                send_alert(msg)

            if puts_opened > 0:
                print(f"  Stock options: opened {puts_opened} puts | ${stock_opt_budget - budget_left:.0f} deployed | ${budget_left:.0f} left")
        except Exception as ex:
            print(f"Stock options error: {ex}")

        # ── ETF Options (10% of cash, builds toward roadmap) ──
        try:
            # imports at top level
            # ETF options = covered calls on owned ETF shares (income from holdings, not cash)
            # Pass a nominal budget just for the guard; real source is owned_etfs shares
            etf_opts = scan_etf_options_live(
                cash_available=999999,  # covered calls don't use trading cash
                positions=positions
            )

            for opp in etf_opts[:3]:  # write calls on up to 3 owned ETFs
                sym  = opp["symbol"]
                typ  = opp["type"]
                prem = opp["premium"]

                if typ == "call":
                    if check_covered_call_already_open(encrypted, sym):
                        continue
                    contracts = opp.get("shares_owned", 100) // 100
                    place_covered_call(encrypted, opp["opt_symbol"], contracts, prem)
                    msg  = "[ ETF OPT ] " + sym + " CALL\n"
                    msg += "STRIKE " + str(opp["strike"]) + " delta " + str(opp["delta"]) + "\n"
                    msg += "PREM   $" + f"{opp['total_prem']:.2f}" + " net\n"
                    msg += "YIELD  " + str(opp["ann_yield"]) + "%/yr\n"
                    msg += "DTE    " + str(opp["dte"]) + "d | roadmap: " + opp.get("roadmap_note", "")
                    send_alert(msg)

                elif typ == "call":
                    if check_covered_call_already_open(encrypted, sym):
                        continue
                    place_covered_call(encrypted, opp["opt_symbol"],
                                      opp.get("contracts", 1), prem)
                    msg  = "[ ETF OPT ] " + sym + " CALL\n"
                    msg += "STRIKE " + str(opp["strike"]) + " delta " + str(opp["delta"]) + "\n"
                    msg += "PREM   $" + f"{opp['total_prem']:.2f}" + " net\n"
                    msg += "YIELD  " + str(opp["ann_yield"]) + "%/yr\n"
                    msg += "DTE    " + str(opp["dte"]) + "d | roadmap: " + opp.get("roadmap_note", "")
                    send_alert(msg)

        except Exception as ex:
            print(f"ETF options error: {ex}")
        run_sgov_parking(encrypted, cash, capital)

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


def get_total_portfolio(encrypted: str = None) -> float:
    """Total portfolio = bot capital + ETF portfolio + cash."""
    try:
        from auth import get_valid_token as _gvt
        _headers = {"Authorization": f"Bearer {_gvt()}"}
        if not encrypted:
            resp = requests.get(
                "https://api.schwabapi.com/trader/v1/accounts/accountNumbers",
                headers=_headers, timeout=10
            )
            encrypted = resp.json()[0]["hashValue"]
        resp = requests.get(
            f"https://api.schwabapi.com/trader/v1/accounts/{encrypted}?fields=positions",
            headers=_headers, timeout=15
        )
        if resp.ok:
            acct   = resp.json()["securitiesAccount"]
            liquid = acct["currentBalances"].get("liquidationValue", 0)
            return liquid
    except Exception:
        pass
    ledger = load_ledger()
    return ledger.get("trading_capital", 2167)


def get_swing_ceiling(encrypted: str = None, cash_available: float = 0) -> float:
    """
    Swing position ceiling = 65% of cash / 3 positions = ~21.67% per trade.
    Allows 3 concurrent swing positions.
    65% swing | 25% options | 10% SGOV reserve.

    At $3,340 cash:
    65% = $2,171 for swings
    Per position: $724 (allows 3 at once)
    """
    if cash_available <= 0:
        ledger = load_ledger()
        cash_available = ledger.get("cash_balance", 1000)
    swing_budget = cash_available * SWING_PCT
    per_position = swing_budget / MAX_SWING_POSITIONS
    return round(per_position, 2)


def get_swing_position_size_v2(conviction: int, encrypted: str = None, cash: float = 0) -> float:
    """
    Swing position size based on conviction only. No FVG gate.
    4/4 → full ceiling
    3/4 → 70% ceiling
    2/4 → 50% ceiling
    Below 2/4 → no trade
    """
    ceiling = get_swing_ceiling(encrypted, cash)
    if conviction >= 4:
        return ceiling
    elif conviction >= 3:
        return ceiling * 0.70
    elif conviction >= 2:
        return ceiling * 0.50
    return 0


def run_sgov_parking(encrypted: str, cash: float, capital: float):
    """Smart SGOV parking — tax reserve, idle ETF bucket, bot excess."""
    try:
        ledger    = load_ledger()
        tax_owed  = ledger.get("ytd_tax_owed", 0)
        etf_b     = ledger.get("etf_bucket", 0)
        bot_b     = ledger.get("bot_bucket", 0)
        last_sweep = ledger.get("last_etf_sweep_date", "")
        idle_days = 0
        if last_sweep:
            try:
                from datetime import datetime as _dt
                idle_days = (_dt.now() - _dt.strptime(last_sweep[:10], "%Y-%m-%d")).days
            except Exception:
                pass
        tax_park = min(tax_owed, cash * 0.30) if tax_owed > 100 else 0
        etf_park = etf_b if (etf_b < 50 and idle_days >= 7) else 0
        bot_park    = max(bot_b - 200, 0) if bot_b > 300 else 0

        # Reserve — maintain 10% of capital in SGOV, don't re-buy if already parked
        sgov_owned  = ledger.get("sgov_shares", 0)
        sgov_value  = ledger.get("sgov_value", 0)
        target_reserve = capital * RESERVE_PCT
        reserve_gap    = max(0, target_reserve - sgov_value)
        # Only park the gap needed to reach target, and only if cash allows
        reserve_amt = min(reserve_gap, cash * 0.50) if (cash > 500 and reserve_gap > 50) else 0

        total       = tax_park + etf_park + bot_park + reserve_amt
        if total < 100:
            return
        resp = requests.get(
            "https://api.schwabapi.com/marketdata/v1/quotes",
            headers={"Authorization": f"Bearer {get_valid_token()}"},
            params={"symbols": "SGOV"}, timeout=10
        )
        if not resp.ok:
            return
        sgov_px = resp.json().get("SGOV", {}).get("quote", {}).get("lastPrice", 0)
        if sgov_px <= 0:
            return
        shares = int(total // sgov_px)
        if shares < 1:
            return
        r = place_equity_order(encrypted, "SGOV", shares, "BUY")
        if r and r.headers.get("Location"):
            # Track SGOV holdings so we don't over-park
            lg = load_ledger()
            lg["sgov_shares"] = lg.get("sgov_shares", 0) + shares
            lg["sgov_value"]  = lg.get("sgov_value", 0) + shares * sgov_px
            save_ledger(lg)
            send_alert("[ CIRCUIT ] SGOV PARK\nSGOV x" + str(shares) + " @ $" + f"{sgov_px:.2f}" + "\nRESERVE ~5%/yr")
    except Exception as ex:
        print(f"SGOV error: {ex}")

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
    """
    Smart ETF accumulation — builds toward 100 shares to unlock covered calls.
    Priority: finish the ETF closest to 100 shares first (fastest to income).
    Falls back to scanner picks only if all owned ETFs already at 100+.
    """
    from roadmap import get_etf_build_target

    etf_bucket = get_etf_bucket()

    # Get smart build target — which ETF to finish toward 100 shares
    target_info = get_etf_build_target(encrypted)
    build       = target_info.get("build_target") if target_info else None

    if build:
        sym   = build["symbol"]
        price = build["share_price"]
        needed = build["shares_needed"]
        threshold = price  # need at least 1 share worth

        print(f"\n-- ETF build: {sym} {build['shares']}/100 | bucket ${etf_bucket:,.2f} --")

        if etf_bucket < threshold:
            print(f"   Need ${threshold:.2f} for 1 share — accumulating")
            return

        # Buy as many shares as bucket allows, capped at what's needed to hit 100
        affordable = int(etf_bucket // price)
        qty = min(affordable, needed)
        if qty < 1:
            return

        try:
            place_equity_order(encrypted, sym, qty, "BUY")
            cost = qty * price
            deduct_etf_bucket(cost)
            new_total = build["shares"] + qty
            if new_total >= 100:
                send_alert(f"🎯 Bought {qty} {sym} @ ${price:.2f} → {int(new_total)} shares! COVERED CALLS UNLOCKED")
            else:
                send_alert(f"📊 Bought {qty} {sym} @ ${price:.2f} → {int(new_total)}/100 shares (building)")
        except Exception as ex:
            print(f"  ETF build error {sym}: {ex}")
        return

    # All owned ETFs already at 100+ — use scanner for new positions
    ready = target_info.get("ready_to_write", []) if target_info else []
    if ready:
        print(f"\n-- ETF sweep: {len(ready)} ETF(s) writing calls | bucket ${etf_bucket:,.2f} --")

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



_last_update_id = 0

def poll_telegram_commands():
    global _last_update_id
    token = os.getenv("TELEGRAM_TOKEN")
    chat  = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 5}, timeout=10
        )
        if not resp.ok:
            return
        for update in resp.json().get("result", []):
            _last_update_id = update["update_id"]
            msg  = update.get("message", {})
            text = msg.get("text", "").strip().lower()
            uid  = str(msg.get("chat", {}).get("id", ""))
            if uid != str(chat):
                continue
            ledger = load_ledger()
            if text == "/pause":
                ledger["bot_paused"] = True
                save_ledger(ledger)
                send_alert("[ CIRCUIT ] PAUSED\nTrading stopped\nPositions held")
            elif text == "/resume":
                ledger["bot_paused"] = False
                save_ledger(ledger)
                send_alert("[ CIRCUIT ] RESUMED\nTrading active")
            elif text == "/status":
                capital = ledger.get("trading_capital", 0)
                cash_b  = ledger.get("cash_bucket", 0)
                etf_b   = ledger.get("etf_bucket", 0)
                pdt     = ledger.get("day_trades_this_week", 0)
                open_t  = list(ledger.get("open_trades", {}).keys())
                state   = "PAUSED" if ledger.get("bot_paused") else "LIVE"
                wr      = ledger.get("current_win_rate", 0)
                parts   = [
                    "[ CIRCUIT ] STATUS",
                    "STATE  " + state,
                    "CAP    " + f"{capital:,.2f}",
                    "SWING  ON",
                    "ETF    " + f"{etf_b:,.2f}",
                    "CASH   " + f"{cash_b:,.2f}",
                    "PDT    " + str(pdt) + "/3",
                    "WIN    " + f"{wr:.1%}",
                    "OPEN   " + (", ".join(open_t) if open_t else "none"),
                ]
                send_alert("\n".join(parts))
            elif text == "/backtest":
                send_alert("[ CIRCUIT ] BACKTEST\nRunning 14d...\nResults in ~5 min")
                import threading
                def _run():
                    from backtest import run_backtest
                    run_backtest(days=14, bot_capital=load_ledger().get("trading_capital", 2167))
                threading.Thread(target=_run, daemon=True).start()
            elif text == "/tax":
                try:
                    from roadmap import get_etf_tax_coverage
                    accts = requests.get(
                        "https://api.schwabapi.com/trader/v1/accounts/accountNumbers",
                        headers={"Authorization": f"Bearer {get_valid_token()}"}
                    ).json()
                    enc = accts[0]["hashValue"]

                    tax_owed = load_ledger().get("ytd_tax_owed", 0.0)
                    cov = get_etf_tax_coverage(enc)

                    parts = [
                        "[ CIRCUIT ] TAX PLAN",
                        "━━━━━━━━━━━━━━━━━━",
                        "TAX OWED  $" + f"{tax_owed:,.2f}",
                        "(short-term swing/options)",
                        "",
                    ]
                    if cov["plan"]:
                        parts.append("TO COVER — sell ETF:")
                        for p in cov["plan"]:
                            parts.append(f"  {p['symbol']}: {p['shares']} sh = ${p['proceeds']:,.0f}")
                            parts.append(f"    ({p['shares_left']} left after)")
                        if not cov["covered"]:
                            parts.append(f"SHORTFALL $" + f"{cov['shortfall']:,.2f}")
                        parts.append("")
                        parts.append("You sell manually when ready")
                    else:
                        parts.append(cov.get("note", "No tax action needed"))
                    send_alert("\n".join(parts))
                except Exception as ex:
                    send_alert("Tax error: " + str(ex))
            elif text == "/roadmap":
                try:
                    from roadmap import send_roadmap_alert
                    accts = requests.get(
                        "https://api.schwabapi.com/trader/v1/accounts/accountNumbers",
                        headers={"Authorization": f"Bearer {get_valid_token()}"}
                    ).json()
                    send_roadmap_alert(accts[0]["hashValue"])
                except Exception as ex:
                    send_alert("Roadmap error: " + str(ex))
            elif text == "/help":
                parts = [
                    "[ CIRCUIT ] COMMANDS",
                    "━━━━━━━━━━━━━━━━━━",
                    "/status   — capital, buckets, win rate",
                    "/pause    — stop trading, hold positions",
                    "/resume   — restart trading",
                    "/roadmap  — ETF options path",
                    "/backtest — run 14d backtest",
                    "/tax      — YTD tax report",
                    "━━━━━━━━━━━━━━━━━━",
                    "7:30 AM  — pre-market",
                    "4:05 PM  — session summary",
                    "4:30 PM  — backtest",
                    "5:00 PM  — EOD",
                    "Sunday 8 AM — weekly report",
                ]
                send_alert("\n".join(parts))
    except Exception as ex:
        print(f"Command poll error: {ex}")

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
        msg = "[ CIRCUIT ] LIVE\n━━━━━━━━━━━━━━━━━━\n" + "CAP    " + f"{capital:,.2f}" + "\nSWING  " + f"{capital:,.2f}" + "\nCASH   " + f"{cash_ready:,.2f}" + "\nMODE   SWING + OPTIONS"
        if pulse:
            msg += f"\n{pulse}"
        send_alert(msg)

    except Exception as ex:
        print(f"Startup error: {ex}")

    if is_market_open():
        run_strategy_safe()

    schedule.every(CHECK_INTERVAL).minutes.do(run_strategy_safe)
    schedule.every(1).minutes.do(poll_telegram_commands)
    schedule.every(5).minutes.do(check_balance_24_7)
    # Daily summary at 4 PM ET
    schedule.every().day.at("16:00").do(send_daily_summary)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
