# Makefile — WriterAgent extension build & dev tools.
# Copyright (c) 2024 John Balis
# Copyright (c) 2025-2026 quazardous (registries, build system)
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
#
# Cross-platform: detects Windows vs Linux/macOS and calls .ps1 or .sh scripts.
#
# Build:
#   make build                     Build .oxt (all modules auto-discovered)
#   make xcu                       Generate XCS/XCU from Python config schemas
#   make clean                     Remove build artifacts
#
# Dev workflow:
#   make deploy                    Build + register once (if needed) + hot-sync to LO cache
#   make cache                     Hot-sync to LO cache only (after a prior make deploy)
#   make dev-deploy-remove         Remove legacy share\extensions symlink (Windows migration)
#
# LibreOffice:
#   make lo-start                  Launch LO with debug logging
#   make lo-start-full             Launch LO with verbose logging
#   make lo-kill                   Kill all LO processes
#
# Cache:
#   make clean-cache               Repair extension cache
#   make nuke-cache                Wipe entire extension cache
#   make unbundle                  Remove bundled dev symlink
#
# Info:
#   make help                      Show this help

EXTENSION_NAME = WriterAgent
LIBREPY_EXTENSION_ID = org.extension.librepy
LIBREPY_OXT = build/LibrePy.oxt
LIBREHARPER_EXTENSION_ID = org.extension.libreharper
LIBREHARPER_OXT = build/LibreHarper.oxt
LO_DEBUG_LOG = $(LO_CONF)/user/config/writeragent_debug.log
COMPONENTS := writer calc draw impress
SELECTED_COMPONENT := $(filter $(COMPONENTS),$(MAKECMDGOALS))


# ── Local overrides (gitignored) ────────────────────────────────────────────
# Create Makefile.local with e.g. USE_DOCKER = 1
-include Makefile.local

# Set NO_RECORDING=1 to build without voice recording (excludes recording modules and Record button).
NO_RECORDING ?= 0

# Set USE_DOCKER=1 to build via Docker instead of local Python/PyYAML.
# Persistent: echo "USE_DOCKER = 1" > Makefile.local
# One-shot:   make deploy USE_DOCKER=1
USE_DOCKER ?=

# ── OS detection ─────────────────────────────────────────────────────────────

ifeq ($(OS),Windows_NT)
    # Use Git Bash as shell so Unix commands (sleep, rm, cat, tail...) work everywhere.
    # Run install.ps1 to ensure Git for Windows is installed.
    # Use 8.3 short path (Progra~1) to avoid spaces that break $(firstword) and SHELL.
    BASH_PATH := $(wildcard C:/Progra~1/Git/usr/bin/bash.exe)
    ifeq ($(BASH_PATH),)
        BASH_PATH := $(wildcard C:/Progra~1/Git/bin/bash.exe)
    endif
    ifeq ($(BASH_PATH),)
        BASH_PATH := $(wildcard C:/Program\ Files/Git/usr/bin/bash.exe)
    endif
    ifneq ($(BASH_PATH),)
        SHELL   := $(BASH_PATH)
    endif
    .SHELLFLAGS := --login -c
    MAKE    := "$(MAKE)"
    SCRIPTS = $(PROJECT_ROOT)/scripts
    RUN_SH  = powershell -ExecutionPolicy Bypass -File
    EXT     = .ps1
    PYTHON  = python
    RM_RF   = rm -rf
    MKDIR   = mkdir -p
    HOME_DIR = $(subst \,/,$(USERPROFILE))
    LO_CONF = $(HOME_DIR)/AppData/Roaming/LibreOffice/4
    # LibreOffice program dir is not in PATH on Windows; detect for unopkg.
    LO_PROGRAM := $(firstword $(wildcard C:/Progra~1/LibreOffice/program) $(wildcard C:/Progra~2/LibreOffice/program))
    ifneq ($(LO_PROGRAM),)
        UNOPKG := "$(LO_PROGRAM)/unopkg.exe"
        LO_PYTHON ?= $(LO_PROGRAM)/python.exe
    else
        UNOPKG := unopkg
        LO_PYTHON ?= python
    endif
else
    SCRIPTS = $(PROJECT_ROOT)/scripts
    RUN_SH  = bash
    EXT     = .sh
    PYTHON  ?= python3
    RM_RF   = rm -rf
    MKDIR   = mkdir -p
    UNAME_S := $(shell uname -s 2>/dev/null)
    ifeq ($(UNAME_S),Darwin)
    LO_CONF := $(HOME)/Library/Application Support/LibreOffice/4
    else
    LO_CONF := $(HOME)/.config/libreoffice/4
    endif
    HOME_DIR = $(HOME)
    UNOPKG := unopkg
endif

# Directory of *this* Makefile, not $(CURDIR). `make release` runs
# `make -f $(PROJECT_ROOT)/Makefile test-uno` from a stripped tree in /tmp
# (no Makefile there). Using CURDIR made PROJECT_ROOT=/tmp/... and
# recursive `$(MAKE) lo-kill` failed with "No rule to make target".
PROJECT_ROOT := $(abspath $(dir $(firstword $(MAKEFILE_LIST))))
# Prefer project .venv so "make test" uses venv even when shell isn't activated
ifneq ($(wildcard $(PROJECT_ROOT)/.venv/bin/python),)
    PYTHON := $(PROJECT_ROOT)/.venv/bin/python
endif
OPENGREP_DIR := tests/semgrep
OPENGREP_CONFIGS := \
	$(OPENGREP_DIR)/uno_thread_safety.yml \
	$(OPENGREP_DIR)/writeragent_security.yml \
	$(OPENGREP_DIR)/third_party/semgrep-rules \
	$(OPENGREP_DIR)/third_party/trailofbits
# Mirror tests/semgrep/semgrepignore (opengrep --semgrepignore-filename accepts basename only).
OPENGREP_EXCLUDES := --exclude=plugin/contrib --exclude=plugin/lib
OPENGREP_SCAN_FLAGS := --error --severity ERROR --taint-intrafile $(OPENGREP_EXCLUDES)
OPENGREP_ENV := SEMGREP_SEND_METRICS=off
ifeq ($(OS),Windows_NT)
ifneq ($(wildcard $(PROJECT_ROOT)/.venv/Scripts/python.exe),)
    PYTHON := $(PROJECT_ROOT)/.venv/Scripts/python.exe
endif
endif
# Optional PySpector CLI (console script; no python -m). Not part of make test.
ifeq ($(OS),Windows_NT)
    PYSPECTOR := $(PROJECT_ROOT)/.venv/Scripts/pyspector.exe
else
    PYSPECTOR := $(PROJECT_ROOT)/.venv/bin/pyspector
endif
OPENGREP := $(shell "$(PYTHON)" $(SCRIPTS)/opengrep_path.py 2>/dev/null)
ifeq ($(OPENGREP),)
ifeq ($(OS),Windows_NT)
    OPENGREP := $(PROJECT_ROOT)/bin/opengrep.exe
else
    OPENGREP := $(PROJECT_ROOT)/bin/opengrep
endif
endif

# ── Phony targets ────────────────────────────────────────────────────────────

.PHONY: help build build-no-recording release release-no-test release-build update-xml repack repack-deploy register-built-oxt manifest manifest-core manifest-harper rdb-core build-core build-core-native deploy-core register-librepy-oxt build-harper deploy-harper register-libreharper-oxt xcu clean \
        native build-native clean-native update-vec sync-vec \
        proxy-stubs \
        openrouter-catalog \
        install install-force uninstall cache \
        dev-deploy dev-deploy-remove \
        lo-start lo-start-full lo-kill lo-restart \
        clean-cache nuke-cache nuke-cache-force unbundle \
        log log-tail lo-log test pytest test-uno test-mock-sidebar test-run test-durations slowtests vhs test-visible lo-test-threadguard lo-test-threadguard-visible typecheck typecheck-full check-ext check-setup deploy ensure-uno \
        verify crosshair-check crosshair-cover crosshair-check-all crosshair-check-all-deep \
        crosshair-cover-all crosshair-cover-all-deep \
        lo-start-log opengrep-lint opengrep-lint-advisory opengrep-rules-sync opengrep-rules-audit uno-thread-lint uno-thread-lint-advisory opengrep-install \
        writer calc draw impress \
        set-config vendor docker-build compile-translations compile-translations-core merge-translations refresh-pot reset-lang preview-translations check ty mypy pyright pyrefly bandit pyspector pyspector-report ty-run mypy-run pyright-run basedpyright basedpyright-run basedpyright-full-run pyrefly-run \
        ruff ruff-fix ruff-for-build ruff-format-check ruff-format-grammar \
        eval-deps run_eval run_eval-smoke run_eval-lo-scripted schema-docs mock-llm

# ── Help ─────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "WriterAgent — build & dev targets"
	@echo "================================="
	@echo ""
	@echo "Build:"
	@echo "  make build                  Build .oxt with tests (regular build, no Cython)"
	@echo "  make build-native           Build .oxt with Cython accelerator"
	@echo "  make openrouter-catalog     Fetch Orca slim OpenRouter catalog + refresh default_models.py (network)"
	@echo "  make release                Typecheck + bandit, then stripped-bundle tests in /tmp,"
	@echo "                              then build/register the release .oxt. (Cython if 'make native')"
	@echo "  make release-no-test        Build release OXT and register it without running tests/verification"
	@echo "  make build-no-recording     Build .oxt without voice recording (no Record button)"
	@echo "  make build-core             Build standalone LibrePy.oxt (scientific Python)"
	@echo "  make deploy-core            Build + install LibrePy (removes WriterAgent if present)"
	@echo "  make build-harper           Build standalone LibreHarper.oxt (Harper grammar only)"
	@echo "  make deploy-harper          Build + install LibreHarper (does not remove WriterAgent)"
	@echo "  make xcu                    Generate XCS/XCU from config schemas"
	@echo "  make schema-docs            Generate docs/writeragent-config-schema.md from module.yaml"
	@echo "  make clean                  Remove build artifacts"
	@echo ""
	@echo "Install:"
	@echo "  make deploy                 Build + register (first time only) + hot-sync; add writer/calc/draw/impress to also launch LO"
	@echo "  make register-built-oxt     Register build/WriterAgent.oxt via unopkg only (after make build)"
	@echo "  make install                Build + install via unopkg"
	@echo "  make install-force          Build + install (no prompts)"
	@echo "  make uninstall              Remove extension via unopkg"
	@echo "  make cache                  Hot-sync to LO cache only (skip build/register)"
	@echo ""
	@echo "Dev deploy:"
	@echo "  make dev-deploy             Hot-sync to LO cache (manifest regen; used internally by make deploy)"
	@echo "  make dev-deploy-remove      Remove legacy share\\extensions symlink (Windows migration)"
	@echo ""
	@echo "LibreOffice:"
	@echo "  make lo-start               Launch Writer (default) with debug logging"
	@echo "  make lo-start-full          Launch with verbose logging"
	@echo "  make log / make log-tail    Show or follow writeragent_debug.log (opt-in)"
	@echo "  make lo-kill                Kill all LO processes"
	@echo ""
	@echo "Cache:"
	@echo "  make clean-cache            Repair extension cache"
	@echo "  make nuke-cache             Wipe entire extension cache"
	@echo "  make unbundle               Remove bundled dev symlink"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build           Build .oxt in Docker (no local deps needed)"
	@echo "  USE_DOCKER=1                Use Docker for all build targets (deploy, install, ...)"
	@echo "                              Persistent: echo 'USE_DOCKER = 1' > Makefile.local"
	@echo ""
	@echo "Info:"
	@echo "  make check-setup            Verify dev stack (Python, LO, make, ...)"
	@echo "  make native                 Build Cython accelerator (default: x86-64)"
	@echo "                              Set WRITERAGENT_ARCH=x86-64-v[1-4] to override."
	@echo "  make check-ext              Verify extension is registered"
	@echo "  make set-config             List all config keys"
	@echo "  make test                   Run ty, mypy, pyright, pyspector, bandit, pytest + LO tests + excel-py-roundtrip"
	@echo "  make pytest                 Unit pytest only (xdist -n -1; PYTEST_WORKERS=0 for serial)"
	@echo "  make mock-llm               Fake OpenAI chat server on :18766 (sidebar soak: scroll, tools, Stop, errors)"
	@echo "  make test-uno               UNO tests only via testing_runner (serial live soffice)"
	@echo "  make test-uno FILTER=…      Same; FILTER=path or test_* name (native runner)"
	@echo "  make test-mock-sidebar      Packet F+B+C+D+E+G mock-LLM sidebar (visible soffice, your user profile)"
	@echo "  make test-mock-sidebar FILTER=E   Packet letter (B/C/D/E/F/G), case id (f3a), or test_* name"
	@echo "  make excel-py-roundtrip     Excel↔DAG sample fidelity over PythonExcelSamples/"
	@echo ""
	@echo "Benchmarks (prompt optimization / eval):"
	@echo "  make eval-deps              uv pip install dspy-ai (after uv sync)"
	@echo "  make run_eval               Run benchmark CLI (pass EVAL_ARGS=...)"
	@echo "  make run_eval-smoke         Quick smoke: one model, one example (live Qwen; needs a key)"
	@echo "  make run_eval-lo-scripted   Headless LO + scripted student (no API key)"
	@echo "  make test-run               make pytest, then serial UNO via testing_runner (no typecheck/bandit)"
	@echo "  make test-durations         Same filters as make pytest with --durations=40 (profile hotspots)"
	@echo "  make slowtests              Slow serialization once each: A/B fixtures, contracts/CrossHair, Hypothesis (vhs)"
	@echo "  make vhs                    Hypothesis serialization fuzz with verbose output (Hypothesis step of slowtests)"
	@echo "  make verify                 Pytest formal-verification suite (-k verification)"
	@echo "  make crosshair-check        CrossHair check on payload_codec.py (long; not in make test)"
	@echo "  make crosshair-cover        CrossHair cover on payload_codec.py (long; not in make test)"
	@echo "  make crosshair-check-all    CrossHair check every @deal. plugin module (regular: 25 iters / 5s + 120s wall; log: build/crosshair-check-all.log)"
	@echo "  make crosshair-check-all-deep  Same sweep, deep mode (200 iters, no per-condition timeout / wall). START_AT=42 resumes from module 42"
	@echo "  make crosshair-cover-all    CrossHair cover every @deal. plugin module (regular: 25 iters / 5s + 120s wall; process pool; log: build/crosshair-cover-all.log)"
	@echo "  make crosshair-cover-all-deep  Same sweep, deep mode (200 iters, no per-condition timeout / wall). START_AT=42 resumes from module 42"
	@echo "  make test-visible           Run LO chart + grep UNO tests visibly (GUI) for processEventsToIdle / OLE queue"
	@echo "  make lo-test-threadguard    Run full in-LO suite with WRITERAGENT_UNO_THREAD_GUARD=1 (Layer B)"
	@echo "  make opengrep-lint          Opengrep UNO + security rules (ERROR; part of make typecheck / make test)"
	@echo "  make opengrep-lint-advisory Same rules including WARNING-level nudges"
	@echo "  make opengrep-rules-sync    Refresh vendored third-party Opengrep rules"
	@echo "  make opengrep-rules-audit   Live registry sweep (p/python; manual triage only)"
	@echo "  make uno-thread-lint        Alias for make opengrep-lint"
	@echo "  make opengrep-install       Install Opengrep CLI (~/.local/bin or bin/opengrep)"
	@echo "  make typecheck              ruff-for-build, then basedpyright/bandit/opengrep/pyspector/ty/thread-safety/mypy in parallel (basedpyright does not walk numpy/etc. source)"
	@echo "  make typecheck-full         same as typecheck, but basedpyright walks library source (numpy/etc.); used by make release"
	@echo "  make ensure-uno             Link system UNO into .venv if import uno fails (auto-run by typecheck/test)"
	@echo "  make fix-uno                Same as ensure-uno with verbose output"
	@echo "  make mypy / make basedpyright / make pyrefly / make bandit   Single-tool runs (bandit: plugin/, excludes contrib + tests)"
	@echo "  make pyrefly                Experimental Meta Pyrefly checker (same scope as ty; not part of make test)"
	@echo "  make pyspector              PySpector AI/taint SAST on plugin/ (--ai; part of make typecheck / make test)"
	@echo "  make pyspector-report       Same scan, write build/pyspector-report.json (optional report)"
	@echo "  make ruff                   Ruff lint (plugin tests scripts; excludes contrib/lib/demos; see pyproject.toml)"
	@echo "  make ruff-fix               Ruff with --fix; make ruff-format-check = ruff format --check plugin/"
	@echo "  make ruff-for-build         Ruff --fix then check (used by make build)"
	@echo "  make ruff-format-grammar    Ruff format ai_grammar_proofreader.py only (project line-length 320)"
	@echo ""
	@echo "Translation:"
	@echo "  make translate-missing      Auto-translate missing strings with AI"
	@echo "  make reset-lang LANG=pt     Clear all translations for a language and reset to template"
	@echo ""

# ── Build ────────────────────────────────────────────────────────────────────

vendor:
	uv pip install --target vendor -r requirements-vendor.txt

ensure-uno:
	@"$(PYTHON)" scripts/fix_uno_import.py -q

fix-uno:
	@"$(PYTHON)" scripts/fix_uno_import.py

docker-build:
	UID=$$(id -u) GID=$$(id -g) docker compose -f builder/docker-compose.yml up --build
	@echo "Done: build/writeragent.oxt"

auto-translate:
	@echo "Regenerating translation templates (.pot)..."; \
	$(MAKE) extract-strings; \
	"$(PYTHON)" scripts/translate_missing.py --preview; \
	if [ -n "$$OPENROUTER_API_KEY" ]; then \
		echo "Auto-translating missing strings with AI..."; \
		"$(PYTHON)" scripts/translate_missing.py --execute --skip-initial-status; \
	fi

refresh-pot:
	@if command -v xgettext >/dev/null 2>&1; then \
		echo "Regenerating translation templates (.pot) without updating .po..."; \
		"$(PYTHON)" scripts/extract_xdl_strings.py; \
		xgettext --add-location=file -d writeragent -o locales/writeragent.pot $$(find plugin -name "*.py"); \
		"$(PYTHON)" scripts/merge_module_yaml_into_pot.py locales/writeragent.pot; \
		rm -f plugin/xdl_strings.py; \
	else \
		echo "Skipping .pot regeneration (xgettext not found; install gettext: choco install gettext.install)"; \
	fi

preview-translations: refresh-pot
	"$(PYTHON)" scripts/translate_missing.py --preview


ifeq ($(USE_DOCKER),1)
build: ty ruff-for-build preview-translations compile-translations
	@$(MAKE) docker-build
else
build: ty ruff-for-build preview-translations vendor manifest compile-translations
	@echo "Building $(EXTENSION_NAME).oxt (with tests)..."
	"$(PYTHON)" $(SCRIPTS)/build_oxt.py --output build/$(EXTENSION_NAME).oxt $(if $(filter 1,$(NO_RECORDING)),--no-recording)
	@echo "Done: build/$(EXTENSION_NAME).oxt  (bundle in build/bundle/)"
endif

build-no-recording: ty ruff-for-build preview-translations vendor manifest compile-translations
	@echo "Building $(EXTENSION_NAME).oxt (no voice recording)..."
	"$(PYTHON)" $(SCRIPTS)/build_oxt.py --no-recording --output build/$(EXTENSION_NAME).oxt
	@echo "Done: build/$(EXTENSION_NAME).oxt  (bundle in build/bundle/)"

# Full verification: typecheck-full (includes bandit + basedpyright library walk), then a stripped-with-tests tree in /tmp
# (tmpfs: faster compileall / pytest bytecode) so stripping doesn't break logic,
# then build the clean release oxt in build/.
schema-docs:
	$(PYTHON) $(SCRIPTS)/generate_config_schema_docs.py

release: clean
	@$(MAKE) schema-docs
	@$(MAKE) typecheck-full
	@echo "Building stripped bundle for verification in a temp dir..."
	@set -e; \
	RELEASE_TMP=$$("$(PYTHON)" -c "import tempfile; print(tempfile.mkdtemp(prefix='writeragent-release-'))"); \
	trap '$(MAKE) -C "$(PROJECT_ROOT)" lo-kill || true; rm -rf "$$RELEASE_TMP"' EXIT; \
	echo "Stripped verification tree: $$RELEASE_TMP"; \
	"$(PYTHON)" $(SCRIPTS)/build_oxt.py --strip --bundle-dir "$$RELEASE_TMP" --skip-zip; \
	"$(PYTHON)" -m compileall -j 0 -q "$$RELEASE_TMP/plugin" "$$RELEASE_TMP/tests"; \
	cp pyproject.toml "$$RELEASE_TMP/pyproject.toml"; \
	echo "Running tests against stripped bundle..."; \
	echo "  (grammar_obs call-site tests self-skip via _grammar_obs_call_sites_present; whole modules ignored below)"; \
	cd "$$RELEASE_TMP" && PYTHONPATH=. "$(abspath $(PYTHON))" -m pytest --ignore=tests/scripts --ignore=tests/compute_service --ignore=tests/test_merge_module_yaml_into_pot.py --ignore=tests/framework/test_logging.py --ignore=tests/writer/locale/test_grammar_linguistic_xcu.py --ignore=tests/scripting/test_generate_tool_proxies.py --ignore=tests/framework/test_thread_guard.py --ignore=tests/framework/test_thread_affinity.py --ignore=tests/framework/test_thread_token.py --ignore=tests/doc/test_specialized_delegation_threading.py --ignore=tests/writer/locale/test_grammar_obs.py --ignore=tests/writer/locale/test_libreharper_oxt.py --ignore=tests/chatbot/test_sidebar_test_hooks.py -k "not test_sync_tool_marshaled_from_background and not test_execute_on_main_thread_timeout and not test_execute_python_addin_from_background_thread" tests; \
	cd "$$RELEASE_TMP" && PYTHONPATH=. $(MAKE) -f "$(PROJECT_ROOT)/Makefile" test-uno; \
	$(MAKE) -C "$(PROJECT_ROOT)" release-build; \
	$(MAKE) -C "$(PROJECT_ROOT)" register-built-oxt

release-no-test:
	@$(MAKE) release-build
	@$(MAKE) register-built-oxt

openrouter-catalog:
	"$(PYTHON)" scripts/sync_orca_openrouter_catalog.py
	"$(PYTHON)" -m ruff format plugin/framework/default_models.py

update-xml:
	"$(PYTHON)" -c "import sys; sys.path.insert(0, '$(SCRIPTS)'); from manifest_registry import generate_update_xml; generate_update_xml('$(PROJECT_ROOT)', 'update.xml')"

release-build: auto-translate vendor manifest openrouter-catalog compile-translations update-xml schema-docs
	@echo "Building $(EXTENSION_NAME).oxt (release, bundle without tests)..."
	"$(PYTHON)" $(SCRIPTS)/build_oxt.py --no-tests --output build/$(EXTENSION_NAME).oxt $(if $(filter 1,$(NO_RECORDING)),--no-recording)
	@echo "Done: build/$(EXTENSION_NAME).oxt  (bundle in build/bundle/)"



repack:
	@echo "Re-packing from build/bundle/..."
	"$(PYTHON)" $(SCRIPTS)/build_oxt.py --repack --output build/$(EXTENSION_NAME).oxt
	@echo "Done: build/$(EXTENSION_NAME).oxt"

repack-deploy: repack register-built-oxt
	@$(if $(SELECTED_COMPONENT),$(MAKE) lo-start-log COMPONENT=$(SELECTED_COMPONENT))

# Stop LibreOffice if running, then unopkg remove + add build/$(EXTENSION_NAME).oxt.
# Does not start LO (use ``make deploy`` with writer/calc/draw/impress, or ``make lo-start``).
# Also removes LibrePy: both OXTs register org.extension.writeragent.PythonFunction,
# so leaving LibrePy installed makes unopkg fail with "enabling: addin.py".
register-built-oxt:
	@echo "Registering build/$(EXTENSION_NAME).oxt..."
	$(MAKE) lo-kill
	@rm -f "$(LO_CONF)/.lock" "$(LO_CONF)/user/.lock"
	-$(UNOPKG) remove $(LIBREPY_EXTENSION_ID) 2>/dev/null
	-$(UNOPKG) remove org.extension.writeragent 2>/dev/null
	@rm -f "$(LO_CONF)/user/extensions/tmp/extensions.pmap"
	@$(RM_RF) "$(LO_CONF)/user/extensions/tmp/extensions/"*.tmp_
	$(UNOPKG) add build/$(EXTENSION_NAME).oxt
	@rm -f $(HOME_DIR)/writeragent.log $(HOME_DIR)/writeragent_agent.log $(HOME_DIR)/writeragent_debug.log
	@rm -f "$(LO_DEBUG_LOG)" "$(LO_CONF)/user/writeragent_debug.log" "$(LO_CONF)/user/writeragent_agent.log"
	@echo "Registered org.extension.writeragent (start LibreOffice manually to load it)."

manifest:
	"$(PYTHON)" $(SCRIPTS)/generate_manifest.py

manifest-core:
	"$(PYTHON)" $(SCRIPTS)/generate_manifest.py --modules scripting vision \
		--manifest-output build/generated/_manifest_librepy.py \
		--skip-writeragent-extension --skip-addons

rdb-core:
	$(RUN_SH) $(SCRIPTS)/rebuild_librepy_rdb$(EXT)

build-core: vendor manifest-core rdb-core compile-translations-core
	@echo "Building LibrePy.oxt (standalone core extension)..."
	"$(PYTHON)" $(SCRIPTS)/build_librepy_oxt.py --output $(LIBREPY_OXT)
	@echo "Done: $(LIBREPY_OXT)  (bundle in build/bundle-librepy/)"

build-core-native: native build-core

register-librepy-oxt:
	@echo "Registering $(LIBREPY_OXT)..."
	$(MAKE) lo-kill
	@rm -f "$(LO_CONF)/.lock" "$(LO_CONF)/user/.lock"
	-$(UNOPKG) remove $(LIBREPY_EXTENSION_ID) 2>/dev/null
	-$(UNOPKG) remove org.extension.writeragent 2>/dev/null
	@rm -f "$(LO_CONF)/user/extensions/tmp/extensions.pmap"
	@$(RM_RF) "$(LO_CONF)/user/extensions/tmp/extensions/"*.tmp_
	$(UNOPKG) add $(LIBREPY_OXT)
	@rm -f $(HOME_DIR)/writeragent.log $(HOME_DIR)/writeragent_agent.log $(HOME_DIR)/writeragent_debug.log
	@rm -f "$(LO_DEBUG_LOG)" "$(LO_CONF)/user/writeragent_debug.log" "$(LO_CONF)/user/writeragent_agent.log"
	@echo "Registered $(LIBREPY_EXTENSION_ID) (start LibreOffice manually to load it)."

deploy-core: build-core register-librepy-oxt
	@$(if $(SELECTED_COMPONENT),$(MAKE) lo-start-log COMPONENT=$(SELECTED_COMPONENT))

manifest-harper:
	@echo "LibreHarper manifest is generated by scripts/build_libreharper_oxt.py from extension-harper/module.yaml"

build-harper: vendor
	@echo "Building LibreHarper.oxt (Harper grammar Linguistic2 proofreader)..."
	"$(PYTHON)" $(SCRIPTS)/build_libreharper_oxt.py --output $(LIBREHARPER_OXT)
	@echo "Done: $(LIBREHARPER_OXT)  (bundle in build/bundle-libreharper/)"

register-libreharper-oxt:
	@echo "Registering $(LIBREHARPER_OXT)..."
	$(MAKE) lo-kill
	@rm -f "$(LO_CONF)/.lock" "$(LO_CONF)/user/.lock"
	-$(UNOPKG) remove $(LIBREHARPER_EXTENSION_ID) 2>/dev/null
	@rm -f "$(LO_CONF)/user/extensions/tmp/extensions.pmap"
	@$(RM_RF) "$(LO_CONF)/user/extensions/tmp/extensions/"*.tmp_
	$(UNOPKG) add $(LIBREHARPER_OXT)
	@echo "Registered $(LIBREHARPER_EXTENSION_ID) (WriterAgent left installed if present)."

deploy-harper: build-harper register-libreharper-oxt
	@$(if $(SELECTED_COMPONENT),$(MAKE) lo-start-log COMPONENT=$(SELECTED_COMPONENT))

# Copy prebuilt writeragent_vec wheels from contrib/vec_pack into plugin/contrib/vec_pack (runtime import path).
sync-vec:
	$(MKDIR) plugin/contrib/vec_pack
	cp contrib/vec_pack/pack.* plugin/contrib/vec_pack/ 2>/dev/null || true

native:
	cd native/writeragent_vec && "$(PYTHON)" setup.py build_ext --inplace
	$(MKDIR) plugin/contrib/vec_pack
	cp native/writeragent_vec/src/writeragent_vec/*.so plugin/contrib/vec_pack/ 2>/dev/null || \
	cp native/writeragent_vec/src/writeragent_vec/*.pyd plugin/contrib/vec_pack/ 2>/dev/null || true
	# Strip debug symbols on Linux/macOS
	@if [ "$(OS)" != "Windows_NT" ]; then \
		strip plugin/contrib/vec_pack/*.so 2>/dev/null || true; \
	fi
	echo "try:" > plugin/contrib/vec_pack/__init__.py
	echo "    from .pack import fast_flatten_grid_2d, fast_flatten_grid_1d" >> plugin/contrib/vec_pack/__init__.py
	echo "except ImportError:" >> plugin/contrib/vec_pack/__init__.py
	echo "    fast_flatten_grid_2d = None" >> plugin/contrib/vec_pack/__init__.py
	echo "    fast_flatten_grid_1d = None" >> plugin/contrib/vec_pack/__init__.py

update-vec:
	@if [ -z "$(WHEELS_DIR)" ]; then \
		echo "Usage: make update-vec WHEELS_DIR=/path/to/wheels"; \
		exit 1; \
	fi
	"$(PYTHON)" scripts/update_vec_contrib.py "$(WHEELS_DIR)"

update-vec-fetch:
	"$(PYTHON)" scripts/update_vec_contrib.py --fetch

# Convenience target to build with Cython accelerator
build-native: native build

clean-native:
	$(RM_RF) native/writeragent_vec/build
	$(RM_RF) native/writeragent_vec/src/writeragent_vec/*.so
	$(RM_RF) native/writeragent_vec/src/writeragent_vec/*.pyd
	$(RM_RF) native/writeragent_vec/src/writeragent_vec/*.c

proxy-stubs:
	"$(PYTHON)" scripts/generate_tool_proxies.py > plugin/scripting/writeragent_api.py

xcu: manifest

clean: clean-native
	$(RM_RF) build
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true

# ── Install ──────────────────────────────────────────────────────────────────

install: build
	$(RUN_SH) $(SCRIPTS)/install-plugin$(EXT) --build-only=false

install-force: build
ifeq ($(OS),Windows_NT)
	$(RUN_SH) $(SCRIPTS)/install-plugin$(EXT) -Force
else
	$(RUN_SH) $(SCRIPTS)/install-plugin$(EXT) --force
endif

uninstall:
ifeq ($(OS),Windows_NT)
	$(RUN_SH) $(SCRIPTS)/install-plugin$(EXT) -Uninstall -Force
else
	$(RUN_SH) $(SCRIPTS)/install-plugin$(EXT) --uninstall --force
endif

cache:
ifeq ($(OS),Windows_NT)
	$(RUN_SH) $(SCRIPTS)/install-plugin$(EXT) -Cache
else
	$(RUN_SH) $(SCRIPTS)/install-plugin$(EXT) --cache
endif

# ── Dev deploy ───────────────────────────────────────────────────────────────

dev-deploy:
	$(RUN_SH) $(SCRIPTS)/dev-deploy$(EXT)

dev-deploy-remove:
ifeq ($(OS),Windows_NT)
	$(RUN_SH) $(SCRIPTS)/dev-deploy$(EXT) -Remove
else
	$(RUN_SH) $(SCRIPTS)/dev-deploy$(EXT) --remove
endif

# ── LibreOffice ──────────────────────────────────────────────────────────────

lo-start:
	WRITERAGENT_SET_CONFIG="$(WRITERAGENT_SET_CONFIG)" $(RUN_SH) $(SCRIPTS)/launch-lo-debug$(EXT) $(if $(COMPONENT),--$(COMPONENT))

lo-start-log:
	$(MAKE) lo-start COMPONENT=$(COMPONENT)
	@echo "Waiting for LO to load..."
	@sleep 12
	@echo "Plugin log: $(LO_DEBUG_LOG)"
	@echo "LO stderr:  $(HOME_DIR)/soffice-debug.log"
	@echo "Follow plugin log: make log-tail"

lo-start-full:
ifeq ($(OS),Windows_NT)
	$(RUN_SH) $(SCRIPTS)/launch-lo-debug$(EXT) -Full
else
	$(RUN_SH) $(SCRIPTS)/launch-lo-debug$(EXT) --full
endif

lo-kill:
	$(RUN_SH) $(SCRIPTS)/kill-libreoffice$(EXT)

# ── Cache management ─────────────────────────────────────────────────────────

clean-cache:
	$(RUN_SH) $(SCRIPTS)/clean-cache$(EXT)

nuke-cache:
ifeq ($(OS),Windows_NT)
	$(RUN_SH) $(SCRIPTS)/clean-cache$(EXT) -Nuke
else
	$(RUN_SH) $(SCRIPTS)/clean-cache$(EXT) --nuke
endif

unbundle:
ifeq ($(OS),Windows_NT)
	$(RUN_SH) $(SCRIPTS)/clean-cache$(EXT) -Unbundle
else
	$(RUN_SH) $(SCRIPTS)/clean-cache$(EXT) --unbundle
endif

nuke-cache-force:
	$(RM_RF) "$(LO_CONF)/user/uno_packages/cache"
	rm -f "$(LO_CONF)/user/extensions/tmp/extensions.pmap"
	@$(RM_RF) "$(LO_CONF)/user/extensions/tmp/extensions/"*.tmp_
	rm -f "$(LO_CONF)/.lock"

# ── Translation ──────────────────────────────────────────────────────────────
extract-strings:
	@if command -v xgettext >/dev/null 2>&1; then \
		"$(PYTHON)" scripts/extract_xdl_strings.py; \
		xgettext --add-location=file -d writeragent -o locales/writeragent.pot $$(find plugin -name "*.py"); \
		"$(PYTHON)" scripts/merge_module_yaml_into_pot.py locales/writeragent.pot; \
		rm -f plugin/xdl_strings.py; \
		$(MAKE) merge-translations; \
	else \
		echo "Skipping string extraction (xgettext not found; install gettext: choco install gettext.install)"; \
	fi

# Merge each locale .po with writeragent.pot, then strip obsolete entries (#~) so removed
# source strings do not accumulate. (msgattrib --no-obsolete: portable where msgmerge lacks --no-obsolete.)
merge-translations:
	@if command -v msgmerge >/dev/null 2>&1; then \
		find locales -name writeragent.po -exec sh -c 'f="$$1"; msgmerge --add-location=file --update --backup=none "$$f" locales/writeragent.pot && msgattrib --no-obsolete -o "$$f.tmp" "$$f" && mv -f "$$f.tmp" "$$f"' _ {} \;; \
	else \
		echo "Skipping .po merge (msgmerge not found; install gettext: choco install gettext.install)"; \
	fi


add-language:
	mkdir -p locales/$(LANG)/LC_MESSAGES
	cp locales/writeragent.pot locales/$(LANG)/LC_MESSAGES/writeragent.po
	msgfmt -o locales/$(LANG)/LC_MESSAGES/writeragent.mo locales/$(LANG)/LC_MESSAGES/writeragent.po

reset-lang: refresh-pot
	@if [ -z "$(LANG)" ]; then echo "Usage: make reset-lang LANG=pt"; exit 1; fi
	@echo "Resetting $(LANG) to template..."
	$(MAKE) add-language LANG=$(LANG)

translate-missing:
	"$(PYTHON)" scripts/translate_missing.py --execute

compile-translations:
	@if command -v msgfmt >/dev/null 2>&1; then \
		find locales -name "*.po" -exec sh -c 'msgfmt -o "$$(dirname $$1)/$$(basename $$1 .po).mo" "$$1"' _ {} \;; \
	else \
		echo "Skipping .mo compilation (msgfmt not found; install gettext: choco install gettext.install)"; \
	fi

compile-translations-core:
	"$(PYTHON)" $(SCRIPTS)/build_librepy_locales.py


# ── Shortcuts ───────────────────────────────────────────────────────────────

lo-restart:
	$(MAKE) lo-kill
	sleep 3
	rm -f "$(LO_CONF)/.lock" "$(LO_CONF)/user/.lock"
	$(MAKE) lo-start

ifeq ($(OS),Windows_NT)
DEPLOY_CACHE_FLAGS = -NoGen
else
DEPLOY_CACHE_FLAGS = --no-gen
endif

deploy: build
	$(RUN_SH) $(SCRIPTS)/dev-deploy$(EXT) $(DEPLOY_CACHE_FLAGS)
	@$(if $(SELECTED_COMPONENT),$(MAKE) lo-start-log COMPONENT=$(SELECTED_COMPONENT))

writer calc draw impress:
	@$(if $(filter deploy repack-deploy,$(MAKECMDGOALS)),,@echo "Stand-alone 'make $@' is disabled. Use 'make deploy $@' to build and launch.")

log:
	@cat "$(LO_DEBUG_LOG)" 2>/dev/null || echo "No writeragent_debug.log found (expected at $(LO_DEBUG_LOG))"

log-tail:
	@tail -f "$(LO_DEBUG_LOG)"

lo-log:
	@cat $(HOME_DIR)/soffice-debug.log 2>/dev/null || echo "No soffice-debug.log found"

check-setup:
	$(RUN_SH) $(SCRIPTS)/check-setup$(EXT)

check-ext:
	@$(UNOPKG) list 2>&1 | head -10
	@echo "---"
	@"$(PYTHON)" -c "from plugin._manifest import MODULES; print('Manifest OK: %d modules, %d with config' % (len(MODULES), len([m for m in MODULES if m.get('config')])))"

# For LO tests: use Python that has uno/officehelper (LibreOffice's Python on Windows;
# otherwise same as "python -m plugin.testing_runner").
# We try to detect one that has the 'uno' module available, falling back to 'python' if none found.
LO_PYTHON ?= $(shell python3 -c "import uno" 2>/dev/null && echo python3 || (python -c "import uno" 2>/dev/null && echo python || echo $(PYTHON)))

# -j6 leaves a core free. Do not pass basedpyright --threads: it nests workers on this pool.
typecheck: manifest ruff-for-build
	@echo "=== typecheck: basedpyright + bandit + opengrep + pyspector + ty + thread-safety + mypy (parallel) ==="
	@$(MAKE) -j6 basedpyright-run bandit opengrep-lint pyspector ty-run thread-safety-lint mypy-run

# Same tools as typecheck, but basedpyright analyzes numpy/pandas/etc. implementation.
typecheck-full: manifest ruff-for-build
	@echo "=== typecheck-full: basedpyright (library source) + bandit + opengrep + pyspector + ty + thread-safety + mypy (parallel) ==="
	@$(MAKE) -j6 basedpyright-full-run bandit opengrep-lint pyspector ty-run thread-safety-lint mypy-run

# Unit pytest only: no *_uno.py collection, no testing_runner / live soffice.
# Exact command: $(PYTHON) -m pytest tests -m "not slow and not integration" --ignore-glob='*_uno.py'
# Default adds $(PYTEST_XDIST) (-n auto --dist=loadgroup). PYTEST_WORKERS=0 is serial.
# Progress goes to stderr as full lines: pytest/xdist otherwise use \r rewrites
# (and classic mode never wraps), so Make/IDE terminals stay blank until exit.
PYTEST_WORKERS ?= auto
ifeq ($(PYTEST_WORKERS),0)
PYTEST_XDIST :=
else ifeq ($(PYTEST_WORKERS),)
PYTEST_XDIST :=
else
PYTEST_XDIST := -n $(PYTEST_WORKERS) --dist=loadgroup
endif
# --timeout is a backstop so a hung worker cannot sit ~19 min (Windows CI).
# Per-test 300s (5 min) is well above unit-test runtime; hang points (Harper close, MCP
# 400 body) are fixed in code. Not in pyproject addopts — vhs/slowtests need longer.
PYTEST_UNIT = WRITERAGENT_PYTEST_PROGRESS=1 PYTHONUNBUFFERED=1 "$(PYTHON)" -u -m pytest tests -m "not slow and not integration" --ignore-glob="*_uno.py" --timeout=300 --timeout-method=thread $(PYTEST_XDIST)

pytest:
	@echo "=== pytest ==="
	$(PYTEST_UNIT)

# Dev-only OpenAI-compatible stub (not MCP: 18766, not 8765/18765). See docs/chat/rich-text-control-sidebar.md
mock-llm:
	$(PYTHON) scripts/mock_llm_server.py

# Optional native-runner selectors: packet letter (B/C/D/E/F/G), case id (f3a), or test_* name.
FILTER ?=

test-uno:
	@$(MAKE) -C "$(PROJECT_ROOT)" lo-kill
	PYTHONUNBUFFERED=1 "$(LO_PYTHON)" -u -m plugin.testing_runner $(FILTER); EXIT_CODE=$$?; $(MAKE) -C "$(PROJECT_ROOT)" lo-kill; exit $$EXIT_CODE

test-mock-sidebar:
	@$(MAKE) -C "$(PROJECT_ROOT)" lo-kill
	PYTHONUNBUFFERED=1 "$(LO_PYTHON)" -u -m plugin.testing_runner --user-profile tests/chatbot/test_mock_llm_sidebar_uno.py $(FILTER); EXIT_CODE=$$?; $(MAKE) -C "$(PROJECT_ROOT)" lo-kill; exit $$EXIT_CODE

test-run:
	@$(MAKE) pytest
	@$(MAKE) test-uno

test-durations:
	$(PYTEST_UNIT) --durations=40

# Deep Hypothesis for make vhs / slowtests (serialization env kept as alias).
_VHS_EXTENSIVE = WRITERAGENT_VHS_EXTENSIVE=1 WRITERAGENT_SERIALIZATION_EXTENSIVE=1

slowtests:
	@echo "=== [1/2] Serialization contracts + extensive A/B fixtures ==="
	$(_VHS_EXTENSIVE) "$(PYTHON)" -m pytest \
		tests/scripting/test_serialization_verification.py \
		tests/scripting/test_serialization_ab.py -k "not hypothesis" -q
	@echo "=== [2/2] Hypothesis fuzz (vhs: serialization + FSM/MCP) ==="
	@$(MAKE) vhs

vhs:
	@echo "Running deep Hypothesis fuzz (serialization + FSM + Phase 8 + security/normalize)..."
	$(_VHS_EXTENSIVE) "$(PYTHON)" -m pytest \
		tests/scripting/test_serialization_ab.py \
		tests/chatbot/test_fsm_verification.py \
		tests/mcp/test_mcp_state_verification.py \
		tests/calc/python/test_formula_edit_verification.py \
		tests/mcp/test_cors_verification.py \
		tests/writer/test_writer_diff_and_html_verification.py \
		tests/embeddings/test_embeddings_split_verification.py \
		tests/framework/test_stream_normalizer_verification.py \
		tests/framework/test_response_normalizers_verification.py \
		tests/scripting/test_sandbox_path_verification.py \
		tests/scripting/test_scrub_env_verification.py \
		tests/scripting/test_payload_codec_policy_verification.py \
		tests/calc/test_address_utils_verification.py \
		-k hypothesis -s --hypothesis-verbosity=verbose

test-visible:
	PYTHONUNBUFFERED=1 "$(LO_PYTHON)" -u -m plugin.testing_runner --visible test_charts_uno test_enhanced_charts_uno test_document_research_grep_uno test_rich_html_uno; EXIT_CODE=$$?; $(MAKE) -C "$(PROJECT_ROOT)" lo-kill; exit $$EXIT_CODE

lo-test-threadguard:
	WRITERAGENT_UNO_THREAD_GUARD=1 $(MAKE) test-uno

lo-test-threadguard-visible:
	WRITERAGENT_UNO_THREAD_GUARD=1 PYTHONUNBUFFERED=1 "$(LO_PYTHON)" -u -m plugin.testing_runner --visible test_charts_uno test_enhanced_charts_uno test_document_research_grep_uno test_rich_html_uno; EXIT_CODE=$$?; $(MAKE) -C "$(PROJECT_ROOT)" lo-kill; exit $$EXIT_CODE

opengrep-lint:
	@test -x "$(OPENGREP)" || (echo "opengrep not found — run: make opengrep-install" && exit 1)
	@test -f $(OPENGREP_DIR)/third_party/SOURCES.json || (echo "vendored Opengrep rules missing — run: make opengrep-rules-sync" && exit 1)
	@"$(PYTHON)" $(SCRIPTS)/run_timed.py opengrep env SEMGREP_SEND_METRICS=off "$(OPENGREP)" scan $(OPENGREP_SCAN_FLAGS) $(foreach c,$(OPENGREP_CONFIGS),-c $(c)) plugin

thread-safety-lint:
	"$(PYTHON)" scripts/lint_thread_safety.py plugin/calc/python plugin/scripting
	"$(PYTHON)" scripts/analyze_thread_deadlocks.py plugin

uno-thread-lint: opengrep-lint thread-safety-lint

opengrep-lint-advisory:
	@test -x "$(OPENGREP)" || (echo "opengrep not found — run: make opengrep-install" && exit 1)
	@test -f $(OPENGREP_DIR)/third_party/SOURCES.json || (echo "vendored Opengrep rules missing — run: make opengrep-rules-sync" && exit 1)
	$(OPENGREP_ENV) "$(OPENGREP)" scan --severity WARNING --taint-intrafile $(OPENGREP_EXCLUDES) $(foreach c,$(OPENGREP_CONFIGS),-c $(c)) plugin

uno-thread-lint-advisory: opengrep-lint-advisory

opengrep-rules-sync:
	bash $(SCRIPTS)/sync-opengrep-rules.sh

opengrep-rules-audit:
	@test -x "$(OPENGREP)" || (echo "opengrep not found — run: make opengrep-install" && exit 1)
	$(OPENGREP_ENV) "$(OPENGREP)" scan --config p/python $(OPENGREP_EXCLUDES) plugin

opengrep-install:
ifeq ($(OS),Windows_NT)
	powershell -NoProfile -ExecutionPolicy Bypass -Command 'if (-not (Get-Command New-TemporaryFile -ErrorAction SilentlyContinue)) { function New-TemporaryFile { $$p = [System.IO.Path]::GetTempFileName(); Get-Item -LiteralPath $$p } }; irm https://raw.githubusercontent.com/opengrep/opengrep/main/install.ps1 | iex'
else
	curl -fsSL https://raw.githubusercontent.com/opengrep/opengrep/main/install.sh | bash
endif

# Excel Python-in-Excel sample round-trip (Excel → DAG → Excel).
# Skips cleanly when PythonExcelSamples/ has no .xlsx (nested demos checkout optional).
excel-py-roundtrip:
	@if ! ls PythonExcelSamples/*.xlsx >/dev/null 2>&1; then \
		echo "excel-py-roundtrip: no PythonExcelSamples/*.xlsx — skipped"; \
	else \
		"$(PYTHON)" scripts/roundtrip_excel_py_samples.py --samples PythonExcelSamples --out build/excel_py_roundtrip; \
	fi

test:
	@echo "=== make test: typecheck ==="
	@$(MAKE) typecheck
	@echo "=== make test: pytest + LibreOffice ==="
	@$(MAKE) test-run
	@$(MAKE) excel-py-roundtrip

CROSSHAIR_MODULE = plugin/scripting/payload_codec.py

verify:
	@echo "=== Running All Formal Verification Unit Tests ==="
	"$(PYTHON)" -m pytest tests/ -k "verification" -m "not slow" -q

install-fizzbee:
	"$(PYTHON)" scripts/install_fizzbee.py --install

check-fizzbee:
	@if command -v fizz >/dev/null 2>&1 || command -v fizzbee >/dev/null 2>&1 || [ -x .venv/bin/fizz ] || [ -x .venv/bin/fizzbee ]; then \
		echo "=== Checking FizzBee Formal Models ==="; \
		FB=$$(command -v fizz || command -v fizzbee || echo ".venv/bin/fizz"); \
		if [ ! -x "$$FB" ]; then FB=".venv/bin/fizzbee"; fi; \
		ok=0; \
		for spec in tests/mcp/fizzbee/writer_mcp_protocol.fizz tests/mcp/fizzbee/writer_tools_model.fizz tests/mcp/fizzbee/calc_tools_model.fizz; do \
			echo "--- $$spec ---"; \
			if ! $$FB "$$spec" | tee /dev/stderr | grep -q "PASSED: Model checker completed successfully"; then \
				echo "FAILED: $$spec"; ok=1; \
			fi; \
		done; \
		exit $$ok; \
	else \
		echo "FizzBee is not installed. Run 'make install-fizzbee' or check docs/tests/fizzbee-testing.md."; \
		"$(PYTHON)" scripts/install_fizzbee.py --check; \
	fi

# CrossHair on entire module files (correctness over speed; see docs/framework-formal-verification.md)
# Use stream.py `run` (not a shell pipe) so engine crashes and exit codes are classified + summarized.
crosshair-check:
	"$(PYTHON)" scripts/crosshair_stream.py run check -- -v --report_all $(CROSSHAIR_MODULE)

crosshair-cover:
	"$(PYTHON)" scripts/crosshair_stream.py run cover -- -v $(CROSSHAIR_MODULE)

# Every plugin file with @deal. (deal contracts only). Not part of make test.
# Regular: 25 uninteresting iters + 5s per condition + 120s module wall. Deep: 200 iters, no timeout/wall.
# Regular payload_codec only: 5 / 5s (module_check_bounds / module_cover_bounds).
# START_AT=N (optional, 1-based) resumes from module N: make crosshair-check-all-deep START_AT=42
START_AT ?=
START_AT_FLAG := $(if $(START_AT),--start-at $(START_AT),)

crosshair-check-all:
	"$(PYTHON)" scripts/crosshair_check_all.py $(START_AT_FLAG)

crosshair-check-all-deep:
	"$(PYTHON)" scripts/crosshair_check_all.py --deep $(START_AT_FLAG)

# Cover (example synthesis) on the same @deal. set / skip list. Not part of make test.
crosshair-cover-all:
	"$(PYTHON)" scripts/crosshair_cover_all.py $(START_AT_FLAG)

crosshair-cover-all-deep:
	"$(PYTHON)" scripts/crosshair_cover_all.py --deep $(START_AT_FLAG)

# ── Benchmarks (scripts/prompt_optimization) ─────────────────────────────────

PO_EVAL_REQ := scripts/prompt_optimization/requirements.txt
EVAL_ARGS ?=

eval-deps:
	uv pip install -r $(PO_EVAL_REQ)

run_eval:
	"$(PYTHON)" scripts/benchmark.py $(EVAL_ARGS)

run_eval-smoke:
	$(MAKE) run_eval EVAL_ARGS="--models qwen/qwen3-coder-next -n 1 -j 1"

run_eval-lo-scripted:
	"$(PYTHON)" scripts/prompt_optimization/run_eval.py --backend lo --student scripted --no-bust-cache -v

# ── POC extension ───────────────────────────────────────────────────────────

set-config:
	@echo "Usage: make deploy WRITERAGENT_SET_CONFIG=\"mcp.port=9000,mcp.host=0.0.0.0\""
	@echo ""
	@echo "Available config keys (module.key = default):"
	@"$(PYTHON)" -c "from plugin._manifest import MODULES; \
	[print('  %s.%s = %s' % (m['name'], k, v.get('default',''))) \
	 for m in MODULES for k,v in m.get('config',{}).items()]"

poc-build:
	@$(MKDIR) build
	cd poc-ext && zip -r ../build/poc-ext.oxt . -x '*.pyc' '__pycache__/*'
	@echo "Built build/poc-ext.oxt"

poc-install: poc-build
	-$(UNOPKG) remove org.extension.poc 2>/dev/null
	sleep 2
	$(UNOPKG) add build/poc-ext.oxt
	@echo "POC installed"

poc-uninstall:
	-$(UNOPKG) remove org.extension.poc 2>/dev/null
	@echo "POC removed"

poc-log:
	@cat $(HOME_DIR)/poc-ext.log 2>/dev/null || echo "No poc-ext.log"

poc-log-tail:
	@tail -f $(HOME_DIR)/poc-ext.log

poc-deploy: poc-install
	$(MAKE) lo-kill
	@sleep 3
	@rm -f "$(LO_CONF)/.lock" "$(LO_CONF)/user/.lock"
	@rm -f $(HOME_DIR)/poc-ext.log
	$(MAKE) lo-start
	@echo "Waiting for LO..."
	@sleep 10
	@$(MAKE) poc-log

ty: manifest ty-run
mypy: manifest mypy-run
basedpyright: manifest basedpyright-run
pyrefly: manifest pyrefly-run

ty-run: ensure-uno
	@"$(PYTHON)" $(SCRIPTS)/run_timed.py ty "$(PYTHON)" -m ty check --exclude plugin/contrib/ --exclude plugin/lib/

mypy-run: ensure-uno
	@"$(PYTHON)" $(SCRIPTS)/run_timed.py mypy "$(PYTHON)" -m mypy

# Future task: try enabling `reportMissingTypeArgument = true` in pyproject.toml to enforce generic type parameters (dict[str, Any], list[str])
basedpyright-run: ensure-uno
	@"$(PYTHON)" $(SCRIPTS)/run_timed.py basedpyright "$(PYTHON)" -m basedpyright

basedpyright-full-run: ensure-uno
	@test -f pyrightconfig.full.json || (echo "pyrightconfig.full.json missing — without -p config basedpyright scans .venv and hangs" && exit 1)
	@"$(PYTHON)" $(SCRIPTS)/run_timed.py basedpyright-full "$(PYTHON)" -m basedpyright -p pyrightconfig.full.json

pyrefly-run: ensure-uno
	@"$(PYTHON)" $(SCRIPTS)/run_timed.py pyrefly "$(PYTHON)" -m pyrefly check

bandit:
	@"$(PYTHON)" $(SCRIPTS)/run_timed.py bandit "$(PYTHON)" -m bandit -r plugin -c pyproject.toml --severity-level medium

# Cross-file / AI-agent SAST (part of make typecheck / make test; not build/release).
# Wrapper disables reviewed FP rules; see scripts/run_pyspector.py.
pyspector:
	@"$(PYTHON)" -c "import pyspector" 2>/dev/null || (echo "pyspector not found — run: uv sync" && exit 1)
	@"$(PYTHON)" $(SCRIPTS)/run_timed.py pyspector "$(PYTHON)" $(SCRIPTS)/run_pyspector.py scan plugin --ai -c pyspector.toml --msg=False

pyspector-report:
	@"$(PYTHON)" -c "import pyspector" 2>/dev/null || (echo "pyspector not found — run: uv sync" && exit 1)
	@$(MKDIR) build
	"$(PYTHON)" $(SCRIPTS)/run_pyspector.py scan plugin --ai -c pyspector.toml --msg=False -f json -o build/pyspector-report.json

# demos/ is never in the ruff gate (local packs only).
RUFF_PATHS := plugin tests scripts

ruff:
	"$(PYTHON)" -m ruff check $(RUFF_PATHS)

ruff-fix:
	"$(PYTHON)" -m ruff check $(RUFF_PATHS) --fix

# Build gate: auto-fix then verify (standalone `make ruff` remains check-only).
ruff-for-build: ruff-fix ruff

ruff-format-check:
	"$(PYTHON)" -m ruff format --check plugin

# Grammar proofreader: formatting this file only is faster than `ruff format plugin`.
ruff-format-grammar:
	"$(PYTHON)" -m ruff format plugin/writer/locale/ai_grammar_proofreader.py
