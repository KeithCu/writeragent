#!/usr/bin/env bash
# Build and run the Python compute service with Collabora-oriented hardening flags.
# From repo root: ./compute_service/start-docker.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE="${PYTHON_COMPUTE_IMAGE:-python-compute}"
docker build -f compute_service/Dockerfile -t "$IMAGE" .
# Do not use --network=none together with -p (published ports need a namespace).
exec docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 \
  --memory="${PYTHON_COMPUTE_MEMORY:-512m}" \
  --cpus="${PYTHON_COMPUTE_CPUS:-1}" \
  --pids-limit=256 \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  -e PYTHON_COMPUTE_HOST=0.0.0.0 \
  -p 127.0.0.1:8000:8000 \
  ${PYTHON_COMPUTE_API_KEY:+-e PYTHON_COMPUTE_API_KEY="$PYTHON_COMPUTE_API_KEY"} \
  ${PYTHON_COMPUTE_API_KEY_FILE:+-e PYTHON_COMPUTE_API_KEY_FILE="$PYTHON_COMPUTE_API_KEY_FILE"} \
  "$IMAGE"
