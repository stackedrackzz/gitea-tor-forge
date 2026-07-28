#!/usr/bin/bash
# One-time bootstrap (idempotent, re-run safe on every container start):
#   1. wait for Gitea to answer HTTP
#   2. create the automation admin user + API token inside the gitea
#      container (needs the mounted podman socket -- gitea's own admin
#      CLI is the only supported non-interactive way to provision a user)
#   3. register a system webhook pointing back at this container, so
#      every repo (existing or new) reports to us without per-repo setup
#   4. hand off to the webhook HTTP server
set -euo pipefail

: "${GITEA_INTERNAL_URL:?}"
: "${GITEA_CONTAINER_NAME:?}"
: "${GITEA_ADMIN_USER:?}"
: "${GITEA_ADMIN_PASSWORD:?}"
: "${GITEA_ADMIN_EMAIL:?}"
: "${WEBHOOK_SECRET:?}"
: "${GITEA_API_TOKEN_FILE:?}"
: "${LISTEN_PORT:=8099}"

mkdir -p "$(dirname "$GITEA_API_TOKEN_FILE")"

echo "[bootstrap] waiting for gitea at ${GITEA_INTERNAL_URL} ..."
until curl -fsS "${GITEA_INTERNAL_URL}/api/healthz" >/dev/null 2>&1; do
    sleep 2
done
echo "[bootstrap] gitea is up"

if [ ! -s "$GITEA_API_TOKEN_FILE" ]; then
    echo "[bootstrap] creating admin user ${GITEA_ADMIN_USER} (ok if it already exists)"
    podman exec -u git "$GITEA_CONTAINER_NAME" gitea admin user create \
        --admin \
        --username "$GITEA_ADMIN_USER" \
        --password "$GITEA_ADMIN_PASSWORD" \
        --email "$GITEA_ADMIN_EMAIL" \
        --must-change-password=false \
        || echo "[bootstrap] user create returned non-zero, assuming already exists"

    echo "[bootstrap] minting API token"
    # Narrower scopes (write:admin,write:repository,write:user) were
    # tested and silently produced empty results on GET /admin/hooks
    # rather than a clear 403 -- "all" avoids that scope-enforcement
    # inconsistency. This token never leaves the mirror-agent volume.
    podman exec -u git "$GITEA_CONTAINER_NAME" gitea admin user generate-access-token \
        --username "$GITEA_ADMIN_USER" \
        --token-name "mirror-agent-$(date +%s)" \
        --scopes "all" \
        --raw \
        > "$GITEA_API_TOKEN_FILE"
    chmod 600 "$GITEA_API_TOKEN_FILE"
    echo "[bootstrap] token saved to ${GITEA_API_TOKEN_FILE}"
else
    echo "[bootstrap] reusing existing API token at ${GITEA_API_TOKEN_FILE}"
fi

TOKEN="$(cat "$GITEA_API_TOKEN_FILE")"
HOOK_URL="http://127.0.0.1:${LISTEN_PORT}/webhook"
HOOK_MARKER="$(dirname "$GITEA_API_TOKEN_FILE")/webhook-registered"

# GET /admin/hooks (list) has been observed to return [] even when hooks
# demonstrably exist (GET /admin/hooks/{id} finds them fine) -- a Gitea
# API quirk, not something worth working around server-side. A local
# marker is a reliable idempotency signal regardless.
if [ ! -e "$HOOK_MARKER" ]; then
    echo "[bootstrap] registering system webhook -> ${HOOK_URL}"
    curl -fsS -X POST \
        -H "Authorization: token ${TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"type\":\"gitea\",\"config\":{\"url\":\"${HOOK_URL}\",\"content_type\":\"json\",\"secret\":\"${WEBHOOK_SECRET}\"},\"events\":[\"repository\",\"push\"],\"active\":true}" \
        "${GITEA_INTERNAL_URL}/api/v1/admin/hooks" >/dev/null
    touch "$HOOK_MARKER"
    echo "[bootstrap] system webhook created"
else
    echo "[bootstrap] system webhook already registered (marker present)"
fi

echo "[bootstrap] starting webhook server"
exec /usr/bin/python3 /usr/local/bin/webhook_server.py
