# Schwab Trading Bot

Automatically buys dividend ETFs when your cash balance exceeds a set threshold.
Sends all activity to Telegram.

## Setup

### 1. Install dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install schedule
```

### 2. Configure secrets
```bash
cp .env.example .env
```
Edit `.env` and fill in your values:
- `SCHWAB_CLIENT_ID` — from developer.schwab.com
- `SCHWAB_CLIENT_SECRET` — from developer.schwab.com
- `TELEGRAM_TOKEN` — from @BotFather on Telegram
- `TELEGRAM_CHAT_ID` — your Telegram chat ID
- `TARGET_ETFS` — comma-separated ETF symbols e.g. `SCHD,JEPI`
- `CASH_THRESHOLD` — buy ETF when cash exceeds this amount
- `ETF_BUY_AMOUNT` — total dollars to spend per buy cycle

### 3. First time login (run once)
```bash
python3 auth.py
```
This opens your browser, you log into Schwab, paste the redirect URL back.
Tokens are saved to `tokens.json` (never committed to git).

### 4. Test Telegram
```bash
python3 telegram.py
```
You should receive "Schwab bot is online and connected!" in Telegram.

### 5. Run the bot
```bash
python3 bot.py
```

## Deploy to Fly.io

```bash
# Install flyctl
brew install flyctl

# Login
flyctl auth login

# Launch app (first time)
flyctl launch

# Set secrets
flyctl secrets set \
  SCHWAB_CLIENT_ID=xxx \
  SCHWAB_CLIENT_SECRET=xxx \
  TELEGRAM_TOKEN=xxx \
  TELEGRAM_CHAT_ID=xxx \
  TARGET_ETFS=SCHD,JEPI \
  CASH_THRESHOLD=1000 \
  ETF_BUY_AMOUNT=500

# Deploy
flyctl deploy
```

## Files
- `auth.py` — OAuth2 login and token auto-refresh
- `bot.py` — main trading strategy and scheduler
- `telegram.py` — Telegram alert helper
- `tokens.json` — saved locally only, never committed
- `.env` — secrets, never committed
