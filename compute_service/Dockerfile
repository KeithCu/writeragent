# WriterAgent - Python Compute Service Dockerfile
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Pins match a known-good classic/desktop stack; Pillow required for multi-figure merge.
# Install dependencies into a separate target directory to avoid copying build-essential/compilation environment.
RUN pip install --no-cache-dir --target=/app/pkgs \
    "numpy==2.3.5" \
    "pandas==2.3.3" \
    "scipy==1.16.3" \
    "matplotlib==3.10.0" \
    "Pillow==12.2.0" \
    "sympy==1.14.0" \
    "pint==0.25.3" \
    "deal==4.24.6"

# Stage 2: Final runner image (no build-essential / compiler)
FROM python:3.12-slim AS runner

WORKDIR /app

# Copy the pre-compiled packages from the builder stage
COPY --from=builder /app/pkgs /app/pkgs

# Runtime import closure only — not the WriterAgent Settings / writeragent.json stack.
COPY compute_service /app/compute_service
COPY plugin/__init__.py /app/plugin/__init__.py
COPY plugin/version.py /app/plugin/version.py
COPY plugin/scripting /app/plugin/scripting
COPY plugin/framework/__init__.py /app/plugin/framework/__init__.py
COPY plugin/framework/constants.py /app/plugin/framework/constants.py
COPY plugin/framework/deal_shim.py /app/plugin/framework/deal_shim.py
# worker_base imports plugin.framework.worker_pool at load. That imports
# errors (which imports i18n and json_utils) and thread_guard. constants.py
# and deal_shim.py above close that graph. queue_executor, uno_context, and
# logging are not imported at load time — do not copy them.
COPY plugin/framework/worker_pool.py /app/plugin/framework/worker_pool.py
COPY plugin/framework/errors.py /app/plugin/framework/errors.py
COPY plugin/framework/i18n.py /app/plugin/framework/i18n.py
COPY plugin/framework/json_utils.py /app/plugin/framework/json_utils.py
COPY plugin/framework/thread_guard.py /app/plugin/framework/thread_guard.py
COPY plugin/contrib/smolagents /app/plugin/contrib/smolagents
COPY plugin/contrib/__init__.py /app/plugin/contrib/__init__.py
# Host-side Cython flatten for large HTTP grids (python:3.12-slim → cp312 .so).
# Formula workers unpack / pack ndarrays without this binary.
COPY contrib/vec_pack /app/contrib/vec_pack

ENV PYTHONPATH=/app/pkgs:/app
# Bind all container interfaces. HOST/PORT were unread; the process used
# PYTHON_COMPUTE_HOST and defaulted to 127.0.0.1, so docker -p never connected.
ENV PYTHON_COMPUTE_HOST=0.0.0.0
ENV PYTHON_COMPUTE_PORT=8000
# Prefer a mounted secret file in production:
#   -e PYTHON_COMPUTE_API_KEY_FILE=/run/secrets/python_compute_api_key
# or:
#   -e PYTHON_COMPUTE_API_KEY=...
# Match coolwsd security.python_compute.api_key when set.
# The entrypoint refuses this wildcard bind when both are empty.

RUN useradd -m appuser \
    && chmod 755 /app/compute_service/docker-entrypoint.sh \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["compute_service/docker-entrypoint.sh"]
