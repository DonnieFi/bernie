# Google OAuth setup (Calendar + optional Gmail)

Bernie uses **your** Google Cloud project. Nothing from the maintainer's homelab is shared — you create an OAuth client, drop `credentials.json` here, and sign in locally.

**Full walkthrough time:** ~15 minutes the first time.

---

## What you need

| Piece | Required? | Notes |
|-------|-----------|-------|
| Google Cloud project | Calendar: **yes** · Gmail: **yes** if you want email | Free tier is fine |
| `credentials.json` | **Yes** | OAuth **Desktop app** client secret |
| `token.json` | **Yes** for calendar | Created by `auth_google.py` |
| `gmail_token.json` | Optional | Bernie's bot mailbox — `auth_gmail.py` |
| Python 3.11+ on the host | **Yes** for auth scripts | Containers only *read* the tokens |

Run auth on the **machine that runs Docker**, from the repo root — **not** inside `bernie-discord`.

---

## 1. Create the OAuth client

1. Open [Google Cloud Console](https://console.cloud.google.com/) → create or pick a project.
2. **APIs & Services → Library** → enable:
   - **Google Calendar API** (required for schedule features)
   - **Gmail API** (only if you want Bernie to send/read mail)
3. **APIs & Services → OAuth consent screen**
   - User type: **External** (or Internal if you use Google Workspace)
   - Add your Google account as a **test user** while the app is in "Testing"
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Download JSON → save as:

   ```text
   family-bot/credentials/credentials.json
   ```

   See `credentials/credentials.json.example` for the expected shape.

> **Testing mode:** Google may show "unverified app" — that's normal for a personal homelab. Proceed as the test user you added.

---

## 2. Install Python deps (host only)

```bash
cd family-bot
pip install --user google-auth-oauthlib google-api-python-client
```

Use a venv if you prefer: `python3 -m venv .venv && source .venv/bin/activate`.

---

## 3. Calendar auth → `token.json`

```bash
python scripts/auth_google.py
# If port 8085 is already in use on your machine:
# GOOGLE_OAUTH_PORT=9090 python scripts/auth_google.py
```

1. Open the URL printed in the terminal (default `http://localhost:8085`, or whatever `GOOGLE_OAUTH_PORT` you set).
2. Sign in as the Google account that **owns the family calendars** you want Bernie to read.
3. Approve Calendar access.

On success you'll see a list of calendar IDs. Paste the ones you need into `config.json`:

```jsonc
"shared_calendars": [
  { "id": "primary", "name": "Family", "include_in_summary": true }
],
"family_members": {
  "parent_one": {
    "calendars": ["primary", "abc123@group.calendar.google.com"]
  }
}
```

Re-run after `/reload` or container restart — Bernie picks up `credentials/token.json` via the `./credentials` mount.

---

## 4. Gmail auth → `gmail_token.json` (optional)

Bernie can send email (digests, study guides, notifications) and read inbox signals when this token exists.

**Recommended:** create a dedicated mailbox (e.g. `bernie@yourfamily.com` or a spare Gmail) — not a personal inbox.

```bash
python scripts/auth_gmail.py
# If port 8086 is busy: GMAIL_OAUTH_PORT=9091 python scripts/auth_gmail.py
```

1. Open the URL printed in the terminal (default `http://localhost:8086`, different from calendar so both can stay configured).
2. Sign in as **Bernie's mailbox account**.
3. Approve Gmail send + read scopes.

Confirm `.env` points at the token (defaults are fine):

```bash
GMAIL_TOKEN_FILE=/credentials/gmail_token.json
```

If `gmail_token.json` is missing, email tools quietly disable — calendar still works.

---

## 5. Two tokens, one OAuth app

| Token file | Script | Who signs in | Scopes |
|------------|--------|--------------|--------|
| `token.json` | `auth_google.py` | Parent / calendar owner | Calendar |
| `gmail_token.json` | `auth_gmail.py` | Bernie's bot account | Gmail send + readonly |

Both use the **same** `credentials.json`. Re-authing one does **not** update the other.

---

## 6. Wire paths in `.env`

Defaults match Docker mounts:

```bash
GOOGLE_CREDENTIALS_FILE=/credentials/credentials.json
GOOGLE_TOKEN_FILE=/credentials/token.json
GMAIL_TOKEN_FILE=/credentials/gmail_token.json
```

On the host, files live under `family-bot/credentials/`. Compose maps that folder to `/credentials` inside containers.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `credentials.json not found` | Download Desktop OAuth JSON from GCP; path must match `CREDENTIALS_DIR` or repo `credentials/` |
| `Google token missing` in logs | Run `auth_google.py`; check `credentials/token.json` exists |
| `redirect_uri_mismatch` | Client type must be **Desktop app**, not Web |
| `Access blocked` / unverified | Add your Google account as OAuth **test user** on consent screen |
| Calendar list empty | Signed into wrong Google account — delete `token.json`, re-run |
| Email tools say not configured | Run `auth_gmail.py` or skip email features |
| Token expired / revoked | Delete the affected `*.json` token file and re-run the matching script |

**Never commit** `credentials.json`, `token.json`, or `gmail_token.json` — they're gitignored.

---

## Backup before `git pull`

`config.json` and everything under `credentials/` stay on disk but are **not** in git. Copy dated backups outside the repo before major pulls:

```bash
cp config.json config.json.bak-$(date +%Y%m%d)
cp -a credentials credentials.bak-$(date +%Y%m%d)
```

See [deploy.md](deploy.md) for the full first-run checklist.
