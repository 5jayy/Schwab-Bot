import os
import json
import time
import base64
import webbrowser
import requests
from urllib.parse import urlparse, parse_qs, unquote
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.getenv("SCHWAB_CLIENT_ID")
CLIENT_SECRET = os.getenv("SCHWAB_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1")
import os as _os
TOKEN_FILE    = "/data/tokens.json" if _os.path.exists("/data") else "tokens.json"
AUTH_URL      = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL     = "https://api.schwabapi.com/v1/oauth/token"


def _basic_header():
    creds   = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded = base64.b64encode(creds.encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type":  "application/x-www-form-urlencoded"
    }


def save_tokens(tokens: dict):
    tokens["saved_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)


def load_tokens() -> dict | None:
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f)


def refresh_access_token() -> dict:
    tokens = load_tokens()
    if not tokens:
        raise RuntimeError("No tokens found. Run first_time_login() first.")
    resp = requests.post(TOKEN_URL, headers=_basic_header(), data={
        "grant_type":    "refresh_token",
        "refresh_token": tokens["refresh_token"],
    })
    resp.raise_for_status()
    new_tokens = resp.json()
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = tokens["refresh_token"]
    save_tokens(new_tokens)
    print("Access token refreshed.")
    return new_tokens


def get_valid_token() -> str:
    tokens = load_tokens()
    if not tokens:
        raise RuntimeError("No tokens found. Run python3 auth.py first.")
    saved_at   = tokens.get("saved_at", 0)
    expires_in = tokens.get("expires_in", 1800)
    if time.time() - saved_at > (expires_in - 300):
        tokens = refresh_access_token()
    return tokens["access_token"]


def first_time_login():
    auth_url = (
        f"{AUTH_URL}?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&scope=readonly%20trading"
        f"&redirect_uri={REDIRECT_URI}"
    )
    print("\n--- FIRST TIME LOGIN ---")
    print("1. Your browser will open to Schwab login")
    print("2. Log in and click Allow")
    print("3. Browser redirects to 127.0.0.1 — looks broken, that's fine")
    print("4. Copy the FULL URL from address bar and paste below IMMEDIATELY\n")
    input("Press Enter when you are ready and have your terminal visible...")
    webbrowser.open(auth_url)
    print("\nBrowser opened — log in, click Allow, then come straight back here.")
    redirected_url = input("Paste the full redirect URL here: ").strip()

    redirected_url = unquote(redirected_url)

    if "code=" not in redirected_url:
        raise ValueError("No auth code found. Make sure you copied the full URL.")

    parsed = urlparse(redirected_url)
    params = parse_qs(parsed.query)
    code   = params.get("code", [None])[0]

    if not code:
        raise ValueError("Could not extract auth code from URL.")

    print(f"\nGot auth code. Exchanging for tokens...")

    resp = requests.post(TOKEN_URL, headers=_basic_header(), data={
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": REDIRECT_URI,
    })

    if not resp.ok:
        print(f"\nError from Schwab: {resp.status_code}")
        print(resp.text)
        raise Exception("Token exchange failed. See error above.")

    tokens = resp.json()
    save_tokens(tokens)
    print("\nTokens saved to tokens.json")
    print("The bot will auto-refresh your access token every 25 minutes.")
    print("You only need to run this again if you see a token expired alert on Telegram.")
    return tokens


if __name__ == "__main__":
    first_time_login()
