# OpenAI models and Codex OAuth

Bernie supports two OpenAI paths. Pick the one that suits the household; both
keep model selection and tool permissions inside Bernie.

- **GPT-5.6 Luna via OpenRouter** is the low-cost choice for everyday family
  chat. It uses a normal API key and does not need the subscription runner.
- **ChatGPT/Codex OAuth** is optional. It uses the official Codex device login
  and native app-server tool calling through an isolated Docker sidecar.

**Grok OAuth is in progress and is not part of the public setup.**
The public `subscriptions` profile builds the Codex runner without installing
the Grok CLI.

## Use Luna directly

Set `OPENROUTER_API_KEY` in `.env`, then use the supplied Luna alias in
`config.json`:

```json
{
  "openrouter_direct": true,
  "litellm_models": ["or-gpt-56-luna"],
  "active_model": "or-gpt-56-luna",
  "webui_model": "or-gpt-56-luna"
}
```

The full example config maps `or-gpt-56-luna` to
`openai/gpt-5.6-luna`. You can choose a different model for a specific surface
later in Bernie’s model settings.

## Use ChatGPT/Codex OAuth

Generate a runner secret in `.env`:

```bash
openssl rand -hex 32
# paste the result as SUBSCRIPTION_RUNNER_SECRET=... in .env
```

Start the optional runner, then complete the official device login. OAuth state
lives in the runner’s named Docker volume, not in `credentials/` or host
`~/.codex`.

```bash
docker compose --profile subscriptions up -d --build bernie-subscription-runner
docker compose --profile subscriptions run --rm \
  --entrypoint codex bernie-subscription-runner login --device-auth
docker compose --profile subscriptions run --rm \
  --entrypoint codex bernie-subscription-runner login status
```

Then enable the Codex entry already present in `subscription_models` and select
it as the model. For example:

```json
{
  "active_model": "gpt-5.6-luna",
  "webui_model": "gpt-5.6-luna",
  "subscription_models": [
    {
      "provider": "codex",
      "model": "gpt-5.6-luna",
      "capabilities": ["text", "tools", "structured-output"],
      "openrouter_fallback_model": "openai/gpt-5.6-luna",
      "litellm_alias": "or-gpt-56-luna",
      "enabled": true
    }
  ]
}
```

The OpenRouter mapping is a resilience path if OAuth is unavailable; it does
not change the selected Codex model. Keep `OPENROUTER_API_KEY` configured when
you enable Codex OAuth so Bernie can continue if the subscription runner needs
reauthentication.

## Status and safety

- `GET /health` on the runner reports `ready`, `reauth-required`, or
  `unavailable` without returning account identity, tokens, or file paths.
- Re-run the device login if status is `reauth-required`.
- Do not paste device codes, OAuth files, or account details into Discord,
  logs, or `credentials/`.
- To stop using OAuth, set the Codex model’s `enabled` field to `false`, switch
  the selected model back to Luna, Anthropic, or Ollama, and restart Bernie.
