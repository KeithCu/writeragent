# Makefile — LocalWriter extension build & dev tools.
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


# ── Local overrides (gitignored) ────────────────────────────────────────────
# Create Makefile.local with e.g. USE_DOCKER = 1
-include Makefile.local

# Set NO_RECORDING=1 to build without voice recording (excludes contrib/audio, audio_recorder.py).
NO_RECORDING ?= 0

# Set USE_DOCKER=1 to build via Docker instead of local Python/PyYAML.
# Persistent: echo "USE_DOCKER = 1" > Makefile.local
# One-shot:   make deploy USE_DOCKER=1
USE_DOCKER ?=

# ── OS detection ─────────────────────────────────────────────────────────────

ifeq ($(OS),Windows_NT)
    # Use Git Bash as shell so Unix commands (sleep, rm, cat, tail...) work everywhere.
    # Run install.ps1 to ensure Git for Windows is installed.
    BASH_PATH := $(firstword $(wildcard C:/Program\ Files/Git/usr/bin/bash.exe) $(wildcard C:/Program\ Files/Git/bin/bash.exe))
    ifneq ($(BASH_PATH),)
        SHELL   := $(BASH_PATH)
    endif
    .SHELLFLAGS := -c
    MAKE    := "$(MAKE)"
    SCRIPTS = scripts
    RUN_SH  = powershell -ExecutionPolicy Bypass -File
    EXT     = .ps1
    PYTHON  = python
    RM_RF   = rm -rf
    MKDIR   = mkdir -p
    HOME_DIR = $(subst \,/,$(USERPROFILE))
    LO_CONF = $(HOME_DIR)/AppData/Roaming/LibreOffice/4
else
    SCRIPTS = scripts
    RUN_SH  = bash
    EXT     = .sh
    PYTHON  = python
    RM_RF   = rm -rf
    MKDIR   = mkdir -p
    LO_CONF = $(HOME)/.config/libreoffice/4
    HOME_DIR = $(HOME)
endif

# Prefer project .venv so "make test" uses venv even when shell isn't activated
ifneq ($(wildcard .venv/bin/python),)
    PYTHON := .venv/bin/python
endif
ifeq ($(OS),Windows_NT)
ifneq ($(wildcard .venv/Scripts/python.exe),)
    PYTHON := .venv/Scripts/python.exe
endif
endif

# ── Phony targets ────────────────────────────────────────────────────────────

.PHONY: help build build-no-recording release release-build repack repack-deploy manifest xcu clean \
        install install-force uninstall cache \
        dev-deploy dev-deploy-remove \
        lo-start lo-start-full lo-kill lo-restart \
        clean-cache nuke-cache nuke-cache-force unbundle \
        log log-tail lo-log test test-run typecheck check-ext check-setup deploy \
        set-config vendor docker-build compile-translations merge-translations refresh-pot preview-translations check ty mypy pyright bandit ty-run mypy-run pyright-run \
        ruff ruff-fix ruff-format-check

# ── Help ─────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "WriterAgent — build & dev targets"
	@echo "================================="
	@echo ""
	@echo "Build:"
	@echo "  make build                  Build .oxt with plugin/tests (runs ty + ruff, then gettext/UI steps)"
	@echo "  make release                Run make test first, then build .oxt without bundled tests"
	@echo "  make build-no-recording     Build .oxt without voice recording (no contrib/audio, no Record button)"
	@echo "  make xcu                    Generate XCS/XCU from config schemas"
	@echo "  make clean                  Remove build artifacts"
	@echo ""
	@echo "Install:"
	@echo "  make deploy                 Build + reinstall + restart LO + show log"
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
	@echo "  make lo-start               Launch with debug logging"
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
	@echo "  make check-ext              Verify extension is registered"
	@echo "  make set-config             List all config keys"
	@echo "  make test                   Run ty, mypy, pyright, bandit, then pytest + in-process LO tests"
	@echo "  make test-run               Pytest + LO tests only (skip typecheck/bandit; for quick reruns)"
	@echo "  make typecheck              Run ty, then mypy, then pyright (same scope as each single target)"
	@echo "  make check                  Quick gate: ty only (also used implicitly before fast workflows)"
	@echo "  make fix-uno                Fix uno import in .venv (adds system UNO paths to .pth)"
	@echo "  make mypy / make pyright / make bandit   Single-tool runs (bandit: plugin/, excludes contrib + tests)"
	@echo "  make ruff                   Ruff lint (plugin/, excludes contrib + tests; see pyproject.toml)"
	@echo "  make ruff-fix               Ruff with --fix; make ruff-format-check = ruff format --check"
	@echo ""

# ── Build ────────────────────────────────────────────────────────────────────

vendor:
	uv pip install --target vendor -r requirements-vendor.txt

fix-uno:
	@echo "Fixing UNO import in .venv..."
	@$(PYTHON) scripts/fix_uno_import.py

docker-build:
	UID=$$(id -u) GID=$$(id -g) docker compose -f builder/docker-compose.yml up --build
	@echo "Done: build/localwriter.oxt"

auto-translate:
	@echo "Regenerating translation templates (.pot)..."; \
	$(MAKE) extract-strings; \
	$(PYTHON) scripts/translate_missing.py --preview; \
	if [ -n "$$OPENROUTER_API_KEY" ]; then \
		echo "Auto-translating missing strings with AI..."; \
		$(PYTHON) scripts/translate_missing.py --execute --skip-initial-status; \
	fi

refresh-pot:
	@echo "Regenerating translation templates (.pot) without updating .po..."; \
	$(PYTHON) scripts/extract_xdl_strings.py; \
	xgettext -d writeragent -o plugin/locales/writeragent.pot $$(find plugin -name "*.py"); \
	$(PYTHON) scripts/merge_module_yaml_into_pot.py plugin/locales/writeragent.pot; \
	rm -f plugin/xdl_strings.py

preview-translations: refresh-pot
	$(PYTHON) scripts/translate_missing.py --preview


ifeq ($(USE_DOCKER),1)
build: ty ruff preview-translations compile-translations
	@$(MAKE) docker-build
else
build: ty ruff preview-translations vendor manifest compile-translations
	@echo "Building $(EXTENSION_NAME).oxt (with tests)..."
	$(PYTHON) $(SCRIPTS)/build_oxt.py --output build/$(EXTENSION_NAME).oxt $(if $(filter 1,$(NO_RECORDING)),--no-recording)
	@echo "Done: build/$(EXTENSION_NAME).oxt  (bundle in build/bundle/)"
endif

build-no-recording: ty ruff preview-translations vendor manifest compile-translations
	@echo "Building $(EXTENSION_NAME).oxt (no voice recording)..."
	$(PYTHON) $(SCRIPTS)/build_oxt.py --no-recording --output build/$(EXTENSION_NAME).oxt
	@echo "Done: build/$(EXTENSION_NAME).oxt  (bundle in build/bundle/)"

# Sub-make so ordering holds even with make -j: full test (typecheck + pytest + LO) then release bundle.
release:
	@$(MAKE) test
	@$(MAKE) release-build

release-build: auto-translate vendor manifest compile-translations
	@echo "Building $(EXTENSION_NAME).oxt (release, bundle without plugin/tests)..."
	$(PYTHON) $(SCRIPTS)/build_oxt.py --no-tests --output build/$(EXTENSION_NAME).oxt $(if $(filter 1,$(NO_RECORDING)),--no-recording)
	@echo "Done: build/$(EXTENSION_NAME).oxt  (bundle in build/bundle/)"



repack:
	@echo "Re-packing from build/bundle/..."
	$(PYTHON) $(SCRIPTS)/build_oxt.py --repack --output build/$(EXTENSION_NAME).oxt
	@echo "Done: build/$(EXTENSION_NAME).oxt"

repack-deploy: repack
	$(MAKE) lo-kill
	@sleep 3
	@rm -f $(LO_CONF)/.lock $(LO_CONF)/user/.lock
	-unopkg remove org.extension.writeragent 2>/dev/null; sleep 1
	unopkg add build/$(EXTENSION_NAME).oxt
	@rm -f $(HOME_DIR)/writeragent.log
	#@sleep 1
	$(MAKE) lo-start
	@echo "Waiting for LO to load..."
	@sleep 12
	@$(MAKE) log

manifest:
	$(PYTHON) $(SCRIPTS)/generate_manifest.py

xcu: manifest

clean:
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
	WRITERAGENT_SET_CONFIG="$(WRITERAGENT_SET_CONFIG)" $(RUN_SH) $(SCRIPTS)/launch-lo-debug$(EXT)

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
	rm -f "$(LO_CONF)/.lock"

# ── Translation ──────────────────────────────────────────────────────────────
extract-strings:
	$(PYTHON) scripts/extract_xdl_strings.py
	xgettext -d writeragent -o plugin/locales/writeragent.pot $$(find plugin -name "*.py")
	$(PYTHON) scripts/merge_module_yaml_into_pot.py plugin/locales/writeragent.pot
	rm -f plugin/xdl_strings.py
	$(MAKE) merge-translations

# Merge each locale .po with writeragent.pot, then strip obsolete entries (#~) so removed
# source strings do not accumulate. (msgattrib --no-obsolete: portable where msgmerge lacks --no-obsolete.)
merge-translations:
	find plugin/locales -name writeragent.po -exec sh -c 'f="$$1"; msgmerge --update --backup=none "$$f" plugin/locales/writeragent.pot && msgattrib --no-obsolete -o "$$f.tmp" "$$f" && mv -f "$$f.tmp" "$$f"' _ {} \;


add-language:
	mkdir -p plugin/locales/$(LANG)/LC_MESSAGES
	cp plugin/locales/writeragent.pot plugin/locales/$(LANG)/LC_MESSAGES/writeragent.po
	msgfmt -o plugin/locales/$(LANG)/LC_MESSAGES/writeragent.mo plugin/locales/$(LANG)/LC_MESSAGES/writeragent.po

translate-missing:
	$(PYTHON) scripts/translate_missing.py --execute

compile-translations:
	find plugin/locales -name "*.po" -exec sh -c 'msgfmt -o "$$(dirname $$1)/$$(basename $$1 .po).mo" "$$1"' _ {} \;


# ── Shortcuts ───────────────────────────────────────────────────────────────

lo-restart:
	$(MAKE) lo-kill
	sleep 3
	rm -f $(LO_CONF)/.lock $(LO_CONF)/user/.lock
	$(MAKE) lo-start

deploy: build
	$(MAKE) lo-kill
	#@sleep 3
	@rm -f $(LO_CONF)/.lock $(LO_CONF)/user/.lock
	-unopkg remove org.extension.writeragent 2>/dev/null
	unopkg add build/$(EXTENSION_NAME).oxt
	@rm -f $(HOME_DIR)/writeragent.log
	#@sleep 1
	$(MAKE) lo-start
	@echo "Waiting for LO to load..."
	@sleep 12
	@$(MAKE) log

log:
	@cat $(HOME_DIR)/writeragent.log 2>/dev/null || echo "No writeragent.log found"

log-tail:
	@tail -f $(HOME_DIR)/writeragent.log

lo-log:
	@cat $(HOME_DIR)/soffice-debug.log 2>/dev/null || echo "No soffice-debug.log found"

check-setup:
	$(RUN_SH) $(SCRIPTS)/check-setup$(EXT)

check-ext:
	@unopkg list 2>&1 | head -10
	@echo "---"
	@$(PYTHON) -c "from plugin._manifest import MODULES; print('Manifest OK: %d modules, %d with config' % (len(MODULES), len([m for m in MODULES if m.get('config')])))"

# For LO tests: use Python that has uno (same as "python -m plugin.testing_runner").
# We try to detect one that has the 'uno' module available, falling back to 'python' if none found.
LO_PYTHON ?= $(shell python3 -c "import uno" 2>/dev/null && echo python3 || (python -c "import uno" 2>/dev/null && echo python || echo python))

typecheck: manifest
	@$(MAKE) ty-run
	@$(MAKE) mypy-run
	@$(MAKE) pyright-run

test-run:
	$(PYTHON) -m pytest plugin/tests
	$(LO_PYTHON) -m plugin.testing_runner

test:
	@$(MAKE) typecheck
	@$(MAKE) bandit
	@$(MAKE) test-run

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
	-unopkg remove org.extension.poc 2>/dev/null
	sleep 2
	unopkg add build/poc-ext.oxt
	@echo "POC installed"

poc-uninstall:
	-unopkg remove org.extension.poc 2>/dev/null
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

ty-run:
	@$(PYTHON) -c "import uno" 2>/dev/null || $(MAKE) fix-uno
	$(PYTHON) -m ty check --exclude plugin/contrib/

mypy-run:
	@$(PYTHON) -c "import uno" 2>/dev/null || $(MAKE) fix-uno
	$(PYTHON) -m mypy

pyright-run:
	@$(PYTHON) -c "import uno" 2>/dev/null || $(MAKE) fix-uno
	$(PYTHON) -m pyright

bandit:
	$(PYTHON) -m bandit -r plugin -c pyproject.toml --severity-level medium

ruff:
	$(PYTHON) -m ruff check plugin

ruff-fix:
	$(PYTHON) -m ruff check plugin --fix

ruff-format-check:
	$(PYTHON) -m ruff format --check plugin
