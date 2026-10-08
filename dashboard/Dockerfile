# macs dashboard as a container. Build context is the repo root (compose.yaml sets it).
FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl gnupg openssh-client \
 && mkdir -p -m 755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /bin/bash macs

WORKDIR /app
# State of the CI watchdog (same image, second container; see docs/watchdog.md) and the shared CI store
# (/ci/ci.db, docs/webhook.md). A new named volume copies the owner of its mount point, so both stay uid 1000.
RUN mkdir /data /ci && chown macs:macs /data /ci
# PyJWT (with cryptography) signs the GitHub App's JWT; see dashboard/gh_app_token.py.
COPY dashboard/requirements.txt ./dashboard/requirements.txt
RUN pip install --no-cache-dir -r dashboard/requirements.txt
COPY runner ./runner
COPY mac/gitrunner ./mac/gitrunner
COPY dashboard ./dashboard
COPY watchdog ./watchdog
COPY webhook ./webhook

ENV MACS_BIND=0.0.0.0 MACS_PORT=8765 MACS_SCRIPT=/app/runner MACS_BASH=/bin/bash PYTHONUNBUFFERED=1
USER macs
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -fsS -m 4 http://127.0.0.1:8765/healthz || exit 1
ENTRYPOINT ["sh", "/app/dashboard/entrypoint.sh"]
