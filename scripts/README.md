# Scripts

Helpers you run **on your computer** (or CI). The live bot is **`bot/`** — Docker starts that, not this folder.

## Beginners — you only need these

| Script | When |
|--------|------|
| [`auth_google.py`](auth_google.py) | One-time Google Calendar login → `credentials/token.json` |
| [`auth_gmail.py`](auth_gmail.py) | Optional one-time Gmail login for Bernie’s mailbox |

```bash
# From the repo root
python scripts/auth_google.py
# Port busy?  GOOGLE_OAUTH_PORT=9090 python scripts/auth_google.py
```

## If you’re developing Bernie

| Script | When |
|--------|------|
| [`run_container_unittest.sh`](run_container_unittest.sh) | Run unit tests in the `bernie-api` container (uses a lock so agents don’t pile on) |
| [`run_mapped_suite.sh`](run_mapped_suite.sh) | Full mapped test gate |

## Everything else

Other files here are **operator / diagnostic** helpers. You do **not** need them to install or use Bernie day to day. Safe to ignore.
