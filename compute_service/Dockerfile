# WriterAgent - Python Compute Service Dockerfile
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install scientific/data python stack
RUN pip install --no-cache-dir \
    numpy \
    pandas \
    scipy \
    matplotlib \
    sympy \
    pint \
    deal

# Copy source code files
COPY compute_service /app/compute_service
COPY plugin /app/plugin

# Configure execution environment
ENV PYTHONPATH=/app
ENV PORT=8000

# Create non-root user for security isolation
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python", "compute_service/server.py"]
