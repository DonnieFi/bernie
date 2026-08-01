FROM node:22-bookworm-slim

ARG CODEX_CLI_VERSION=0.144.6
ARG GROK_CLI_VERSION=0.2.106
ARG INSTALL_GROK=true

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 \
    && npm install --global "@openai/codex@${CODEX_CLI_VERSION}" \
    && if [ "$INSTALL_GROK" = "true" ]; then \
         curl -fsSL https://x.ai/cli/install.sh -o /tmp/install-grok.sh; \
         bash /tmp/install-grok.sh "${GROK_CLI_VERSION}"; \
         install -m 0755 "$(readlink -f /root/.grok/bin/grok)" /usr/local/bin/grok; \
         grok --version; \
       fi \
    && rm -f /usr/local/bin/agent \
    && codex --version \
    && rm -rf /root/.grok /root/.npm /tmp/install-grok.sh /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin runner \
    && install -d -o runner -g runner /app /var/lib/bernie/codex /var/lib/bernie/grok

COPY --chown=runner:runner server.py grok_adapter.py codex_adapter.py /app/

ENV CODEX_HOME=/var/lib/bernie/codex \
    GROK_HOME=/var/lib/bernie/grok \
    HOME=/home/runner \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER runner
EXPOSE 8080
ENTRYPOINT ["python3", "/app/server.py"]
