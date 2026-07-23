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
COPY plugin/scripting /app/plugin/scripting
COPY plugin/framework/__init__.py /app/plugin/framework/__init__.py
COPY plugin/framework/constants.py /app/plugin/framework/constants.py
COPY plugin/contrib/smolagents /app/plugin/contrib/smolagents
COPY plugin/contrib/__init__.py /app/plugin/contrib/__init__.py

ENV PYTHONPATH=/app/pkgs:/app
ENV HOST=127.0.0.1
ENV PORT=8000
# Prefer a mounted secret file in production:
#   -e PYTHON_COMPUTE_API_KEY_FILE=/run/secrets/python_compute_api_key
# or:
#   -e PYTHON_COMPUTE_API_KEY=...
# Match coolwsd security.python_compute.api_key when set.
# Empty key = no auth (dev/test only).

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python", "compute_service/server.py"]
