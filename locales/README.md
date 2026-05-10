# Localization (i18n)

WriterAgent uses standard Python `gettext` for localizing its user interface, including dynamically translated LibreOffice menus and custom dialogs. This guide explains how to generate, update, and compile translations.

## Adding or Updating Translations

Translations are stored in the repository-root `locales/` directory (sibling of `plugin/`).

### 1. Extract Strings

From the repository root, use **`make extract-strings`**. Order matters:

1. **`scripts/extract_xdl_strings.py`** — writes a temporary `plugin/xdl_strings.py` with `_()` lines extracted from XDL dialogs so `xgettext` can pick them up.
2. **`xgettext`** — scans all `plugin/**/*.py` (including that stub) and writes `locales/writeragent.pot`.
3. **`scripts/merge_module_yaml_into_pot.py`** — merges translatable strings from `plugin/**/module.yaml` (module titles, config labels, helpers, option labels) into the same POT using **polib**, deduplicated by `msgid`. Requires `polib` and `PyYAML` (see `pyproject.toml`). Idempotent: re-running does not duplicate entries.
4. **Removes** the temporary `plugin/xdl_strings.py`.

Do not run only `xgettext` on `*.py` if you need XDL and YAML strings; use `make extract-strings`.

### 1b. Update Existing `.po` Files After a POT Change

After regenerating `writeragent.pot`, merge new or changed template entries into each locale file (preserves existing translations):

```bash
msgmerge --update locales/de/LC_MESSAGES/writeragent.po locales/writeragent.pot
```

Repeat for each language directory under `locales/`, or use Poedit’s “Update from POT file” on each `writeragent.po`.

### 2. Create a New Language File
To start translating a new language (e.g., German - `de`), create the directory structure and copy the `.pot` file to a `.po` file:

```bash
mkdir -p locales/de/LC_MESSAGES/
cp locales/writeragent.pot locales/de/LC_MESSAGES/writeragent.po
```

### 3. Translate
Open the `writeragent.po` file in a text editor or a tool like [Poedit](https://poedit.net/) and translate the `msgid` strings into `msgstr`.

### 4. Compile Translations
LibreOffice's Python runtime requires the compiled binary `.mo` files to load translations efficiently. Compile your `.po` file using `msgfmt`:

```bash
msgfmt -o locales/de/LC_MESSAGES/writeragent.mo locales/de/LC_MESSAGES/writeragent.po
```

The resulting `writeragent.mo` file will be packaged inside the LibreOffice extension (`.oxt`) during the build process and loaded automatically based on the user's LibreOffice UI locale setting.
