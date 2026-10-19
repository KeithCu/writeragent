# OXT Packaging & Assembly

This document outlines the extension packaging pipeline for WriterAgent (`scripts/build_oxt.py`), LibrePy (`scripts/build_librepy_oxt.py`), and LibreHarper (`scripts/build_libreharper_oxt.py`).

## Required Inputs and Make Targets

The build pipeline performs preflight checks to fail immediately if required artifacts are missing:

| Required Path | Make Target | Purpose |
|---------------|-------------|---------|
| `plugin/_manifest.py`, `plugin/main.py` | `make manifest` | Python manifest listing modules, tools, and actions |
| `extension/META-INF/manifest.xml`, `extension/description.xml` | `make manifest` | Extension metadata and UNO component registrations |
| `extension/Addons.xcu`, `extension/Accelerators.xcu`, `extension/Jobs.xcu`, `extension/ProtocolHandler.xcu` | `make manifest` (generated) / committed | UI menus, keyboard shortcuts, job hooks, and protocol dispatch |
| `extension/XPythonFunction.rdb`, `extension/XPromptFunction.rdb` | Committed (`make rdb-core` for LibrePy) | UNO type libraries |
| `build/generated/Dialogs/SettingsDialog.xdl` | `make manifest` | Generated settings dialog layout |
| `locales/*/*.mo` | `make compile-translations` | Compiled gettext catalogs |
| `vendor/` package trees + `latex2mathml-*.dist-info/METADATA` | `make vendor` | Vendored Python packages |

Missing inputs abort the build with a nonzero exit code naming the make target to run.

## Hand-Maintained XCU Rule

- **`Addons.xcu`**: The hand-maintained `extension/Addons.xcu` contains the `OfficeToolBar` definition that generated XCUs do not emit. The generated `build/generated/Addons.xcu` is not packaged. In release builds (`--no-tests`), the Debug menu (`M17`) is stripped from `Addons.xcu`.
- **`Accelerators.xcu`**: `extension/Accelerators.xcu` uses production `service:org.extension.writeragent.Main?...` URLs rather than `protocol:module.action` URLs and takes precedence over `build/generated/Accelerators.xcu`.

If two source files remap to the same archive path with differing bytes, assembly halts with an archive collision error. Identical duplicates collapse to a single entry.

## Vendor & `plugin/lib/` Sync

- `sync_vendor_into_lib()` copies vendor packages into `plugin/lib/` (hot-deploy path) and the OXT bundle directory (`build/bundle/plugin/lib/`).
- `bin/` directories are never copied (they contain host machine-local shebangs).
- `*.dist-info/METADATA` is preserved for packages that inspect their own version at import (such as `latex2mathml`).
- Stale packages in `lib_dir` are deleted after syncing so removed vendor dependencies do not persist across deployments. Windows cache deploy lacks a vendor overlay and Linux rsync skips `*.dist-info`.

## Atomic Zip & Deterministic Entries

`write_oxt_zip()` produces deterministic and safe zip archives:
- Entry filenames are sorted alphabetically.
- Timestamps are normalized to `SOURCE_DATE_EPOCH` (or clamped to `1980-01-01`).
- Archive is written to a temporary sibling file (`<output>.<pid>.tmp`) and atomically moved to the destination via `os.replace` after closing. A failed build never corrupts or deletes a previous good `.oxt`.
- Verifies that every file declared in `META-INF/manifest.xml` is present in the bundle before zipping.
