"""
simplefin.py  --  Live, read-only bank balance via SimpleFIN Bridge
===================================================================

WHAT THIS REPLACES
------------------
The CSV download/shuffle. Instead of exporting a file and syncing it, this
pulls your current balance straight from your bank through SimpleFIN's
read-only feed. The planner doesn't change in spirit -- you're just swapping
the "Bank CSV" input box for a live feed behind the same loader boundary.

THE SECURITY MODEL (worth understanding before you use it)
----------------------------------------------------------
- You connect your bank ONCE, in a browser, at the SimpleFIN Bridge site.
  Your bank credentials go to their open-banking provider (MX), never to this
  script and never to SimpleFIN itself.
- The browser gives you a one-time SETUP TOKEN. You "claim" it once (below),
  which trades it for a long-lived ACCESS URL.
- That access URL is READ-ONLY and REVOCABLE. It can read balances and
  transactions and nothing else -- it cannot move money. If your machine is
  ever compromised, the blast radius is "someone read my transactions," and
  you can disable the token from the bridge in seconds.
- BUT the access URL is still a secret (it has credentials baked in). So:
  it is stored in a local file `.simplefin_token`, never hardcoded, and the
  claim step auto-adds it to .gitignore so it can't get pushed to GitHub.

ONE-TIME SETUP
--------------
  1. In a browser, connect your bank at the SimpleFIN Bridge and copy the
     setup token it gives you.
  2. Claim it (run once):
         python simplefin.py claim PASTE_SETUP_TOKEN_HERE
  3. Verify it works:
         python simplefin.py check

DAILY USE
---------
The planner calls load_balance_from_simplefin() for you. You can also peek:
         python simplefin.py balance

Standard library only. SimpleFIN expects <= 24 requests/day, so a once-daily
run sits well inside the limit.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

TOKEN_FILE = Path(__file__).parent / ".simplefin_token"
ENV_VAR = "SIMPLEFIN_ACCESS_URL"
USER_AGENT = "DebtFreePlanner/1.0 (+simplefin client)"


def _request(url: str, data: bytes | None = None, headers: dict | None = None,
             method: str | None = None) -> urllib.request.Request:
    """
    Build a Request that always sends a real User-Agent. Python's default
    ('Python-urllib/...') is a frequent trigger for 403 Forbidden from servers
    with bot/WAF protection, so we override it on every call.
    """
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        h.update(headers)
    return urllib.request.Request(url, data=data, headers=h, method=method)


# ----------------------------------------------------------------------------
# TOKEN STORAGE  (secrets handling)
# ----------------------------------------------------------------------------

def get_access_url() -> str | None:
    """
    Find the access URL, preferring an environment variable over the file.
    Returns None if neither is set (so callers can give a helpful message).
    """
    env = os.environ.get(ENV_VAR)
    if env:
        return env.strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    return None


def _save_access_url(access_url: str) -> None:
    """Write the access URL to a local file and lock it down as best we can."""
    TOKEN_FILE.write_text(access_url.strip() + "\n")
    try:
        os.chmod(TOKEN_FILE, 0o600)   # owner read/write only (no-op-ish on Windows)
    except OSError:
        pass
    _ensure_gitignored(TOKEN_FILE.name)


def _ensure_gitignored(name: str) -> None:
    """Make sure the secret file is listed in .gitignore so it never gets committed."""
    gi = Path(__file__).parent / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    if name not in lines:
        with gi.open("a") as f:
            f.write(name + "\n")


# ----------------------------------------------------------------------------
# THE CLAIM STEP  (run once)
# ----------------------------------------------------------------------------

def _decode_setup_token(token: str) -> str:
    """
    Turn a setup token back into the /claim URL it encodes.

    Tokens get mangled in transit: stray whitespace from copy-paste, missing
    '=' padding, or url-safe base64 (- and _ instead of + and /). We strip
    whitespace, restore padding, and try both alphabets. A real token always
    decodes to an http(s) URL -- if we don't get one, we raise a message that
    says what to check instead of a cryptic decode error.
    """
    s = "".join(token.split())          # drop any spaces/newlines from pasting
    s += "=" * (-len(s) % 4)            # restore base64 padding if it was lost
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            url = decoder(s).decode("utf-8").strip()
        except Exception:
            continue
        if url.startswith("http"):
            return url
    raise ValueError(
        "That doesn't look like a valid SimpleFIN setup token.\n"
        "  - Make sure you copied the ENTIRE token (they're long; nothing cut off).\n"
        "  - Make sure it's the SETUP token from the Bridge, not the access URL.\n"
        "  - Try the interactive paste: run 'python simplefin.py claim' with no token."
    )


def claim(setup_token: str) -> str:
    """
    Trade a one-time setup token for a long-lived access URL.

    The setup token is a base64-encoded URL pointing at a /claim endpoint.
    We decode it, POST to it once, and the response body IS the access URL.
    """
    claim_url = _decode_setup_token(setup_token)
    req = _request(claim_url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            access_url = resp.read().decode().strip()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise SystemExit(
                "Claim refused (HTTP 403). Two likely causes:\n"
                "  1. This setup token was already claimed (they are ONE-TIME) or\n"
                "     has expired. Generate a FRESH token at the Bridge and retry.\n"
                "  2. The server was blocking the request client. This version now\n"
                "     sends a normal User-Agent, which usually clears that -- so try\n"
                "     this same token once more first before regenerating."
            ) from None
        raise
    if not access_url.startswith("http"):
        raise ValueError(f"Unexpected claim response: {access_url[:80]!r}")
    _save_access_url(access_url)
    return access_url


# ----------------------------------------------------------------------------
# FETCHING  (the network call) + parsing (pure, so it's testable offline)
# ----------------------------------------------------------------------------

def _prepare(access_url: str) -> tuple[str, dict]:
    """
    The access URL embeds credentials like  https://USER:PASS@host/simplefin .
    urllib won't use inline credentials, so split them out: rebuild the URL
    without the userinfo, and turn USER:PASS into a Basic auth header.
    """
    parts = urlsplit(access_url)
    user, pwd = parts.username or "", parts.password or ""
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    base = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return base, {"Authorization": f"Basic {auth}"}


def fetch_accounts(access_url: str, params: str = "balances-only=1") -> dict:
    """
    GET the account set from SimpleFIN. 'balances-only=1' skips transaction
    history, since the planner only needs the current balance -- smaller and
    faster. Returns the parsed JSON.
    """
    base, headers = _prepare(access_url)
    url = base.rstrip("/") + "/accounts"
    if params:
        url += "?" + params
    req = _request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _pick_balance(data: dict, account_id: str | None) -> float:
    """
    Pure function: given a SimpleFIN account set, return one balance.
    - If account_id is given, return that account's balance.
    - If there's exactly one account, return it.
    - Otherwise raise, listing the accounts so you can choose one.
    Kept separate from the network call so it can be unit-tested offline.
    """
    for err in data.get("errors", []) or []:
        print(f"  ! SimpleFIN warning: {err}", file=sys.stderr)

    accounts = data.get("accounts", [])
    if not accounts:
        raise ValueError("SimpleFIN returned no accounts.")

    if account_id:
        for a in accounts:
            if a.get("id") == account_id:
                return float(a["balance"])
        raise ValueError(f"No account with id {account_id!r}.")

    if len(accounts) == 1:
        return float(accounts[0]["balance"])

    listing = "\n".join(
        f"    {a.get('id')}  --  {a.get('name')}  ({a.get('balance')})"
        for a in accounts
    )
    raise ValueError(
        "Multiple accounts found; set 'simplefin_account_id' in config to one of:\n"
        + listing
    )


def load_balance_from_simplefin(access_url: str, account_id: str | None = None) -> float:
    """The function the planner calls: fetch + pick, returns a float balance."""
    return _pick_balance(fetch_accounts(access_url), account_id)


# ----------------------------------------------------------------------------
# CLI  (claim / check / balance)
# ----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="SimpleFIN read-only balance helper.")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("claim", help="trade a setup token for an access URL (run once)")
    c.add_argument("setup_token", nargs="?", default=None,
                   help="the token from the SimpleFIN Bridge (omit to paste at a prompt)")
    sub.add_parser("check", help="list connected accounts and balances")
    sub.add_parser("balance", help="print the balance the planner would use")
    args = parser.parse_args()

    if args.command == "claim":
        token = args.setup_token or input("Paste your SimpleFIN setup token: ").strip()
        claim(token)
        print(f"Claimed. Access URL saved to {TOKEN_FILE.name} (and gitignored).")
        print("Run 'python simplefin.py check' to confirm it works.")
        return

    access_url = get_access_url()
    if not access_url:
        print(f"No access URL found. Set ${ENV_VAR} or run 'claim' first.",
              file=sys.stderr)
        sys.exit(1)

    if args.command == "check":
        data = fetch_accounts(access_url)
        for a in data.get("accounts", []):
            print(f"  {a.get('id')}  {a.get('name'):<20} {a.get('balance')} "
                  f"{a.get('currency', '')}")
    elif args.command == "balance":
        print(load_balance_from_simplefin(access_url))


if __name__ == "__main__":
    main()