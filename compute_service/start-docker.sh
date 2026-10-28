#!/usr/bin/env bash
# Build and run the Python compute service with Collabora-oriented hardening flags.
# From repo root: ./compute_service/start-docker.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Resolve the key file before cd. The script used to cd to the repo root
# first, so a relative path was taken against the repo instead of the
# caller's cwd. A source with no slash is a Docker named volume, not a bind
# mount. realpath makes the -v source absolute.
# GNU realpath still succeeds when the last component is missing, and Docker
# would then create a directory at the mount point. Require a regular file.
# Env key wins, same as load_settings, so a stale key-file path is not
# checked when PYTHON_COMPUTE_API_KEY is set. The file is only the mount
# source when it is the secret we will use.
KEY_FILE=""
if [[ -z "${PYTHON_COMPUTE_API_KEY:-}" && -n "${PYTHON_COMPUTE_API_KEY_FILE:-}" ]]; then
  if [[ ! -f "$PYTHON_COMPUTE_API_KEY_FILE" ]]; then
    echo "PYTHON_COMPUTE_API_KEY_FILE is not a file: $PYTHON_COMPUTE_API_KEY_FILE" >&2
    exit 1
  fi
  KEY_FILE="$(realpath "$PYTHON_COMPUTE_API_KEY_FILE")"
fi

cd "$ROOT"
IMAGE="${PYTHON_COMPUTE_IMAGE:-python-compute}"
# The process binds 0.0.0.0 inside the container. The published port is
# loopback on the host, but other containers on the bridge can reach it.
if [[ -z "${PYTHON_COMPUTE_API_KEY:-}" && -z "$KEY_FILE" ]]; then
  echo "PYTHON_COMPUTE_API_KEY or PYTHON_COMPUTE_API_KEY_FILE is required." >&2
  exit 1
fi

# The publish target used to be hardcoded 127.0.0.1:8000:8000, and this
# variable was not passed into the container. The process kept the image
# default of 8000, so setting the variable did nothing. Publish and listen
# on the same port. Forwarding the variable without changing -p would make
# the listener and the published port disagree.
PORT="${PYTHON_COMPUTE_PORT:-8000}"
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( 10#$PORT < 1 || 10#$PORT > 65535 )); then
  echo "PYTHON_COMPUTE_PORT must be an integer from 1 to 65535." >&2
  exit 1
fi

# 1g: --memory-swap equals --memory, so there is no extra swap. The first
# cell imports numpy, pandas, sympy, and scipy.stats, and matplotlib only when
# the cell names plt. 512m only covers the startup check (numpy and sympy).
MEMORY="${PYTHON_COMPUTE_MEMORY:-1g}"
docker build -f compute_service/Dockerfile -t "$IMAGE" .

# Env key wins over a key file, same as load_settings. -e PYTHON_COMPUTE_API_KEY
# put the secret in the docker run argv, where ps can show it. Write the
# bytes to a mode-600 temp file and mount that instead. printf '%s' adds no
# newline: the key-file reader strips one trailing newline, and the env
# value is not stripped, so an added newline would change the secret.
# No exec below. An EXIT trap has to remove the temp file after docker run
# returns. The shell waits in the foreground process group, so SIGINT still
# reaches docker.
tmp_key=""
cleanup() {
  if [[ -n "$tmp_key" ]]; then
    rm -f "$tmp_key"
  fi
}
trap cleanup EXIT

MOUNT_SRC=""
if [[ -n "${PYTHON_COMPUTE_API_KEY:-}" ]]; then
  tmp_key="$(mktemp)"
  printf '%s' "$PYTHON_COMPUTE_API_KEY" > "$tmp_key"
  chmod 600 "$tmp_key"
  MOUNT_SRC="$tmp_key"
elif [[ -n "$KEY_FILE" ]]; then
  MOUNT_SRC="$KEY_FILE"
fi

# Do not use --network=none together with -p (published ports need a namespace).
# --memory-swap equal to --memory disables extra swap (Docker's default is
# about the same amount of swap again).
# --init: the server is PID 1 and spawns formula workers. It reaps children
# it tracks. A grandchild reparented to PID 1 is not reaped without an init.
# --user: the image user is appuser. A host key file mode 600 is unreadable
# unless that uid matches. --cap-drop ALL also stops a root entrypoint from
# reading another user's 600 file. /app is world-readable and /tmp is a
# 1777 tmpfs, so the invoking uid can run the server.
docker_args=(
  docker
  run
  --rm
  --init
  --read-only
  --tmpfs
  /tmp:rw,size=64m,mode=1777
  --memory="$MEMORY"
  --memory-swap="$MEMORY"
  --cpus="${PYTHON_COMPUTE_CPUS:-1}"
  --pids-limit=256
  --security-opt
  no-new-privileges
  --cap-drop
  ALL
  --user
  "$(id -u):$(id -g)"
  -e
  PYTHON_COMPUTE_HOST=0.0.0.0
  -e
  "PYTHON_COMPUTE_PORT=${PORT}"
  -p
  "127.0.0.1:${PORT}:${PORT}"
)
if [[ -n "$MOUNT_SRC" ]]; then
  docker_args+=(
    -v
    "${MOUNT_SRC}:/run/secrets/python_compute_api_key:ro"
    -e
    PYTHON_COMPUTE_API_KEY_FILE=/run/secrets/python_compute_api_key
  )
fi
docker_args+=("$IMAGE")
"${docker_args[@]}"
