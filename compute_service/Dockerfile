# WriterAgent - Python Compute Service Dockerfile
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Pins match a known-good classic/desktop stack; Pillow required for multi-figure merge.
RUN pip install --no-cache-dir \
    "numpy==2.3.5" \
    "pandas==2.3.3" \
    "scipy==1.16.3" \
    "matplotlib==3.10.0" \
    "Pillow==12.2.0" \
    "sympy==1.14.0" \
    "pint==0.25.3" \
    "deal==4.24.6"

# Only what the service imports (not the whole extension UI surface).
COPY compute_service /app/compute_service
COPY plugin/scripting /app/plugin/scripting
COPY plugin/framework /app/plugin/framework
COPY plugin/__init__.py /app/plugin/__init__.py

ENV PYTHONPATH=/app
ENV PORT=8000

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python", "compute_service/server.py"]
