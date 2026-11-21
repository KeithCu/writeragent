#!/usr/bin/env bash
# Smoke-test a built python-compute image against /health and /v1/execute.
# Usage: compute_service/smoke-docker.sh IMAGE [PORT]
# Starts IMAGE with a generated API key, waits for /health, then POSTs
# =PY("1+1") and a DataFrame to /v1/execute the way the kit does.
set -euo pipefail

IMAGE="${1:?usage: smoke-docker.sh IMAGE [PORT]}"
PORT="${2:-18000}"

dir="$(mktemp -d)"
name="compute-smoke-$$"

cleanup() {
  local rc=$?
  if (( rc != 0 )); then
    docker logs "$name" >&2 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  rm -rf "$dir"
}
trap cleanup EXIT

key="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
printf '%s' "$key" > "$dir/key"
chmod 600 "$dir/key"

docker run -d \
  --name "$name" \
  --init \
  --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --user "$(id -u):$(id -g)" \
  -p "127.0.0.1:${PORT}:8000" \
  -v "$dir/key:/run/secrets/python_compute_api_key:ro" \
  -e PYTHON_COMPUTE_API_KEY_FILE=/run/secrets/python_compute_api_key \
  "$IMAGE" >/dev/null

ready=0
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if (( ready == 0 )); then
  echo "compute container did not become healthy on port ${PORT}" >&2
  exit 1
fi

post() {
  curl -fsS --max-time 120 \
    -H "Authorization: Bearer $key" \
    -H 'Content-Type: application/json' \
    --data-binary "$1" \
    "http://127.0.0.1:${PORT}/v1/execute"
}

check() {
  local body="$1"
  local expected="$2"
  local resp
  resp="$(post "$body")"
  python3 -c '
import json, sys
resp = json.loads(sys.argv[1])
expected = json.loads(sys.argv[2])
assert resp.get("status") == "ok", f"unexpected status: {resp}"
got = resp.get("result")
assert got == expected, f"expected {expected!r}, got {got!r}"
' "$resp" "$expected"
}

check '{"code":"1+1"}' '2'
check '{"code":"import pandas as pd\npd.DataFrame({\"a\": [1, 2]})"}' '[["a"],[1],[2]]'

echo "compute image smoke test passed"
