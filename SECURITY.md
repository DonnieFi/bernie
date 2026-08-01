# Security Policy

## Supported versions

Security fixes are applied to the default branch of the public repository as time allows. Self-hosted operators should pin a known-good commit and review changes before upgrading.

## Reporting a vulnerability

**Do not** open a public GitHub issue for security vulnerabilities.

Prefer **GitHub private vulnerability reporting** on the public repo (Security → Report a vulnerability), or contact the maintainer privately if that feature is unavailable.

Please include:

- Description of the issue and impact
- Steps to reproduce (PoC without real secrets)
- Affected component (API, Discord role, ToolGateway, DB write path, etc.)
- Whether you believe remote unauthenticated exploit is possible

## Scope notes

Bernie is designed for a **trusted LAN / household** deploy:

- Dashboard and API tokens assume a private network unless you add reverse-proxy auth.
- `INTERNAL_POST_SECRET` must be set and unguessable for cross-container writes.
- Never commit `.env`, `config.json` with live tokens, OAuth tokens, or real family PII.

## Secrets that may need rotation

If any of these ever hit a remote git history or pastebin: Discord bot token, Anthropic/OpenRouter keys, Google OAuth client secrets, Langfuse keys, `INTERNAL_POST_SECRET`, HA long-lived tokens.
