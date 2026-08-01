# Google credentials (local only — never commit)

Bernie reads OAuth files from this folder via the Docker bind-mount `./credentials:/credentials`.

| File | Created by | Purpose |
|------|------------|---------|
| `credentials.json` | **You** — download from Google Cloud Console | OAuth client (Desktop app). Same file for Calendar + Gmail. |
| `token.json` | `python scripts/auth_google.py` | Calendar access (sign in as a parent / calendar owner). |
| `gmail_token.json` | `python scripts/auth_gmail.py` | Bernie's mailbox — send + read (optional). |

## First-time setup

1. Copy the example shape (optional reference only):
   ```bash
   cp credentials/credentials.json.example credentials/credentials.json
   ```
2. Follow **[docs/google-oauth.md](../docs/google-oauth.md)** to create a real `credentials.json` in Google Cloud Console.
3. On the **host** (not in Docker), install auth deps once:
   ```bash
   pip install --user google-auth-oauthlib google-api-python-client
   ```
4. Run the auth scripts from the repo root:
   ```bash
   python scripts/auth_google.py    # Calendar → token.json
   python scripts/auth_gmail.py     # Optional mailbox → gmail_token.json
   ```

Tokens auto-refresh at runtime. Re-run a script only if Google revokes access or you change API scopes.

## Your app, your tokens

Each household creates **their own** Google Cloud OAuth client. You are **not** using the maintainer's project or tokens — only the same helper scripts and folder layout.
