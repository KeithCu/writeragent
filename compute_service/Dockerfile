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

# Only what the service imports (not the whole extension UI surface).
COPY compute_service /app/compute_service
COPY plugin/scripting /app/plugin/scripting
COPY plugin/framework /app/plugin/framework
COPY plugin/__init__.py /app/plugin/__init__.py

ENV PYTHONPATH=/app/pkgs:/app
ENV HOST=127.0.0.1
ENV PORT=8000

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python", "compute_service/server.py"]
