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
from scanner import scan_best_stocks, scan_best_etfs, get_market_pulse, get_tier, get_etf_level
from strategy import get_trade_stocks, get_signal
from options import find_best_covered_call, place_covered_call, check_covered_call_already_open, find_best_cash_secured_put, place_cash_secured_put, check_put_already_open
from dividends import get_recent_dividends
from telegram import send_alert
from token_manager import check_token_health
from ledger import (
    sync_ledger_from_schwab, load_ledger, save_ledger,
    record_buy, record_sell_and_split,
    get_profit_bucket, get_trading_capital,
    get_etf_bucket, get_cash_bucket, get_bot_bucket,
    deduct_etf_bucket, detect_deposit, detect_withdrawal,
    get_withdrawal_stats, record_dividend, get_dividend_stats,
    mark_dividend_seen, update_high_price, get_trailing_stop_info,
    get_dynamic_stop, BOT_STOCKS, ETF_MIN_SWEEP
)

load_dotenv()


def is_market_open() -> bool:
    """Check market hours using Schwab API — handles holidays and half days automatically."""
    try:
        from datetime import date as _date
        today = _date.today().strftime("%Y-%m-%d")
        resp = requests.get(
            f"https://api.schwabapi.com/marketdata/v1/markets",
            headers={"Authorization": f"Bearer {get_valid_token()}"},
            params={"markets": "equity", "date": today},
            timeout=10
        )
        if not resp.ok:
            raise Exception(f"API error {resp.status_code}")

        data = resp.json()
        # Navigate to equity market hours
        equity = data.get("equity", {})
        for session_type in ["EQ", "equity"]:
            if session_type in equity:
                market = equity[session_type]
                is_open = market.get("isOpen", False)
                if not is_open:
                    print(f"Market closed — Schwab API says closed today")
                    return False
                # Check if current time is within session hours
                et = pytz.timezone("America/New_York")
                now = datetime.now(et)
                session = market.get("sessionHours", {}).get("regularMarket", [{}])[0]
                start_str = session.get("start", "")
                end_str   = session.get("end", "")
                if start_str and end_str:
                    from datetime import datetime as _dt
                    start = _dt.fromisoformat(start_str).astimezone(et)
                    end   = _dt.fromisoformat(end_str).astimezone(et)
                    if not (start <= now <= end):
                        print(f"Market closed — outside hours ({now.strftime('%H:%M')} ET)")
                        return False
                return True
        print("Market closed — could not determine hours")
        return False
    except Exception as e:
        # Fallback to pytz-based check if API fails
        print(f"Market hours API error: {e} — using fallback")
        et = pytz.timezone("America/New_York")
        now = datetime.now(et)
        if now.weekday() > 4:
            return False
        market_open  = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
        return market_open <= now <= market_close

def handle_shutdown(signum, frame):
    print("Shutdown signal received — bot stopping cleanly.")
    sys.exit(0)

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

BASE_URL       = "https://api.schwabapi.com/trader/v1"
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", 0.07))  # 7% drop from peak triggers sell
ETF_PCT        = float(os.getenv("ETF_PCT", 0.60))
CASH_PCT       = float(os.getenv("CASH_PCT", 0.30))
BOT_PCT        = float(os.getenv("BOT_PCT", 0.10))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", 30))


# ── Schwab API helpers ───────────────────────────────────────────────────────

def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_account_numbers() -> list:
    resp = requests.get(f"{BASE_URL}/accounts/accountNumbers", headers=headers())
    resp.raise_for_status()
    return resp.json()


def get_account(encrypted: str) -> dict:
    resp = requests.get(
        f"{BASE_URL}/accounts/{encrypted}",
        headers=headers(),
        params={"fields": "positions"}
    )
    resp.raise_for_status()
    return resp.json()


def get_cash_balance(account: dict) -> float:
    """Returns total cash balance for deposit/withdrawal tracking."""
    try:
        balances = account["securitiesAccount"]["currentBalances"]
        return max(balances.get("cashBalance", 0.0), 0.0)
    except KeyError:
        return 0.0


def get_available_cash(account: dict) -> float:
    """Returns cash actually available for trading — excludes put collateral and unsettled funds."""
    try:
        balances = account["securitiesAccount"]["currentBalances"]
        available = balances.get("cashAvailableForTrading", None)
        if available is not None:
            return max(available, 0.0)
        return max(balances.get("cashBalance", 0.0), 0.0)
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


def place_equity_order(encrypted: str, symbol: str, quantity: int, instruction: str):
    order = {
        "orderType": "MARKET",
        "session":   "NORMAL",
        "duration":  "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": instruction,
            "quantity":    quantity,
            "instrument":  {"symbol": symbol, "assetType": "EQUITY"}
        }]
    }
    resp = requests.post(
        f"{BASE_URL}/accounts/{encrypted}/orders",
        headers={**headers(), "Content-Type": "application/json"},
        json=order
    )
    resp.raise_for_status()
    return resp


def check_order_filled(encrypted: str, order_location: str) -> dict:
    """
    Check if an order actually filled or got rejected/canceled.
    order_location is the Location header from the order POST response.
    Returns dict with status and statusDescription.
    """
    import time
    time.sleep(1.5)  # give Schwab a moment to process
    try:
        resp = requests.get(order_location, headers=headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {
            "status":      data.get("status", "UNKNOWN"),
            "description": data.get("statusDescription", ""),
            "filled":      data.get("status") == "FILLED"
        }
    except Exception as e:
        return {"status": "UNKNOWN", "description": str(e), "filled": False}


# ── Sell with immediate profit split ────────────────────────────────────────

def execute_sell(encrypted: str, symbol: str, quantity: int, price: float, cash: float, reason: str = "signal") -> float:
    try:
        place_equity_order(encrypted, symbol, quantity, "SELL")
        proceeds = quantity * price
        split    = record_sell_and_split(symbol, quantity, price, proceeds, ETF_PCT, CASH_PCT, BOT_PCT)
        cash    += proceeds
        profit   = split["profit"]
        label    = "PROFIT" if profit > 0 else "LOSS"
        tag      = "🛑 Trailing Stop" if reason == "trailing_stop" else "Signal"

        if profit > 0:
            send_alert(
                f"💰 Sold {symbol} x{quantity} @ ${price:.2f} | +${profit:,.2f}\n"
                f"→ ETF ${split['etf_cut']:,.0f} | Cash ${split['cash_cut']:,.0f} | Bot ${split['bot_cut']:,.0f}"
            )
        else:
            send_alert(f"📉 Sold {symbol} x{quantity} @ ${price:.2f} | ${profit:,.2f}")
        print(f"  Sold {quantity} {symbol} @ ${price:.2f} | P&L ${profit:+,.2f} | {tag}")
    except Exception as e:
        send_alert(f"*Sell error* {symbol}: {e}")
    return cash


# ── Stock strategy ───────────────────────────────────────────────────────────

def run_stock_strategy(encrypted: str, positions: list, cash: float, account_value: float) -> float:
    capital       = get_trading_capital()
    tier_name, tier_cfg = get_tier(capital)
    tier          = tier_cfg["label"]
    position_size = cash * tier_cfg["pos_pct"]
    bought_this_run = set()

    # Always check existing positions for sell signals regardless of cash
    sold_this_run = set()
    for position in positions:
        sym = position["instrument"]["symbol"]
        if sym not in BOT_STOCKS:
            continue
        if sym in sold_this_run:
            continue

        sig   = get_signal(sym)
        price = sig.get("price", 0)
        quantity = int(position.get("longQuantity", 0))
        if quantity < 1 or price <= 0:
            continue

        # Update peak price tracker
        update_high_price(sym, price)
        trail_info = get_trailing_stop_info(sym)

        trigger_reason = None

        if sig["signal"] == "SELL":
            trigger_reason = "signal"
        elif trail_info:
            stop_info = get_dynamic_stop(
                trail_info["buy_price"],
                trail_info["high_price"],
                price,
                TRAILING_STOP_PCT
            )
            if price <= stop_info["stop_price"]:
                trigger_reason = stop_info["reason"]
                print(f"  {sym}: {stop_info['reason']} triggered | profit {stop_info['profit_pct']*100:.1f}% | stop ${stop_info['stop_price']:.2f}")

        if trigger_reason:
            # Never sell a stock that has an open covered call
            if check_covered_call_already_open(encrypted, sym):
                print(f"  {sym}: skipping sell — covered call open")
                continue
            sold_this_run.add(sym)
            if trigger_reason == "trailing_stop":
                high = trail_info["high_price"]
                print(f"  {sym}: trailing stop triggered — peak ${high:.2f} now ${price:.2f} ({(high-price)/high*100:.1f}% drop)")
            cash = execute_sell(encrypted, sym, quantity, price, cash, trigger_reason)

    # Only buy if we have enough cash
    if cash < 10:
        print("Not enough cash to buy — monitoring positions only.")
        return cash

    top_stocks = scan_best_stocks(cash, account_value=capital)
    if not top_stocks:
        print("No buy signals found in scan.")
        return cash

    print(f"\n-- Buying scanner picks [{tier}] | Position size: ${position_size:,.2f} --")

    for stock in top_stocks:
        symbol = stock["symbol"]
        price  = stock["price"]

        if symbol in bought_this_run:
            continue

        # Skip if already own it
        position = get_position_for(positions, symbol)
        if position:
            continue

        if cash < price:
            continue
        quantity = int(position_size // price)
        if quantity < 1:
            continue

        try:
            order_resp = place_equity_order(encrypted, symbol, quantity, "BUY")
            order_location = order_resp.headers.get("Location", "")
            cost = quantity * price

            if order_location:
                check = check_order_filled(encrypted, order_location)
                if not check["filled"] and check["status"] in ("REJECTED", "CANCELED"):
                    send_alert(f"❌ {symbol} canceled — {check['description'][:50]}")
                    bought_this_run.add(symbol)  # don't retry this run
                    continue

            cash -= cost
            bought_this_run.add(symbol)
            record_buy(symbol, quantity, price, cost)
            send_alert(f"📈 Bought {symbol} x{quantity} @ ${price:.2f}")
            print(f"  Bought {quantity} {symbol} @ ${price:.2f}")
        except Exception as e:
            send_alert(f"*Buy error* {symbol}: {e}")

    return cash


# ── Options strategy ─────────────────────────────────────────────────────────

def run_options_strategy(encrypted: str, positions: list, account_value: float):
    print("\n-- Covered calls --")
    for position in positions:
        symbol = position["instrument"]["symbol"]
        if position["instrument"].get("assetType") != "EQUITY":
            continue
        shares = int(position.get("longQuantity", 0))
        if shares < 100:
            continue

        # Skip if already have open covered call on this stock
        if check_covered_call_already_open(encrypted, symbol):
            print(f"  {symbol}: covered call already open")
            continue

        best_call = find_best_covered_call(symbol, shares)
        if not best_call:
            print(f"  {symbol}: no good covered call found")
            continue

        print(f"  {symbol}: placing covered call strike ${best_call['strike']} exp {best_call['expiry']} premium ${best_call['premium']:.2f}")

        try:
            cc_resp = place_covered_call(encrypted, best_call["option_symbol"], best_call["contracts"], best_call["premium"])
            cc_location = cc_resp.headers.get("Location", "")

            if cc_location:
                check = check_order_filled(encrypted, cc_location)
                if not check["filled"] and check["status"] in ("REJECTED", "CANCELED"):
                    send_alert(f"❌ {symbol} call canceled — {check['description'][:50]}")
                    continue

            total    = best_call["total_premium"]
            etf_cut  = total * 0.60
            cash_cut = total * 0.30
            bot_cut  = total * 0.10

            from ledger import load_ledger, save_ledger
            ledger = load_ledger()
            ledger["profit_bucket"]   = ledger.get("profit_bucket", 0.0) + total
            ledger["etf_bucket"]      = ledger.get("etf_bucket", 0.0) + etf_cut
            ledger["cash_bucket"]     = ledger.get("cash_bucket", 0.0) + cash_cut
            ledger["bot_bucket"]      = ledger.get("bot_bucket", 0.0) + bot_cut
            ledger["trading_capital"] = ledger.get("trading_capital", 0.0) + bot_cut
            ledger["total_withdrawn"] = ledger.get("total_withdrawn", 0.0) + cash_cut
            save_ledger(ledger)

            send_alert(
                f"📝 {symbol} call ${best_call['strike']:.2f} {best_call['expiry']} | +${total:,.2f} premium"
            )
        except Exception as e:
            send_alert(f"*Covered call error* {symbol}: {e}")



# ── Cash secured puts ────────────────────────────────────────────────────────

def run_cash_secured_puts(encrypted: str, cash: float, account_value: float):
    if cash < 200:
        print("Not enough cash for cash secured puts.")
        return

    print("\n-- Cash secured puts | Cash available: $" + f"{cash:,.2f} --")

    from scanner import scan_best_stocks
    candidates = scan_best_stocks(cash, top_n=3)

    for stock in candidates:
        symbol = stock["symbol"]
        price  = stock["price"]

        if check_put_already_open(encrypted, symbol):
            print(f"  {symbol}: put already open")
            continue

        best_put = find_best_cash_secured_put(symbol, price, cash)
        if not best_put:
            print(f"  {symbol}: no good put found")
            continue

        try:
            put_resp = place_cash_secured_put(encrypted, best_put["option_symbol"], best_put["premium"])
            put_location = put_resp.headers.get("Location", "")

            if put_location:
                check = check_order_filled(encrypted, put_location)
                if not check["filled"] and check["status"] in ("REJECTED", "CANCELED"):
                    send_alert(f"❌ {symbol} put canceled — {check['description'][:50]}")
                    continue

            total    = best_put["total_premium"]
            etf_cut  = total * 0.60
            cash_cut = total * 0.30
            bot_cut  = total * 0.10

            from ledger import load_ledger, save_ledger
            ledger = load_ledger()
            ledger["profit_bucket"]   = ledger.get("profit_bucket", 0.0) + total
            ledger["etf_bucket"]      = ledger.get("etf_bucket", 0.0) + etf_cut
            ledger["cash_bucket"]     = ledger.get("cash_bucket", 0.0) + cash_cut
            ledger["bot_bucket"]      = ledger.get("bot_bucket", 0.0) + bot_cut
            ledger["trading_capital"] = ledger.get("trading_capital", 0.0) + bot_cut
            ledger["total_withdrawn"] = ledger.get("total_withdrawn", 0.0) + cash_cut
            save_ledger(ledger)

            send_alert(
                f"📝 {symbol} put ${best_put['strike']:.2f} {best_put['expiry']} | +${total:,.2f} premium"
            )
        except Exception as e:
            send_alert(f"*Put error* {symbol}: {e}")


# ── ETF sweep from ETF bucket ────────────────────────────────────────────────

def run_etf_sweep(encrypted: str):
    etf_bucket = get_etf_bucket()

    # Dynamic threshold — scan first to find cheapest ETF price
    # Only sweep when we can actually afford at least 1 share
    probe = scan_best_etfs(etf_bucket, top_n=1)
    if not probe:
        # No signals yet — use fallback minimum
        min_threshold = ETF_MIN_SWEEP
    else:
        # Threshold = price of best available ETF (need at least 1 share)
        min_threshold = probe[0]["price"]

    print(f"\n-- ETF bucket: ${etf_bucket:,.2f} | Dynamic threshold: ${min_threshold:,.2f} --")

    if etf_bucket < min_threshold:
        print("ETF bucket accumulating — not enough for 1 share yet.")
        return

    best_etfs = scan_best_etfs(etf_bucket, top_n=2)
    if not best_etfs:
        print("No ETF signals found.")
        return

    per_etf = etf_bucket / len(best_etfs)
    for etf in best_etfs:
        symbol   = etf["symbol"]
        price    = etf["price"]
        quantity = int(per_etf // price)
        if quantity < 1:
            continue
        try:
            place_equity_order(encrypted, symbol, quantity, "BUY")
            cost = quantity * price
            deduct_etf_bucket(cost)
            send_alert(f"📊 Bought {quantity} {symbol} @ ${price:.2f} from profits")
            print(f"Swept ${cost:,.2f} into {symbol}")
        except Exception as e:
            send_alert(f"*ETF sweep error* {symbol}: {e}")



# ── Dividend tracking ────────────────────────────────────────────────────────

def check_dividends(encrypted: str):
    """Check for new dividend payments and record them."""
    dividends = get_recent_dividends(encrypted, days_back=2)
    seen_ids = get_dividend_stats()["seen_dividend_ids"]

    for div in dividends:
        txn_id = div["transaction_id"]
        if txn_id in seen_ids:
            continue

        from scanner import get_etf_category, ETF_DIVIDEND_RULES
        cat          = get_etf_category(div["symbol"])
        rule         = ETF_DIVIDEND_RULES.get(cat, {"reinvest": 0.5, "cash": 0.5})
        reinvest_amt = div["amount"] * rule["reinvest"]
        cash_amt     = div["amount"] * rule["cash"]

        record_dividend(div["symbol"], div["amount"], div["reinvested"])
        mark_dividend_seen(txn_id)

        if reinvest_amt > 0 and cash_amt > 0:
            send_alert(
                f"*{div['symbol']} Dividend 💵*\n"
                f"Amount: ${div['amount']:,.2f}\n"
                f"→ ${reinvest_amt:,.2f} reinvested\n"
                f"→ ${cash_amt:,.2f} to your cash"
            )
        elif reinvest_amt > 0:
            send_alert(
                f"*{div['symbol']} Dividend 🔄*\n"
                f"${div['amount']:,.2f} reinvested"
            )
        else:
            send_alert(
                f"*{div['symbol']} Dividend 💵*\n"
                f"${div['amount']:,.2f} to your cash"
            )
        print(f"  Dividend: {div['symbol']} ${div['amount']:.2f} | reinvest=${reinvest_amt:.2f} cash=${cash_amt:.2f}")



# ── 24/7 Balance Monitor ─────────────────────────────────────────────────────

def get_full_balances(account: dict) -> dict:
    """Extract all balance fields from Schwab account."""
    try:
        b = account["securitiesAccount"]["currentBalances"]
        p = account["securitiesAccount"].get("projectedBalances", {})
        pos = account["securitiesAccount"].get("positions", [])

        # Options P&L from positions
        options_pl = sum(
            p.get("currentDayProfitLoss", 0)
            for p in pos
            if p.get("instrument", {}).get("assetType") == "OPTION"
        )

        return {
            "cash_balance":          b.get("cashBalance", 0.0),
            "cash_available_trade":  b.get("cashAvailableForTrading", b.get("cashBalance", 0.0)),
            "cash_available_withdraw": b.get("cashAvailableForWithdrawal", 0.0),
            "cash_on_hold":          b.get("cashEquityPut", 0.0),
            "settled_funds":         b.get("settledCashAvailableForTrading", 0.0),
            "options_value":         b.get("optionShortValue", 0.0),
            "options_pl":            options_pl,
            "total_securities":      b.get("longStockValue", 0.0),
            "account_value":         b.get("liquidationValue", 0.0),
        }
    except Exception:
        return {}


def get_schwab_transactions(encrypted: str, days_back: int = 1) -> list:
    """
    Pull real transaction history from Schwab API.
    Types: TRADE, CASH_IN_OR_CASH_OUT, DIVIDEND_OR_INTEREST, DIVIDEND_REINVESTMENT
    This is the source of truth — eliminates false deposit/withdrawal alerts.
    """
    from datetime import datetime, timedelta
    end   = datetime.utcnow()
    start = end - timedelta(days=days_back)
    try:
        resp = requests.get(
            f"{BASE_URL}/accounts/{encrypted}/transactions",
            headers=headers(),
            params={
                "startDate": start.strftime("%Y-%m-%dT00:00:00.000Z"),
                "endDate":   end.strftime("%Y-%m-%dT23:59:59.000Z"),
                "types":     "CASH_IN_OR_CASH_OUT"
            },
            timeout=15
        )
        if resp.ok:
            return resp.json() if isinstance(resp.json(), list) else []
        return []
    except Exception as e:
        print(f"Transaction fetch error: {e}")
        return []


def check_balance_24_7():
    """
    Runs every 5 minutes around the clock.
    Uses Schwab transaction history for accurate deposit/withdrawal detection.
    No more false alerts from put collateral changes.
    """
    try:
        from ledger import load_ledger, save_ledger
        accounts  = get_account_numbers()
        encrypted = accounts[0]["hashValue"]
        account   = get_account(encrypted)
        cash      = get_cash_balance(account)
        capital   = get_trading_capital()
        balances  = get_full_balances(account)

        ledger    = load_ledger()
        last_cash = ledger.get("last_known_cash", cash)

        print(f"Balance check — last: ${last_cash:.2f} | current: ${cash:.2f}")

        # Use Schwab transaction history for accurate deposit/withdrawal detection
        # This eliminates false alerts from put collateral changes
        seen_txn_ids = set(ledger.get("seen_cash_txn_ids", []))
        transactions = get_schwab_transactions(encrypted, days_back=1)

        for txn in transactions:
            txn_id = str(txn.get("activityId", txn.get("transactionId", "")))
            if txn_id in seen_txn_ids:
                continue

            amount    = txn.get("netAmount", 0.0)
            desc      = txn.get("description", "")
            seen_txn_ids.add(txn_id)

            if amount > 1:  # real deposit
                ledger["deposits"]        = ledger.get("deposits", 0.0) + amount
                ledger["trading_capital"] = ledger.get("trading_capital", 0.0) + amount
                send_alert(f"💵 +${amount:,.2f} deposited")
                print(f"Deposit detected via transactions: ${amount:.2f}")
            elif amount < -1:  # real withdrawal
                withdrawal = abs(amount)
                ledger.setdefault("withdrawal_history", []).append({
                    "amount":    withdrawal,
                    "timestamp": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
                })
                ledger["total_withdrawn"] = ledger.get("total_withdrawn", 0.0) + withdrawal
                send_alert(f"🏦 -${withdrawal:,.2f} withdrawn")
                print(f"Withdrawal detected via transactions: ${withdrawal:.2f}")

        ledger["seen_cash_txn_ids"] = list(seen_txn_ids)[-200:]  # keep last 200
        ledger["last_known_cash"]   = cash
        save_ledger(ledger)

        # Track options P&L and cash on hold — only notify on significant 24h changes
        if balances:
            import time as _t

            # Options P&L — only notify if changed more than $5 since last 24h snapshot
            curr_options_pl   = balances.get("options_pl", 0.0)
            last_options_pl   = ledger.get("last_options_pl", curr_options_pl)
            last_options_time = ledger.get("last_options_notify_time", 0)

            if abs(curr_options_pl - last_options_pl) > 5 and _t.time() - last_options_time > 86400:
                direction = "📈" if curr_options_pl > last_options_pl else "📉"
                send_alert(f"{direction} Options ${curr_options_pl:,.0f}")
                ledger["last_options_pl"] = curr_options_pl
                ledger["last_options_notify_time"] = _t.time()
                save_ledger(ledger)
            else:
                ledger["last_options_pl"] = curr_options_pl
                save_ledger(ledger)

            # Cash on hold — only notify when it actually changes (put placed or released)
            curr_hold = balances.get("cash_on_hold", 0.0)
            last_hold = ledger.get("last_cash_on_hold", curr_hold)
            if abs(curr_hold - last_hold) > 1:
                if curr_hold > last_hold:
                    send_alert(f"🔒 +${curr_hold - last_hold:,.0f} on hold")
                else:
                    send_alert(f"🔓 ${last_hold - curr_hold:,.0f} released")
                ledger["last_cash_on_hold"] = curr_hold
                save_ledger(ledger)

    except Exception as e:
        print(f"Balance check error: {e}")

# ── Main ─────────────────────────────────────────────────────────────────────

# Track last strategy run time to prevent duplicates
_last_strategy_run = 0

def run_strategy():
    global _last_strategy_run
    import time as _t
    # Prevent duplicate runs within 60 seconds
    if _t.time() - _last_strategy_run < 60:
        print("Strategy already ran recently — skipping duplicate")
        return
    if not is_market_open():
        return
    _last_strategy_run = _t.time()
    print("\n=== Strategy check ===")
    try:
        accounts      = get_account_numbers()
        encrypted     = accounts[0]["hashValue"]
        account       = get_account(encrypted)
        cash          = get_available_cash(account)  # trading cash excludes put collateral
        account_value = get_portfolio_value(account)
        positions     = get_positions(account)

        # Always sync ledger with Schwab — picks up portfolio after every update
        sync_ledger_from_schwab(encrypted)

        capital     = get_trading_capital()
        bucket      = get_profit_bucket()
        etf_bucket  = get_etf_bucket()
        cash_bucket = get_cash_bucket()
        t_name, t_cfg = get_tier(capital)
        tier        = t_cfg["label"]

        print(f"Account: ${account_value:,.2f} | Cash: ${cash:,.2f} | Capital: ${capital:,.2f} | Profit: ${bucket:,.2f} | ETF bucket: ${etf_bucket:,.2f} | {tier}")

        # Check token health every run
        check_token_health()

        check_dividends(encrypted)

        cash = run_stock_strategy(encrypted, positions, cash, account_value)
        run_options_strategy(encrypted, positions, account_value)
        run_cash_secured_puts(encrypted, cash, account_value)
        run_etf_sweep(encrypted)

        # Remind about cash payout if accumulated
        if cash_bucket > 20:
            send_alert(f"💵 ${cash_bucket:,.0f} profit cash ready to withdraw")

    except Exception as e:
        msg = f"*Bot error*: {e}"
        print(msg)
        send_alert(msg)


def main():
    try:
        accounts      = get_account_numbers()
        encrypted     = accounts[0]["hashValue"]
        account       = get_account(encrypted)
        cash          = get_cash_balance(account)
        account_value = get_portfolio_value(account)

        # Run balance check on startup using transaction API
        check_balance_24_7()

        print("Syncing ledger with Schwab account...")
        sync_ledger_from_schwab(encrypted)

        capital    = get_trading_capital()
        bucket     = get_profit_bucket()
        etf_bucket = get_etf_bucket()
        _tn, _tc   = get_tier(capital)
        tier       = _tc["label"]

        # Calculate last 24h profit from closed trades
        import time as _time
        from ledger import load_ledger as _ll
        _ledger = _ll()
        _now = _time.time()
        _24h_profit = sum(
            t.get("profit", 0)
            for t in _ledger.get("closed_trades", [])
            if _now - _time.mktime(_time.strptime(t.get("sold_at", "2000-01-01T00:00:00Z"), "%Y-%m-%dT%H:%M:%SZ")) < 86400
        )
        # Get cash on hold (put collateral)
        _balances = get_full_balances(account)
        _on_hold  = _balances.get("cash_on_hold", 0.0)
        _avail    = _balances.get("cash_available_trade", cash)

        # Only show market pulse during market hours
        _pulse = get_market_pulse() if is_market_open() else ""

        _cash_ready = get_cash_bucket()

        if _on_hold > 0:
            msg = f"✅ Bot online | 💵 ${_cash_ready:,.0f} ready | 🔒 ${_on_hold:,.0f} | 24h ${_24h_profit:,.0f}"
        else:
            msg = f"✅ Bot online | 💵 ${_cash_ready:,.0f} ready | 24h ${_24h_profit:,.0f}"

        if _pulse:
            msg += f"\n{_pulse}"
        send_alert(msg)
    except Exception as e:
        print(f"Startup error: {e}")

    if is_market_open():
        run_strategy()

    # Schedule next runs — first one fires after CHECK_INTERVAL minutes
    # so startup run and first scheduled run don't overlap
    schedule.every(CHECK_INTERVAL).minutes.do(run_strategy)
    schedule.every(5).minutes.do(check_balance_24_7)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
