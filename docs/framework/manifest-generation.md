# Manifest Generation

This document describes how extension manifests and dialog definitions are generated from module metadata.

## Overview

The build target `make manifest` (`scripts/generate_manifest.py`) scans plugin module declarations and generates the runtime module manifest and dialog files.

For LibrePy (`make manifest-core`), a slim manifest is produced containing only the core scripting and vision modules (`--modules scripting vision --skip-writeragent-extension`).

## Inputs

- **Module declarations:** `plugin/**/module.yaml` for each individual module.
- **Framework configuration:** `plugin/plugin.yaml` (provides framework actions and `main` module).
- **Settings dialog template:** `extension/Dialogs/SettingsDialog.xdl.tpl`.
- **Dialog library catalog template:** `extension/Dialogs/dialog.xlb.tpl`.
- **Extension description template:** `extension/description.xml.tpl`.

## Outputs

- `plugin/_manifest.py`: Runtime module list (`MODULES`), used by `config_schema.py` and bootstrap.
- `build/generated/Dialogs/SettingsDialog.xdl`: Splice of static tabs and auto-generated settings tabs/pages.
- `build/generated/Dialogs/*.xdl`: Standalone dialog definitions (e.g. `VisionSettingsDialog.xdl`).
- `extension/Dialogs/dialog.xlb`: Catalog of dialog IDs updated to include standalone dialogs.
- `extension/description.xml`: Extension description containing current version from `plugin/version.py`.
- `extension/META-INF/manifest.xml`: UNO component registration entries (WriterAgent build only).

## Settings Tabs and Saved Fields

Settings tabs and saved fields come from `iter_settings_tab_modules` / `iter_settings_field_modules`. Exclusions are `core`, `main`, `ai`, `launcher`, `config_inline` children, `settings_tab: false`, and `config_dialog`. Field specs are the tab modules plus inline children of those tabs. Standalone dialogs do not use this list.

