#!/usr/bin/env python3
"""One-time Gmail OAuth on the host → credentials/gmail_token.json (optional)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from google_auth_paths import client_secrets_file, credentials_dir, gmail_token_file

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
# Host-local only — default 8086 so it does not clash with Calendar (8085).
PORT = int(os.environ.get("GMAIL_OAUTH_PORT", os.environ.get("PORT", "8086")))


def main() -> None:
    creds_path = client_secrets_file()
    token_path = gmail_token_file()
    if not creds_path.exists():
        print(f"ERROR: OAuth client file not found: {creds_path}")
        print("See docs/google-oauth.md — same credentials.json as Calendar auth.")
        sys.exit(1)

    credentials_dir().mkdir(parents=True, exist_ok=True)

    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    print(f"Gmail auth — open http://localhost:{PORT} in your browser…")
    print("Sign in as Bernie's dedicated mailbox (not your personal calendar account).")
    print(f"  (override with GMAIL_OAUTH_PORT=… if this port is busy)")
    print(f"  client: {creds_path}")
    print(f"  token:  {token_path}")
    creds = flow.run_local_server(
        port=PORT,
        open_browser=False,
        success_message="Done! Close this tab and return to the terminal.",
    )

    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"\n✅ Saved {token_path}\n")

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    print(f"📧 Authed as: {profile['emailAddress']}")
    print("Add family addresses in config.json → family_members.*.email for send policy.")


if __name__ == "__main__":
    main()
