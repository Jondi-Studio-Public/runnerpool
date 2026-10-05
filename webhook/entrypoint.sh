#!/bin/sh
# Container entrypoint: the webhook secret arrives as a file; the receiver holds no GitHub credentials.
export WEBHOOK_SECRET_FILE=/run/secrets/webhook_secret
# Not provisioned yet (docs/webhook.md step 1): stop cleanly. compose restarts only on failure, so a
# deploy before the secret exists leaves this container exited instead of looping.
if [ ! -s "$WEBHOOK_SECRET_FILE" ]; then
  echo "webhook: no secret provisioned, not starting (see docs/webhook.md)" >&2
  exit 0
fi
exec python3 /app/webhook/receiver.py
