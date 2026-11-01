# WriterAgent - Python Compute Service Dockerfile
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

FROM python:3.14-slim AS wheels
WORKDIR /app
# Pins are the known-good cp314 binary set. Only this image runs 3.14;
# the LibreOffice plugin/host code stays 3.11-compatible. deal is omitted:
# deal_shim stubs a missing install, and the bundle strips @deal. Pillow is
# for a returned PIL.Image and user `import PIL`. Figure capture defaults to
# SVG. statsmodels and seaborn are two of Python in Excel's default imports;
# openpyxl backs pandas read_excel/to_excel. See
# docs/scripting/python-in-excel-packages.md.
# wrapt is pinned because formulaic (statsmodels) requires wrapt>=1.17.0rc1
# on Python 3.13+. That prerelease specifier lets pip pick wrapt dev builds.
RUN pip install --no-cache-dir --only-binary=:all: --target=/app/pkgs \
    "numpy==2.4.6" "pandas==3.1.0" "scipy==1.17.1" "matplotlib==3.11.2" \
    "Pillow==13.0.0" "sympy==1.14.0" "pint==0.26.2" \
    "statsmodels==0.15.0" "seaborn==0.13.2" "openpyxl==3.1.5" "networkx==3.7" \
    "tabulate==0.10.0" \
    "xlrd==2.0.2" \
    "thefuzz==0.22.1" "rapidfuzz==3.14.6" \
    "beautifulsoup4==4.15.0" "soupsieve==3.0.4" \
    "scikit-learn==1.9.1" "joblib==1.6.0" "threadpoolctl==3.7.0" \
    "wrapt==2.5.2"

FROM python:3.14-slim AS bundle
WORKDIR /src
COPY plugin plugin
COPY compute_service compute_service
COPY scripts/__init__.py scripts/librepy_bundle_paths.py \
     scripts/strip_code.py scripts/build_compute_bundle.py scripts/
# strip_code imports plugin.framework.ast_stmt_edit from this source tree.
# That module is not part of the runtime closure and is not in /out.
RUN python scripts/build_compute_bundle.py --output /out

FROM python:3.14-slim
LABEL org.opencontainers.image.source="https://github.com/KeithCu/writeragent"
WORKDIR /app
COPY --from=wheels /app/pkgs /app/pkgs
COPY --from=bundle /out/ /app/
ENV PYTHONPATH=/app/pkgs:/app \
    PYTHON_COMPUTE_HOST=0.0.0.0 \
    PYTHON_COMPUTE_PORT=8000
ENV HOME=/tmp
ENV MPLCONFIGDIR=/tmp/mpl
ENV PYTHONDONTWRITEBYTECODE=1
RUN useradd --no-create-home appuser \
    && chmod 755 /app/compute_service/docker-entrypoint.sh
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PYTHON_COMPUTE_PORT','8000')+'/health', timeout=2)"]
CMD ["compute_service/docker-entrypoint.sh"]
