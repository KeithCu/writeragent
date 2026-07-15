#!/bin/bash
# WriterAgent - Python Compute Service Startup Script
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Default to writeragent/.venv if VENV_PATH is not set
VENV_PATH="${VENV_PATH:-$PROJECT_ROOT/.venv}"

# Resolve python interpreter
PYTHON_EXE="$VENV_PATH/bin/python"
if [ ! -f "$PYTHON_EXE" ] && [ -f "$VENV_PATH/Scripts/python.exe" ]; then
    PYTHON_EXE="$VENV_PATH/Scripts/python.exe"
fi

if [ -f "$PYTHON_EXE" ]; then
    echo "Starting compute service with venv python: $PYTHON_EXE"
    exec "$PYTHON_EXE" "$SCRIPT_DIR/server.py" "$@"
else
    echo "Virtual environment not found at $VENV_PATH."
    echo "Please configure writeragent/.venv or set VENV_PATH environment variable."
    echo "Falling back to system python..."
    exec python "$SCRIPT_DIR/server.py" "$@"
fi
