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
#   make deploy                    Build + reinstall + restart LO + show log
#   make install                   Build + install via unopkg
#   make install-force             Build + install (no prompts, kills LO)
#   make cache                     Hot-deploy to LO cache (fast iteration)
#   make dev-deploy                Symlink project into LO extensions
#   make dev-deploy-remove         Remove the dev symlink
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
    SCRIPTS = scripts
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
        LO_PYTHON ?= "$(LO_PROGRAM)/python.exe"
    else
        UNOPKG := unopkg
        LO_PYTHON ?= python
    endif
else
    SCRIPTS = scripts
    RUN_SH  = bash
    EXT     = .sh
    PYTHON  ?= python3
    RM_RF   = rm -rf
    MKDIR   = mkdir -p
    LO_CONF = $(HOME)/.config/libreoffice/4
    HOME_DIR = $(HOME)
    UNOPKG := unopkg
endif

# Prefer project .venv so "make test" uses venv even when shell isn't activated
PROJECT_ROOT := $(CURDIR)
ifneq ($(wildcard .venv/bin/python),)
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
ifneq ($(wildcard .venv/Scripts/python.exe),)
    PYTHON := $(PROJECT_ROOT)/.venv/Scripts/python.exe
endif
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

.PHONY: help build build-no-recording release release-build repack repack-deploy register-built-oxt manifest manifest-core rdb-core build-core deploy-core register-librepy-oxt xcu clean \
        native build-native clean-native update-vec \
        proxy-stubs \
        openrouter-catalog \
        install install-force uninstall cache \
        dev-deploy dev-deploy-remove \
        lo-start lo-start-full lo-kill lo-restart \
        clean-cache nuke-cache nuke-cache-force unbundle \
        log log-tail lo-log test test-run slowtests vhs test-visible lo-test-threadguard lo-test-threadguard-visible typecheck check-ext check-setup deploy ensure-uno \
        lo-start-log opengrep-lint opengrep-lint-advisory opengrep-rules-sync opengrep-rules-audit uno-thread-lint uno-thread-lint-advisory opengrep-install \
        writer calc draw impress \
        set-config vendor docker-build compile-translations merge-translations refresh-pot reset-lang preview-translations check ty mypy pyright pyrefly bandit ty-run mypy-run pyright-run pyrefly-run \
        ruff ruff-fix ruff-for-build ruff-format-check ruff-format-grammar \
        eval-deps run_eval run_eval-smoke

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
	@echo "  make release                Regular build with full verification: test source, build stripped bundle,"
	@echo "                              test bundle, build final .oxt. (Includes Cython if pre-built via 'make native')"
	@echo "  make build-no-recording     Build .oxt without voice recording (no Record button)"
	@echo "  make build-core             Build standalone LibrePy.oxt (scientific Python prototype)"
	@echo "  make deploy-core            Build + install LibrePy (removes WriterAgent if present)"
	@echo "  make xcu                    Generate XCS/XCU from config schemas"
	@echo "  make clean                  Remove build artifacts"
	@echo ""
	@echo "Install:"
	@echo "  make deploy                 Build + register extension (stop LO, unopkg remove/add); add writer/calc/draw/impress to also launch LO"
	@echo "  make install                Build + install via unopkg"
	@echo "  make install-force          Build + install (no prompts)"
	@echo "  make uninstall              Remove extension via unopkg"
	@echo "  make cache                  Hot-deploy to LO cache"
	@echo ""
	@echo "Dev deploy:"
	@echo "  make dev-deploy             Symlink project into LO extensions"
	@echo "  make dev-deploy-remove      Remove the dev symlink"
	@echo ""
	@echo "LibreOffice:"
	@echo "  make lo-start               Launch Writer (default) with debug logging"
	@echo "  make lo-start-full          Launch with verbose logging"
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
	@echo "  make test                   Run ty, mypy, pyright, bandit, then pytest + in-process LO tests"
	@echo ""
	@echo "Benchmarks (prompt optimization / eval):"
	@echo "  make eval-deps              uv pip install dspy-ai (after uv sync)"
	@echo "  make run_eval               Run benchmark CLI (pass EVAL_ARGS=...)"
	@echo "  make run_eval-smoke         Quick smoke: one model, one example"
	@echo "  make test-run               Pytest + LO tests only (skip typecheck/bandit; for quick reruns)"
	@echo "  make slowtests              Slow serialization once each: A/B fixtures, contracts/CrossHair, Hypothesis (vhs)"
	@echo "  make vhs                    Hypothesis serialization fuzz with verbose output (Hypothesis step of slowtests)"
	@echo "  make test-visible           Run LO chart + grep UNO tests visibly (GUI) for processEventsToIdle / OLE queue"
	@echo "  make lo-test-threadguard    Run full in-LO suite with WRITERAGENT_UNO_THREAD_GUARD=1 (Layer B)"
	@echo "  make opengrep-lint          Opengrep UNO + security rules (ERROR; part of make test)"
	@echo "  make opengrep-lint-advisory Same rules including WARNING-level nudges"
	@echo "  make opengrep-rules-sync    Refresh vendored third-party Opengrep rules"
	@echo "  make opengrep-rules-audit   Live registry sweep (p/python; manual triage only)"
	@echo "  make uno-thread-lint        Alias for make opengrep-lint"
	@echo "  make opengrep-install       Install Opengrep CLI (~/.local/bin or bin/opengrep)"
	@echo "  make typecheck              Run ty, then mypy, then pyright (same scope as each single target)"
	@echo "  make check                  Quick gate: ty only"
	@echo "  make ensure-uno             Link system UNO into .venv if import uno fails (auto-run by typecheck/test)"
	@echo "  make fix-uno                Same as ensure-uno with verbose output"
	@echo "  make mypy / make pyright / make pyrefly / make bandit   Single-tool runs (bandit: plugin/, excludes contrib + tests)"
	@echo "  make pyrefly                Experimental Meta Pyrefly checker (same scope as ty; not part of make test)"
	@echo "  make ruff                   Ruff lint (plugin/, excludes contrib + tests; see pyproject.toml)"
	@echo "  make ruff-fix               Ruff with --fix; make ruff-format-check = ruff format --check"
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
	@$(PYTHON) scripts/fix_uno_import.py -q

fix-uno:
	@$(PYTHON) scripts/fix_uno_import.py

docker-build:
	UID=$$(id -u) GID=$$(id -g) docker compose -f builder/docker-compose.yml up --build
	@echo "Done: build/writeragent.oxt"

auto-translate:
	@echo "Regenerating translation templates (.pot)..."; \
	$(MAKE) extract-strings; \
	$(PYTHON) scripts/translate_missing.py --preview; \
	if [ -n "$$OPENROUTER_API_KEY" ]; then \
		echo "Auto-translating missing strings with AI..."; \
		$(PYTHON) scripts/translate_missing.py --execute --skip-initial-status; \
	fi

refresh-pot:
	@if command -v xgettext >/dev/null 2>&1; then \
		echo "Regenerating translation templates (.pot) without updating .po..."; \
		$(PYTHON) scripts/extract_xdl_strings.py; \
		xgettext --add-location=file -d writeragent -o locales/writeragent.pot $$(find plugin -name "*.py"); \
		$(PYTHON) scripts/merge_module_yaml_into_pot.py locales/writeragent.pot; \
		rm -f plugin/xdl_strings.py; \
	else \
		echo "Skipping .pot regeneration (xgettext not found; install gettext: choco install gettext.install)"; \
	fi

preview-translations: refresh-pot
	$(PYTHON) scripts/translate_missing.py --preview


ifeq ($(USE_DOCKER),1)
build: ty ruff-for-build preview-translations compile-translations
	@$(MAKE) docker-build
else
build: ty ruff-for-build preview-translations vendor manifest compile-translations
	@echo "Building $(EXTENSION_NAME).oxt (with tests)..."
	$(PYTHON) $(SCRIPTS)/build_oxt.py --output build/$(EXTENSION_NAME).oxt $(if $(filter 1,$(NO_RECORDING)),--no-recording)
	@echo "Done: build/$(EXTENSION_NAME).oxt  (bundle in build/bundle/)"
endif

build-no-recording: ty ruff-for-build preview-translations vendor manifest compile-translations
	@echo "Building $(EXTENSION_NAME).oxt (no voice recording)..."
	$(PYTHON) $(SCRIPTS)/build_oxt.py --no-recording --output build/$(EXTENSION_NAME).oxt
	@echo "Done: build/$(EXTENSION_NAME).oxt  (bundle in build/bundle/)"

# Sub-make so ordering holds even with make -j: full test (typecheck + pytest + LO) then release bundle.
# Full verification: typecheck, bandit, then build a stripped bundle with tests
# to verify stripping doesn't break logic, then finally build the clean release oxt.
release: clean
	@$(MAKE) typecheck
	@$(MAKE) bandit
	@echo "Building stripped bundle for verification..."
	$(PYTHON) $(SCRIPTS)/build_oxt.py --strip --output build/test-stripped.oxt
	@echo "Running tests against stripped bundle..."
	@echo "  (grammar_obs call-site tests self-skip via _grammar_obs_call_sites_present; whole modules ignored below)"
	cd build/bundle && PYTHONPATH=. $(abspath $(PYTHON)) -m pytest --ignore=tests/scripts --ignore=tests/test_fix_uno_import.py --ignore=tests/test_merge_module_yaml_into_pot.py --ignore=tests/framework/test_logging.py --ignore=tests/writer/locale/test_grammar_linguistic_xcu.py --ignore=tests/scripting/test_generate_tool_proxies.py --ignore=tests/framework/test_thread_guard.py --ignore=tests/framework/test_thread_affinity.py --ignore=tests/doc/test_specialized_delegation_threading.py --ignore=tests/writer/locale/test_grammar_obs.py -k "not test_sync_tool_marshaled_from_background and not test_execute_on_main_thread_timeout" tests
	cd build/bundle && PYTHONPATH=. $(LO_PYTHON) -m plugin.testing_runner; EXIT_CODE=$$?; $(MAKE) lo-kill; exit $$EXIT_CODE
	@$(MAKE) release-build
	@$(MAKE) register-built-oxt

openrouter-catalog:
	$(PYTHON) scripts/sync_orca_openrouter_catalog.py
	$(PYTHON) -m ruff format plugin/framework/default_models.py

release-build: auto-translate vendor manifest openrouter-catalog compile-translations
	@echo "Building $(EXTENSION_NAME).oxt (release, bundle without tests)..."
	$(PYTHON) $(SCRIPTS)/build_oxt.py --no-tests --output build/$(EXTENSION_NAME).oxt $(if $(filter 1,$(NO_RECORDING)),--no-recording)
	@echo "Done: build/$(EXTENSION_NAME).oxt  (bundle in build/bundle/)"



repack:
	@echo "Re-packing from build/bundle/..."
	$(PYTHON) $(SCRIPTS)/build_oxt.py --repack --output build/$(EXTENSION_NAME).oxt
	@echo "Done: build/$(EXTENSION_NAME).oxt"

repack-deploy: repack register-built-oxt
	@$(if $(SELECTED_COMPONENT),$(MAKE) lo-start-log COMPONENT=$(SELECTED_COMPONENT))

# Stop LibreOffice if running, then unopkg remove + add build/$(EXTENSION_NAME).oxt.
# Does not start LO (use ``make deploy`` with writer/calc/draw/impress, or ``make lo-start``).
register-built-oxt:
	@echo "Registering build/$(EXTENSION_NAME).oxt..."
	$(MAKE) lo-kill
	@rm -f $(LO_CONF)/.lock $(LO_CONF)/user/.lock
	-$(UNOPKG) remove org.extension.writeragent 2>/dev/null
	@rm -f $(LO_CONF)/user/extensions/tmp/extensions.pmap
	@$(RM_RF) "$(LO_CONF)/user/extensions/tmp/extensions/"*.tmp_
	$(UNOPKG) add build/$(EXTENSION_NAME).oxt
	@rm -f $(HOME_DIR)/writeragent.log $(HOME_DIR)/writeragent_agent.log $(HOME_DIR)/writeragent_debug.log
	@rm -f $(LO_DEBUG_LOG) $(LO_CONF)/user/writeragent_debug.log $(LO_CONF)/user/writeragent_agent.log
	@echo "Registered org.extension.writeragent (start LibreOffice manually to load it)."

manifest:
	$(PYTHON) $(SCRIPTS)/generate_manifest.py

manifest-core:
	$(PYTHON) $(SCRIPTS)/generate_manifest.py --modules scripting vision \
		--manifest-output build/generated/_manifest_librepy.py \
		--skip-writeragent-extension --skip-addons

rdb-core:
	$(RUN_SH) $(SCRIPTS)/rebuild_librepy_rdb$(EXT)

build-core: vendor manifest-core rdb-core compile-translations
	@echo "Building LibrePy.oxt (prototype core extension)..."
	$(PYTHON) $(SCRIPTS)/build_librepy_oxt.py --output $(LIBREPY_OXT)
	@echo "Done: $(LIBREPY_OXT)  (bundle in build/bundle-librepy/)"

register-librepy-oxt:
	@echo "Registering $(LIBREPY_OXT)..."
	$(MAKE) lo-kill
	@rm -f $(LO_CONF)/.lock $(LO_CONF)/user/.lock
	-$(UNOPKG) remove $(LIBREPY_EXTENSION_ID) 2>/dev/null
	-$(UNOPKG) remove org.extension.writeragent 2>/dev/null
	@rm -f $(LO_CONF)/user/extensions/tmp/extensions.pmap
	@$(RM_RF) "$(LO_CONF)/user/extensions/tmp/extensions/"*.tmp_
	$(UNOPKG) add $(LIBREPY_OXT)
	@rm -f $(HOME_DIR)/writeragent.log $(HOME_DIR)/writeragent_agent.log $(HOME_DIR)/writeragent_debug.log
	@rm -f $(LO_DEBUG_LOG) $(LO_CONF)/user/writeragent_debug.log $(LO_CONF)/user/writeragent_agent.log
	@echo "Registered $(LIBREPY_EXTENSION_ID) (start LibreOffice manually to load it)."

deploy-core: build-core register-librepy-oxt
	@$(if $(SELECTED_COMPONENT),$(MAKE) lo-start-log COMPONENT=$(SELECTED_COMPONENT))

native:
	cd native/writeragent_vec && $(PYTHON) setup.py build_ext --inplace
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
	$(PYTHON) scripts/update_vec_contrib.py "$(WHEELS_DIR)"

update-vec-fetch:
	$(PYTHON) scripts/update_vec_contrib.py --fetch

# Convenience target to build with Cython accelerator
build-native: native build

clean-native:
	$(RM_RF) native/writeragent_vec/build
	$(RM_RF) native/writeragent_vec/src/writeragent_vec/*.so
	$(RM_RF) native/writeragent_vec/src/writeragent_vec/*.pyd
	$(RM_RF) native/writeragent_vec/src/writeragent_vec/*.c

proxy-stubs:
	$(PYTHON) scripts/generate_tool_proxies.py > plugin/scripting/writeragent_api.py

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
	@$(MAKE) log

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
		$(PYTHON) scripts/extract_xdl_strings.py; \
		xgettext --add-location=file -d writeragent -o locales/writeragent.pot $$(find plugin -name "*.py"); \
		$(PYTHON) scripts/merge_module_yaml_into_pot.py locales/writeragent.pot; \
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
	$(PYTHON) scripts/translate_missing.py --execute

compile-translations:
	@if command -v msgfmt >/dev/null 2>&1; then \
		find locales -name "*.po" -exec sh -c 'msgfmt -o "$$(dirname $$1)/$$(basename $$1 .po).mo" "$$1"' _ {} \;; \
	else \
		echo "Skipping .mo compilation (msgfmt not found; install gettext: choco install gettext.install)"; \
	fi


# ── Shortcuts ───────────────────────────────────────────────────────────────

lo-restart:
	$(MAKE) lo-kill
	sleep 3
	rm -f $(LO_CONF)/.lock $(LO_CONF)/user/.lock
	$(MAKE) lo-start

deploy: dev-deploy
	@$(if $(SELECTED_COMPONENT),$(MAKE) lo-start-log COMPONENT=$(SELECTED_COMPONENT))

writer calc draw impress:
	@$(if $(filter deploy repack-deploy,$(MAKECMDGOALS)),,@echo "Stand-alone 'make $@' is disabled. Use 'make deploy $@' to build and launch.")

log:
	@cat $(LO_DEBUG_LOG) 2>/dev/null || echo "No writeragent_debug.log found (expected at $(LO_DEBUG_LOG))"

log-tail:
	@tail -f $(LO_DEBUG_LOG)

lo-log:
	@cat $(HOME_DIR)/soffice-debug.log 2>/dev/null || echo "No soffice-debug.log found"

check-setup:
	$(RUN_SH) $(SCRIPTS)/check-setup$(EXT)

check-ext:
	@$(UNOPKG) list 2>&1 | head -10
	@echo "---"
	@$(PYTHON) -c "from plugin._manifest import MODULES; print('Manifest OK: %d modules, %d with config' % (len(MODULES), len([m for m in MODULES if m.get('config')])))"

# For LO tests: use Python that has uno/officehelper (LibreOffice's Python on Windows;
# otherwise same as "python -m plugin.testing_runner").
# We try to detect one that has the 'uno' module available, falling back to 'python' if none found.
LO_PYTHON ?= $(shell python3 -c "import uno" 2>/dev/null && echo python3 || (python -c "import uno" 2>/dev/null && echo python || echo python))

typecheck: manifest
	@$(MAKE) ty-run
	@$(MAKE) mypy-run
	@$(MAKE) pyright-run

test-run:
	$(PYTHON) -m pytest tests
	@$(MAKE) lo-kill
	$(LO_PYTHON) -m plugin.testing_runner; EXIT_CODE=$$?; $(MAKE) lo-kill; exit $$EXIT_CODE

_SERIALIZATION_EXTENSIVE = WRITERAGENT_SERIALIZATION_EXTENSIVE=1

slowtests:
	@echo "=== [1/2] Serialization contracts + extensive A/B fixtures ==="
	$(_SERIALIZATION_EXTENSIVE) $(PYTHON) -m pytest \
		tests/scripting/test_serialization_verification.py \
		tests/scripting/test_serialization_ab.py -k "not hypothesis" -q
	@echo "=== [2/2] Hypothesis fuzz (vhs) ==="
	@$(MAKE) vhs

vhs:
	@echo "Running Hypothesis serialization fuzz tests with visualization..."
	$(_SERIALIZATION_EXTENSIVE) $(PYTHON) -m pytest tests/scripting/test_serialization_ab.py -k hypothesis -s --hypothesis-verbosity=verbose

test-visible:
	$(LO_PYTHON) -m plugin.testing_runner --visible test_charts_uno test_enhanced_charts_uno test_document_research_grep_uno; EXIT_CODE=$$?; $(MAKE) lo-kill; exit $$EXIT_CODE

lo-test-threadguard:
	WRITERAGENT_UNO_THREAD_GUARD=1 $(LO_PYTHON) -m plugin.testing_runner; EXIT_CODE=$$?; $(MAKE) lo-kill; exit $$EXIT_CODE

lo-test-threadguard-visible:
	WRITERAGENT_UNO_THREAD_GUARD=1 $(LO_PYTHON) -m plugin.testing_runner --visible test_charts_uno test_enhanced_charts_uno test_document_research_grep_uno; EXIT_CODE=$$?; $(MAKE) lo-kill; exit $$EXIT_CODE

opengrep-lint:
	@test -x "$(OPENGREP)" || (echo "opengrep not found — run: make opengrep-install" && exit 1)
	@test -f $(OPENGREP_DIR)/third_party/SOURCES.json || (echo "vendored Opengrep rules missing — run: make opengrep-rules-sync" && exit 1)
	$(OPENGREP_ENV) "$(OPENGREP)" scan $(OPENGREP_SCAN_FLAGS) $(foreach c,$(OPENGREP_CONFIGS),-c $(c)) plugin

uno-thread-lint: opengrep-lint

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

test:
	@$(MAKE) typecheck
	@$(MAKE) opengrep-lint
	@$(MAKE) test-run
	@$(MAKE) bandit

CROSSHAIR_MODULE = plugin/scripting/payload_codec.py

verify-serialization:
	@echo "=== Pytest oracles ==="
	$(PYTHON) -m pytest tests/scripting/test_serialization_verification.py -k "not crosshair" -q
	@echo "=== CrossHair check (full module, live filtered) ==="
	$(MAKE) crosshair-check

test-serialization-ab:
	$(_SERIALIZATION_EXTENSIVE) $(PYTHON) -m pytest tests/scripting/test_serialization_ab.py -q

# CrossHair on entire module files (correctness over speed; see docs/formal_verification.md)
crosshair-check:
	.venv/bin/crosshair check -v --report_all $(CROSSHAIR_MODULE) 2>&1 | $(PYTHON) scripts/crosshair_stream.py check

crosshair-cover:
	.venv/bin/crosshair cover -v $(CROSSHAIR_MODULE) 2>&1 | $(PYTHON) scripts/crosshair_stream.py cover

# ── Benchmarks (scripts/prompt_optimization) ─────────────────────────────────

PO_EVAL_REQ := scripts/prompt_optimization/requirements.txt
EVAL_ARGS ?=

eval-deps:
	uv pip install -r $(PO_EVAL_REQ)

run_eval:
	$(PYTHON) scripts/benchmark.py $(EVAL_ARGS)

run_eval-smoke:
	$(MAKE) run_eval EVAL_ARGS="--models qwen/qwen3-coder-next -n 1 -j 1"

# ── POC extension ───────────────────────────────────────────────────────────

set-config:
	@echo "Usage: make deploy WRITERAGENT_SET_CONFIG=\"mcp.port=9000,mcp.host=0.0.0.0\""
	@echo ""
	@echo "Available config keys (module.key = default):"
	@$(PYTHON) -c "from plugin._manifest import MODULES; \
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
	@rm -f $(LO_CONF)/.lock $(LO_CONF)/user/.lock
	@rm -f $(HOME_DIR)/poc-ext.log
	$(MAKE) lo-start
	@echo "Waiting for LO..."
	@sleep 10
	@$(MAKE) poc-log

check: ty

ty: manifest ty-run
mypy: manifest mypy-run
pyright: manifest pyright-run
pyrefly: manifest pyrefly-run

ty-run: ensure-uno
	$(PYTHON) -m ty check --exclude plugin/contrib/ --exclude plugin/lib/

mypy-run: ensure-uno
	$(PYTHON) -m mypy

pyright-run: ensure-uno
	$(PYTHON) -m pyright

pyrefly-run: ensure-uno
	$(PYTHON) -m pyrefly check

bandit:
	$(PYTHON) -m bandit -r plugin -c pyproject.toml --severity-level medium

ruff:
	$(PYTHON) -m ruff check plugin

ruff-fix:
	$(PYTHON) -m ruff check plugin --fix

# Build gate: auto-fix then verify (standalone `make ruff` remains check-only).
ruff-for-build: ruff-fix ruff

ruff-format-check:
	$(PYTHON) -m ruff format --check plugin

# Grammar proofreader: formatting this file only is faster than `ruff format plugin`.
ruff-format-grammar:
	$(PYTHON) -m ruff format plugin/writer/locale/ai_grammar_proofreader.py
