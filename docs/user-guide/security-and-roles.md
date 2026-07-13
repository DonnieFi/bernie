# Security & roles

Bernie connects to real messaging surfaces and smart home devices. Treat inbound Discord messages as **untrusted input** — especially in large servers.

For a family deploy: use a **private server**, lock down `#anvil`, and assign Discord roles that match Bernie's RBAC.

---

## RBAC roles

Configured in `config.json` → `family_members.<name>.role`:

| Role | Typical access |
|------|----------------|
| **admin** | Full system — logs, config reload, model switch, eval, network |
| **parents** | Household management — tasks, automations, calendar writes, email send |
| **kids** | Daily utility — own tasks, calendar read, weather, grocery, limited HA |
| **friend** | Guest — presence only (arrival announcements) |

Discord server roles map via `discord_roles` — create matching roles in Discord and assign members.

Details: [Family setup § RBAC](../family.md)

---

## Channel isolation

| Channel | Who should access | Why |
|---------|-------------------|-----|
| `#anvil` | Parents/admins only | Model switch, reload, eval, logs |
| `#security` | Family | Camera alerts — no admin tools |
| `#smithy` | Family | Main chat — curated tool surface |
| `#bellows` | Family | Bot silent — no AI attack surface |

Discord channel permissions are your first firewall. Bernie also enforces tool RBAC server-side.

---

## Tool gateway

Every tool call passes through `ToolGateway.execute()`:

1. **Role check** — maps Discord user → person → role
2. **JSON schema** — validates arguments
3. **HITL tier** — see below
4. **Audit** — Langfuse span + `activity_log`

Direct handler calls are forbidden in production code — always through the gateway.

---

## HITL tiers (human-in-the-loop)

| Tier | Behavior | Examples |
|------|----------|----------|
| **1** | Proceed silently | Calendar read, weather |
| **2** | Proceed + `#anvil` audit post | Config writes, tier-2 stubs |
| **3** | Hold → admin DM Approve/Deny | High-risk writes |

Tier 3 pending actions resume after approval with `hitl_approved=True`.

---

## Email policy

Outbound email is tightly scoped:

- **Recipients:** `family_members[].email` only — external addresses blocked
- **Body:** plain text — no markdown
- **Kids:** draft posted to main channel; parents react ✅/❌ before send
- **Rate limits:** `email.max_sends_per_hour`, per-domain caps

Inbox read: digests for all; full body parents only.

---

## Smart home writes

- Bernie resolves friendly names → HA entity IDs — never hardcoded in chat
- Shared devices (whole-house lights, locks) may ask confirmation in concierge mode
- Kids may be blocked from sensitive domains by mode ceiling

---

## Secrets handling

| Secret | Location | In git? |
|--------|----------|---------|
| Discord token | `.env` | **No** |
| Anthropic key | `.env` | **No** |
| Google OAuth | `credentials/*.json` | **No** |
| HA token | `config.json` | **No** (gitignored) |
| `INTERNAL_POST_SECRET` | `.env` | **No** |

Never commit real snowflakes, tokens, or API keys. Public repo uses `YOUR_*` placeholders.

Report vulnerabilities: [SECURITY.md](../../SECURITY.md) — no public issues with secrets.

---

## Data leaving your LAN

| Data | Destination |
|------|-------------|
| Chat text (per turn) | Anthropic API (or LiteLLM/Ollama if configured) |
| Calendar | Google (your account) |
| Optional traces | Langfuse host you configure |
| HA / cameras / presence | **Stays local** — read into prompts, not uploaded as archives |
| SQLite history | **Local disk** only |

No telemetry beacons. No cloud vector DB.

Anthropic API traffic is not used for training by default (see Anthropic policy).

---

## DM safety

Unlike OpenClaw's pairing flow, Bernie relies on:

- Private Discord server membership
- Role-based tool restrictions
- Optional DM mode toggle per person

Do not invite untrusted users to a server where Bernie has admin tools enabled.

---

## Dashboard exposure

Port `:8000` should stay on LAN or VPN unless you add TLS + strong auth. See [Web dashboard § exposure](web-dashboard.md#who-should-expose-this).

---

## Eval / shadow pipeline (operators)

Shadow eval captures alternate model responses **after** the family sees Bernie's reply — it does not change live behavior unless you manually switch models based on data.

`/shadow_mode` has no tool parity — Bernie cannot flip his own shadow model from chat.

---

## Kid-specific notes

- Class reminders → DMs only
- Chore completion → may require parent approval
- Email send → parent approval flow
- Research requests → parents can queue; results DM'd

---

## Checklist for a new household

- [ ] Private Discord server
- [ ] `#anvil` permission-locked
- [ ] Each person has correct `role` in config
- [ ] `.env` and `credentials/` not in git
- [ ] Backup `config.json` outside repo before pulls
- [ ] Frigate alerts go to `#security`, not main channel (optional)
- [ ] Review [Optional integrations](../integrations/optional-services.md) — disable what you don't need
