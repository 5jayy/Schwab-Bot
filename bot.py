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
from scanner import scan_best_stocks, scan_best_etfs, get_market_pulse, get_tier, get_etf_level, ETF_DIVIDEND_RULES, get_etf_category
from options import find_best_covered_call, place_covered_call, check_covered_call_already_open, find_best_cash_secured_put, place_cash_secured_put, check_put_already_open
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
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", 0.07))
ETF_PCT           = float(os.getenv("ETF_PCT", 0.60))
CASH_PCT          = float(os.getenv("CASH_PCT", 0.30))
BOT_PCT           = float(os.getenv("BOT_PCT", 0.10))
CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL_MINUTES", 30))


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
    resp = requests.get(f"{BASE_URL}/accounts/{encrypted}", headers=headers(), params={"fields": "positions"})
    resp.raise_for_status()
    return resp.json()


def get_cash_balance(account: dict) -> float:
    """Total cash — used for deposit/withdrawal tracking."""
    try:
        return max(account["securitiesAccount"]["currentBalances"].get("cashBalance", 0.0), 0.0)
    except KeyError:
        return 0.0


def get_available_cash(account: dict) -> float:
    """Cash available for trading — excludes put collateral."""
    try:
        b = account["securitiesAccount"]["currentBalances"]
        avail = b.get("cashAvailableForTrading")
        return max(avail if avail is not None else b.get("cashBalance", 0.0), 0.0)
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


# ── Market hours (Schwab API — knows holidays/half days) ─────────────────────

def is_market_open() -> bool:
    try:
        from datetime import date as _d
        resp = requests.get(
            "https://api.schwabapi.com/marketdata/v1/markets",
            headers=headers(),
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
        return {"status": d.get("status", "UNKNOWN"), "description": d.get("statusDescription", ""), "filled": d.get("status") == "FILLED"}
    except Exception as ex:
        return {"status": "UNKNOWN", "description": str(ex), "filled": False}


# ── Win rate tracker ──────────────────────────────────────────────────────────

def update_win_rate(profit: float):
    """Track rolling 10-trade win rate. Tightens filters if below 40%."""
    ledger = load_ledger()
    history = ledger.get("win_rate_history", [])
    history.append(1 if profit > 0 else 0)
    ledger["win_rate_history"] = history[-10:]  # rolling 10
    win_rate = sum(ledger["win_rate_history"]) / len(ledger["win_rate_history"])
    ledger["current_win_rate"] = win_rate
    save_ledger(ledger)
    return win_rate


def get_win_rate() -> float:
    ledger = load_ledger()
    history = ledger.get("win_rate_history", [])
    if not history:
        return 1.0  # assume good until data exists
    return sum(history) / len(history)


# ── Can trade gatekeeper ──────────────────────────────────────────────────────

def can_trade(capital: float, trades_today: int, consecutive_losses: int) -> tuple:
    """
    Dynamic gatekeeper — checks all conditions before allowing a buy.
    Returns (bool, reason_string).
    All thresholds dynamic based on capital and tier.
    """
    tier_name, tier_cfg = get_tier(capital)

    # Warmup — skip first 15 min after open (9:30-9:45 ET)
    et  = pytz.timezone("America/New_York")
    now = datetime.now(et)
    open_time = now.replace(hour=9, minute=30, second=0)
    warmup_end = now.replace(hour=9, minute=45, second=0)
    if open_time <= now <= warmup_end:
        return False, "warmup"

    # Max trades per day — dynamic per tier
    max_trades = int(tier_cfg.get("max_trades", 5))
    if trades_today >= max_trades:
        return False, f"max_trades_{max_trades}"

    # Cooldown — 2 consecutive losses → pause 60 min
    if consecutive_losses >= 2:
        return False, "cooldown_2_losses"

    # Win rate — if below 40% tighten to only best signals
    win_rate = get_win_rate()
    if win_rate < 0.40:
        return False, f"win_rate_low_{win_rate:.0%}"

    # Daily loss limit — dynamic: 2% of bot capital
    ledger = load_ledger()
    daily_loss = ledger.get("daily_loss", 0.0)
    daily_limit = capital * 0.02
    if daily_loss >= daily_limit:
        return False, f"daily_loss_limit_${daily_limit:.0f}"

    # Daily profit cap — once up 3% lock the day
    daily_profit = ledger.get("daily_profit", 0.0)
    daily_cap = capital * 0.03
    if daily_profit >= daily_cap:
        return False, f"daily_profit_cap_${daily_cap:.0f}"

    return True, "ok"


def get_daily_stats() -> dict:
    ledger = load_ledger()
    today  = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    if ledger.get("daily_stats_date") != today:
        ledger["daily_stats_date"]    = today
        ledger["daily_loss"]          = 0.0
        ledger["daily_profit"]        = 0.0
        ledger["trades_today"]        = 0
        ledger["consecutive_losses"]  = 0
        save_ledger(ledger)
    return {
        "trades_today":       ledger.get("trades_today", 0),
        "consecutive_losses": ledger.get("consecutive_losses", 0),
        "daily_loss":         ledger.get("daily_loss", 0.0),
        "daily_profit":       ledger.get("daily_profit", 0.0),
    }


def record_trade_result(profit: float):
    """Update daily stats and win rate after every trade."""
    ledger = load_ledger()
    ledger["trades_today"] = ledger.get("trades_today", 0) + 1
    if profit > 0:
        ledger["daily_profit"]       = ledger.get("daily_profit", 0.0) + profit
        ledger["consecutive_losses"] = 0
    else:
        ledger["daily_loss"]         = ledger.get("daily_loss", 0.0) + abs(profit)
        ledger["consecutive_losses"] = ledger.get("consecutive_losses", 0) + 1
    save_ledger(ledger)
    return update_win_rate(profit)


# ── Execute sell ──────────────────────────────────────────────────────────────

def execute_sell(encrypted: str, symbol: str, quantity: int, price: float, cash: float, reason: str = "signal") -> float:
    try:
        place_equity_order(encrypted, symbol, quantity, "SELL")
        proceeds = quantity * price
        split    = record_sell_and_split(symbol, quantity, price, proceeds, ETF_PCT, CASH_PCT, BOT_PCT)
        cash    += proceeds
        profit   = split["profit"]
        tag      = "🛑 Stop" if "stop" in reason or "breakeven" in reason else "Signal"

        record_trade_result(profit)

        if profit > 0:
            send_alert(
                f"💰 Sold {symbol} x{quantity} @ ${price:.2f} | +${profit:,.2f}\n"
                f"→ ETF ${split['etf_cut']:,.0f} | Cash ${split['cash_cut']:,.0f} | Bot ${split['bot_cut']:,.0f}"
            )
        else:
            send_alert(f"📉 Sold {symbol} x{quantity} @ ${price:.2f} | ${profit:,.2f} | {tag}")
        print(f"  Sold {quantity} {symbol} @ ${price:.2f} | P&L ${profit:+,.2f} | {reason}")
    except Exception as ex:
        send_alert(f"Sell error {symbol}: {ex}")
    return cash


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

        capital     = get_trading_capital()
        tier_name, tier_cfg = get_tier(capital)
        daily_stats = get_daily_stats()

        print(f"Account: ${account_value:,.2f} | Cash: ${cash:,.2f} | Capital: ${capital:,.2f} | {tier_name}")

        # ── Dividends ──
        check_dividends(encrypted)

        # ── Sell signals + dynamic stops ──
        sold_this_run = set()
        for pos in positions:
            sym = pos["instrument"]["symbol"]
            if sym not in BOT_STOCKS or sym in sold_this_run:
                continue

            price    = pos.get("marketValue", 0) / max(pos.get("longQuantity", 1), 1)
            quantity = int(pos.get("longQuantity", 0))
            if quantity < 1 or price <= 0:
                continue

            update_high_price(sym, price)
            trail_info = get_trailing_stop_info(sym)
            trigger    = None

            # Get live signal
            try:
                from strategy import get_signal
                sig = get_signal(sym)
                if sig.get("signal") == "SELL":
                    trigger = "signal"
                    price   = sig.get("price", price)
            except Exception:
                pass

            if not trigger and trail_info:
                stop_info = get_dynamic_stop(trail_info["buy_price"], trail_info["high_price"], price, TRAILING_STOP_PCT)
                if price <= stop_info["stop_price"]:
                    trigger = stop_info["reason"]

            if trigger:
                if check_covered_call_already_open(encrypted, sym):
                    print(f"  {sym}: skip sell — covered call open")
                    continue
                sold_this_run.add(sym)
                cash = execute_sell(encrypted, sym, quantity, price, cash, trigger)

        # ── Buy signals ──
        if cash >= 10:
            ok, reason = can_trade(capital, daily_stats["trades_today"], daily_stats["consecutive_losses"])
            if not ok:
                print(f"  Buying paused — {reason}")
            else:
                base_size = cash * tier_cfg["pos_pct"]
                top_stocks = scan_best_stocks(cash, bot_capital=capital)
                bought_this_run = set()

                for stock in top_stocks:
                    symbol = stock["symbol"]
                    price  = stock["price"]
                    score  = stock["score"]
                    adx_val = stock.get("adx") or 0

                    if symbol in bought_this_run or get_position_for(positions, symbol):
                        continue
                    if score < max(tier_cfg.get("min_score", 35), 6):
                        continue
                    if cash < price:
                        continue

                    # Dynamic quantity — scales with ADX strength, score quality, win rate
                    adx_mult  = 1.0 if adx_val >= 25 else 0.75 if adx_val >= 20 else 0.5
                    score_mult = 1.0 if score >= 60 else 0.75 if score >= 40 else 0.5
                    wr        = get_win_rate()
                    wr_mult   = 1.1 if wr >= 0.70 else 0.5 if wr < 0.40 else 1.0

                    position_size = base_size * adx_mult * score_mult * wr_mult

                    # Skip if adjusted size too small to buy even 1 share
                    if position_size < price:
                        print(f"  {symbol}: position too small after quality adjustment — skip")
                        continue

                    quantity = int(position_size // price)
                    if quantity < 1:
                        continue

                    try:
                        resp     = place_equity_order(encrypted, symbol, quantity, "BUY")
                        location = resp.headers.get("Location", "")
                        cost     = quantity * price

                        if location:
                            check = check_order_filled(encrypted, location)
                            if not check["filled"] and check["status"] in ("REJECTED", "CANCELED"):
                                send_alert(f"❌ {symbol} canceled — {check['description'][:50]}")
                                bought_this_run.add(symbol)
                                continue

                        cash -= cost
                        bought_this_run.add(symbol)
                        record_buy(symbol, quantity, price, cost)
                        send_alert(f"📈 Bought {symbol} x{quantity} @ ${price:.2f}")
                        print(f"  Bought {quantity} {symbol} @ ${price:.2f}")
                    except Exception as ex:
                        send_alert(f"Buy error {symbol}: {ex}")
        else:
            print("Not enough cash — monitoring only.")

        # ── Options ──
        run_options(encrypted, positions, cash)

        # ── ETF sweep ──
        run_etf_sweep(encrypted)

        # ── Cash ready reminder — once per day only ──
        cash_bucket = get_cash_bucket()
        if cash_bucket > 20:
            import time as _t2
            ledger2 = load_ledger()
            last_reminder = ledger2.get("last_cash_reminder_time", 0)
            last_amount   = ledger2.get("last_cash_reminder_amount", 0)
            # Only notify if amount changed OR 24 hours passed
            if abs(cash_bucket - last_amount) > 5 or _t2.time() - last_reminder > 86400:
                send_alert(f"💵 ${cash_bucket:,.0f} profit cash ready to withdraw")
                ledger2["last_cash_reminder_time"]   = _t2.time()
                ledger2["last_cash_reminder_amount"] = cash_bucket
                save_ledger(ledger2)

    except Exception as ex:
        msg = f"Bot error: {ex}"
        print(msg)
        send_alert(msg)


# ── Options (covered calls + puts) ───────────────────────────────────────────

def run_options(encrypted: str, positions: list, cash: float):
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
            resp = place_covered_call(encrypted, call["option_symbol"], call["contracts"], call["premium"])
            loc  = resp.headers.get("Location", "")
            if loc:
                chk = check_order_filled(encrypted, loc)
                if not chk["filled"] and chk["status"] in ("REJECTED", "CANCELED"):
                    send_alert(f"❌ {sym} call canceled — {chk['description'][:50]}")
                    continue
            total = call["total_premium"]
            _record_options_profit(total)
            send_alert(f"📝 {sym} call ${call['strike']:.2f} {call['expiry']} | +${total:,.2f}")
        except Exception as ex:
            print(f"  Call error {sym}: {ex}")

    # Cash secured puts
    if cash < 200:
        return
    print(f"\n-- Cash secured puts | ${cash:,.2f} --")
    capital   = get_trading_capital()
    candidates = scan_best_stocks(cash, bot_capital=capital)
    for stock in candidates:
        sym   = stock["symbol"]
        price = stock["price"]
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
            _record_options_profit(total)
            send_alert(f"📝 {sym} put ${put['strike']:.2f} {put['expiry']} | +${total:,.2f}")
        except Exception as ex:
            print(f"  Put error {sym}: {ex}")


def _record_options_profit(total: float):
    ledger = load_ledger()
    ledger["profit_bucket"]   = ledger.get("profit_bucket", 0.0) + total
    ledger["etf_bucket"]      = ledger.get("etf_bucket", 0.0) + total * ETF_PCT
    ledger["cash_bucket"]     = ledger.get("cash_bucket", 0.0) + total * CASH_PCT
    ledger["bot_bucket"]      = ledger.get("bot_bucket", 0.0) + total * BOT_PCT
    ledger["trading_capital"] = ledger.get("trading_capital", 0.0) + total * BOT_PCT
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
    for div in dividends:
        if div["transaction_id"] in seen_ids:
            continue
        cat  = get_etf_category(div["symbol"])
        rule = ETF_DIVIDEND_RULES.get(cat, {"reinvest": 0.5, "cash": 0.5})
        r_amt = div["amount"] * rule["reinvest"]
        c_amt = div["amount"] * rule["cash"]
        record_dividend(div["symbol"], div["amount"], div["reinvested"])
        mark_dividend_seen(div["transaction_id"])
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
                    "types":     "CASH_IN_OR_CASH_OUT"},
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

        ledger       = load_ledger()
        seen_ids     = set(ledger.get("seen_cash_txn_ids", []))
        transactions = get_schwab_transactions(encrypted, days_back=1)

        for txn in transactions:
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
                ledger.setdefault("withdrawal_history", []).append({"amount": w, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                ledger["total_withdrawn"] = ledger.get("total_withdrawn", 0.0) + w
                send_alert(f"🏦 -${w:,.2f} withdrawn")

        ledger["seen_cash_txn_ids"] = list(seen_ids)[-200:]
        ledger["last_known_cash"]   = cash

        # Cash on hold changes
        last_hold = ledger.get("last_cash_on_hold", on_hold)
        if abs(on_hold - last_hold) > 1:
            send_alert(f"🔒 +${on_hold - last_hold:,.0f} on hold" if on_hold > last_hold else f"🔓 ${last_hold - on_hold:,.0f} released")
        ledger["last_cash_on_hold"] = on_hold

        # Options P&L daily change
        positions = account["securitiesAccount"].get("positions", [])
        opts_pl   = sum(p.get("currentDayProfitLoss", 0) for p in positions if p.get("instrument", {}).get("assetType") == "OPTION")
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
    # Balance check first — catches overnight deposits/withdrawals
    check_balance_24_7()

    try:
        accounts      = get_account_numbers()
        encrypted     = accounts[0]["hashValue"]
        account       = get_account(encrypted)
        cash          = get_cash_balance(account)
        account_value = get_portfolio_value(account)

        sync_ledger_from_schwab(encrypted)

        capital    = get_trading_capital()
        cash_ready = get_cash_bucket()
        etf_bucket = get_etf_bucket()
        on_hold    = get_cash_on_hold(account)
        tier_name, _ = get_tier(capital)

        # 24h profit
        ledger  = load_ledger()
        now_ts  = time.time()
        p24h    = sum(t.get("profit", 0) for t in ledger.get("closed_trades", [])
                      if now_ts - time.mktime(time.strptime(t.get("sold_at", "2000-01-01T00:00:00Z"), "%Y-%m-%dT%H:%M:%SZ")) < 86400)

        pulse = get_market_pulse() if is_market_open() else ""
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

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
