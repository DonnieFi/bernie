#!/usr/bin/env python3
"""One-time Google Calendar OAuth on the host → credentials/token.json."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `python scripts/auth_google.py` from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from google_auth_paths import calendar_token_file, client_secrets_file, credentials_dir

SCOPES = ["https://www.googleapis.com/auth/calendar"]
# Host-local only — override if 8085 is already taken on your machine.
PORT = int(os.environ.get("GOOGLE_OAUTH_PORT", os.environ.get("PORT", "8085")))


def main() -> None:
    creds_path = client_secrets_file()
    token_path = calendar_token_file()
    if not creds_path.exists():
        print(f"ERROR: OAuth client file not found: {creds_path}")
        print("See docs/google-oauth.md — download Desktop app JSON from Google Cloud Console.")
        sys.exit(1)

    credentials_dir().mkdir(parents=True, exist_ok=True)

    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    print(f"Calendar auth — open http://localhost:{PORT} in your browser…")
    print(f"  (override with GOOGLE_OAUTH_PORT=… if this port is busy)")
    print(f"  client: {creds_path}")
    print(f"  token:  {token_path}")
    creds = flow.run_local_server(
        port=PORT,
        open_browser=False,
        success_message="Done! Close this tab and return to the terminal.",
    )

    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"\n✅ Saved {token_path}\n")

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    calendars = service.calendarList().list().execute()
    print("📅 Calendars — copy IDs into config.json → shared_calendars / family_members:\n")
    for cal in calendars.get("items", []):
        print(f"  {cal['summary']:<40} {cal['id']}")
    print("\nNext: paste calendar IDs into config.json, then docker compose up.")


if __name__ == "__main__":
    main()
