import os
import json
import time
import base64
import requests
from telegram import send_alert
from dotenv import load_dotenv

load_dotenv()

TOKEN_FILE    = "tokens.json"
TOKEN_URL     = "https://api.schwabapi.com/v1/oauth/token"
CLIENT_ID     = os.getenv("SCHWAB_CLIENT_ID")
CLIENT_SECRET = os.getenv("SCHWAB_CLIENT_SECRET")

# Token expires in 30 min (access) and 7 days (refresh)
ACCESS_EXPIRE_SECONDS  = 1800        # 30 minutes
REFRESH_EXPIRE_SECONDS = 7 * 24 * 3600  # 7 days
REFRESH_WARN_SECONDS   = 6 * 24 * 3600  # warn at 6 days


def _basic_header():
    creds   = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded = base64.b64encode(creds.encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type":  "application/x-www-form-urlencoded"
    }


def load_tokens() -> dict | None:
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f)


def save_tokens(tokens: dict):
    tokens["saved_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)


def get_token_age() -> float:
    """Returns age of saved token in seconds."""
    tokens = load_tokens()
    if not tokens:
        return float("inf")
    return time.time() - tokens.get("saved_at", 0)


def refresh_access_token() -> bool:
    """
    Refreshes the access token using the refresh token.
    Returns True if successful, False if refresh token also expired.
    """
    tokens = load_tokens()
    if not tokens or "refresh_token" not in tokens:
        return False

    try:
        resp = requests.post(TOKEN_URL, headers=_basic_header(), data={
            "grant_type":    "refresh_token",
            "refresh_token": tokens["refresh_token"],
        }, timeout=15)

        if resp.status_code == 200:
            new_tokens = resp.json()
            if "refresh_token" not in new_tokens:
                new_tokens["refresh_token"] = tokens["refresh_token"]
            save_tokens(new_tokens)
            print("Access token refreshed successfully.")
            return True
        else:
            print(f"Token refresh failed: {resp.status_code} {resp.text}")
            return False

    except Exception as e:
        print(f"Token refresh error: {e}")
        return False


def check_token_health():
    """
    Runs on every strategy check.
    Auto-refreshes access token when needed.
    Warns via Telegram when refresh token is about to expire.
    """
    tokens = load_tokens()
    if not tokens:
        send_alert(
            "⚠️ *No tokens found!*\n"
            "Run on your Mac:\n"
            "`python3 auth.py`\n"
            "Bot cannot trade without valid tokens."
        )
        return

    saved_at   = tokens.get("saved_at", 0)
    age        = time.time() - saved_at
    expires_in = tokens.get("expires_in", ACCESS_EXPIRE_SECONDS)

    # Auto-refresh access token if within 5 minutes of expiry
    if age > (expires_in - 300):
        print("Access token near expiry — refreshing...")
        success = refresh_access_token()
        if not success:
            # Refresh token likely expired — need manual re-login
            send_alert(
                "⚠️ *Token Expired — Action Required!*\n\n"
                "Your Schwab login has expired (7 day limit).\n\n"
                "On your Mac run:\n"
                "`cd '/Users/jay/Desktop/schwab bot'`\n"
                "`source venv/bin/activate`\n"
                "`python3 auth.py`\n\n"
                "Then run:\n"
                "`flyctl deploy`\n\n"
                "Bot is paused until re-authenticated."
            )
            return

    # Warn at 6 days — refresh token approaching expiry
    if age > REFRESH_WARN_SECONDS:
        days_left = (REFRESH_EXPIRE_SECONDS - age) / 3600 / 24
        send_alert(
            f"⚠️ *Token Expiring Soon*\n"
            f"Your Schwab token expires in {days_left:.1f} days.\n\n"
            f"On your Mac run:\n"
            f"`python3 auth.py`\n"
            f"Then: `flyctl deploy`\n\n"
            f"Do this before Sunday to avoid interruption."
        )

    # Also try to proactively refresh every 25 minutes
    if age > (expires_in - 300):
        refresh_access_token()


if __name__ == "__main__":
    print(f"Token age: {get_token_age() / 3600:.1f} hours")
    check_token_health()
