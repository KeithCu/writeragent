# WriterAgent - Python Compute Service Dockerfile
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Stage 1: Install wheels. The runner copies only /app/pkgs, so a source
# build would link against libraries that never arrive in the runtime image.
# --only-binary fails the build when a pin has no wheel.
FROM python:3.12-slim AS builder

WORKDIR /app

# Pins match a known-good classic/desktop stack; Pillow required for multi-figure merge.
RUN pip install --no-cache-dir --only-binary=:all: --target=/app/pkgs \
    "numpy==2.3.5" \
    "pandas==2.3.3" \
    "scipy==1.16.3" \
    "matplotlib==3.10.0" \
    "Pillow==12.2.0" \
    "sympy==1.14.0" \
    "pint==0.25.3" \
    "deal==4.24.6"

# Stage 2: Final runner image (no compiler)
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
COPY plugin/framework/http_server.py /app/plugin/framework/http_server.py
COPY plugin/framework/i18n.py /app/plugin/framework/i18n.py
COPY plugin/framework/json_utils.py /app/plugin/framework/json_utils.py
COPY plugin/framework/process_worker.py /app/plugin/framework/process_worker.py
COPY plugin/framework/thread_guard.py /app/plugin/framework/thread_guard.py
COPY plugin/contrib/smolagents /app/plugin/contrib/smolagents
COPY plugin/contrib/__init__.py /app/plugin/contrib/__init__.py
# JSON-forward does not load the Cython flatten accelerator. Workers import
# unpack helpers from payload_codec and must not pay for contrib/vec_pack.
# server.py imports plugin.vision.vision_common at load (IMPLEMENTED_HELPERS).
# The runner stage never copied plugin/vision, so the process exited with
# ModuleNotFoundError before it bound a port. Do not copy
# plugin/vision/__init__.py: that file imports module_base and
# plugin.vision.venv.vision (desktop VisionModule / run_vision). The package
# init runs before vision_common and fails the same way. With no __init__.py,
# plugin.vision is a namespace package. vision_common has no relative imports.
# plugin.framework.config is imported only inside merge_vision_params.
# OCR stays out of this image (ocr_workers defaults to 0).
# Every cell imports plugin.scripting.named_scripts, which imports
# domain_registry, which imports plugin.doc.doc_type at load. Without this
# file, result = 1 fails with ModuleNotFoundError: plugin.doc. Do not copy
# plugin/doc/__init__.py: it imports common_module (desktop ModuleBase).
# With no __init__.py, plugin.doc is a namespace package and doc_type loads.
COPY plugin/doc/doc_type.py /app/plugin/doc/doc_type.py
COPY plugin/vision/vision_common.py /app/plugin/vision/vision_common.py

RUN python -m compileall -q /app/compute_service /app/plugin

# Workers scrub PYTHONPATH (LibreOffice must not leak its stdlib into a
# child). install_image_pkgs in worker_base puts /app/pkgs back on sys.path
# by directory, so this variable is only for the parent process.
ENV PYTHONPATH=/app/pkgs:/app
# Bind all container interfaces. HOST/PORT were unread; the process used
# PYTHON_COMPUTE_HOST and defaulted to 127.0.0.1, so docker -p never connected.
ENV PYTHON_COMPUTE_HOST=0.0.0.0
ENV PYTHON_COMPUTE_PORT=8000
# --read-only plus --user <host uid> has no passwd home, and the image home
# is not writable either. Matplotlib otherwise warns and rebuilds its config
# on every import. /tmp is the tmpfs the launcher mounts; do not create
# /tmp/mpl here, because that mount replaces /tmp and Matplotlib creates it.
ENV HOME=/tmp
ENV MPLCONFIGDIR=/tmp/mpl
# Prefer a mounted secret file in production:
#   -e PYTHON_COMPUTE_API_KEY_FILE=/run/secrets/python_compute_api_key
# or:
#   -e PYTHON_COMPUTE_API_KEY=...
# Match coolwsd security.python_compute.api_key when set.
# docker-entrypoint.sh only execs the server. ComputeSettings.validate
# refuses this wildcard bind when no API key is set.
# OCR is not in this image (ocr_workers defaults to 0). A vision request
# returns VISION_UNAVAILABLE.

# /app is world-readable. The real run is a host uid, so owning the tree
# adds a full extra layer and still does not match that uid. --no-create-home
# beats Debian login.defs CREATE_HOME; nothing can write a home on a
# read-only root, and HOME points at /tmp.
RUN useradd --no-create-home appuser \
    && chmod 755 /app/compute_service/docker-entrypoint.sh
USER appuser

EXPOSE 8000

# python:3.12-slim has no curl. Exec form reads PYTHON_COMPUTE_PORT at check
# time. start-period covers the numpy/sympy import check before the socket listens.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PYTHON_COMPUTE_PORT','8000')+'/health', timeout=2)"]

CMD ["compute_service/docker-entrypoint.sh"]
