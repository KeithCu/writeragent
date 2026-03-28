# WriterAgent localization (i18n)

This document describes how translated UI strings are produced, loaded at runtime, and maintained. It replaces the older split between a “plan” and a short overview.

## What ships today

- **GNU gettext** (`.pot` template, per-locale `writeragent.po`, compiled `writeragent.mo` under `plugin/locales/<lang>/LC_MESSAGES/`).
- **Runtime loading** in [`plugin/framework/i18n.py`](../plugin/framework/i18n.py): `init_i18n()` installs a `gettext.translation` for domain `writeragent`; user code wraps strings with `_()`.
- **Locale source**: LibreOffice’s UI locale from configuration path `/org.openoffice.Setup/L10N` → property `ooLocale` (see `get_lo_locale()`). Values like `en-US` are normalized to gettext-style `en_US`. If lookup fails, the code falls back to `en_US` so behavior stays predictable in tests or early init.
- **Missing strings**: `fallback=True` on the translation object means untranslated or absent catalogs show the English `msgid`.

There is **no separate “UI language” override in `writeragent.json`** today: displayed language follows LibreOffice’s UI locale plus whichever `.mo` files are packaged for that locale.

## How strings get into the template

1. **Python**: User-visible literals should use `_('...')` (or the same pattern with format strings). After `init_i18n()`, `_()` delegates to gettext.

2. **XDL dialogs**: English strings in `.xdl` files are not read by `xgettext` directly. [`scripts/extract_xdl_strings.py`](../scripts/extract_xdl_strings.py) generates a temporary `plugin/xdl_strings.py` containing `_()` calls so those strings are picked up; that stub is removed after extraction.

3. **Module metadata**: [`scripts/merge_module_yaml_into_pot.py`](../scripts/merge_module_yaml_into_pot.py) merges translatable entries from `plugin/modules/**/module.yaml` (titles, labels, options) into the same POT, deduplicated by `msgid`. Requires `polib` and PyYAML.

4. **Dialogs at runtime**: [`translate_dialog` in `plugin/framework/dialogs.py`](../plugin/framework/dialogs.py) walks controls and applies translated text. **Do not** pass raw saved config values through `_()` in [`legacy_ui.py`](../plugin/framework/legacy_ui.py): empty strings can pick up gettext header garbage. Config validation strips bogus gettext headers; see [`plugin/tests/test_i18n.py`](../plugin/tests/test_i18n.py).

## Build and maintenance commands

| Target | Role |
|--------|------|
| `make extract-strings` | Full pipeline: XDL stub → `xgettext` → YAML merge → **delete stub** → **`merge-translations`** on every `writeragent.po`. Use when sources change and you want POT and all POs updated. |
| `make refresh-pot` | Regenerates `writeragent.pot` only (same extraction steps) **without** merging into `.po` files. |
| `make preview-translations` | `refresh-pot` then `scripts/translate_missing.py --preview` (status table of completion vs POT). Used by normal `make build`. |
| `make merge-translations` | For each `writeragent.po`: `msgmerge --update` from `writeragent.pot`, then `msgattrib --no-obsolete` so removed strings do not linger as obsolete entries. |
| `make compile-translations` | `msgfmt` every `.po` to `.mo` (required for LibreOffice to load catalogs efficiently). |
| `make add-language LANG=xx` | Creates `plugin/locales/xx/LC_MESSAGES/writeragent.po` from the POT and compiles an initial `.mo`. |

**Note:** `make build` runs `preview-translations` (refresh POT + preview), not necessarily the full `extract-strings` + merge. Run **`make extract-strings`** when you add or change marked strings and need all locale files updated from the new template.

**Release / AI assist:** `make release-build` uses `auto-translate`, which regenerates strings with `extract-strings`, previews, then (if `OPENROUTER_API_KEY` is set) runs `translate_missing.py --execute` to fill gaps. Manual runs: `make translate-missing` or `python scripts/translate_missing.py --execute`.

## AI-assisted gap filling (`scripts/translate_missing.py`)

Optional automation for translators:

- Compares each locale’s `writeragent.po` to `writeragent.pot` and finds missing, empty, or **fuzzy** entries.
- **`--preview`** prints the **completion table** (POT totals, pending, done, percentage) and exits; used by `make preview-translations` / `make build`.
- With **`--execute`**, prints that same table at start unless `--skip-initial-status`, then fills gaps via the API.
- Sends strings to an OpenAI-compatible chat API (default model `x-ai/grok-4.1-fast`, default endpoint OpenRouter) in batches; uses project auth helpers / `writeragent.json` keys / `OPENROUTER_API_KEY` when available.
- Preserves leading/trailing whitespace on strings by peeling it before the API call and re-applying it to results.

This is a productivity aid, not a substitute for human review of tone and terminology.

## Extension menus and registry (`.xcu`)

LibreOffice supports localized properties with `<value xml:lang="...">`. The project’s `.xcu` files use this shape but **mostly only define `en-US` / `en` today**. Adding more languages is a matter of supplying parallel `<value xml:lang="de">` (etc.) entries for the same properties—no Python change required for those labels.

## LLM prompts vs UI language

A practical policy (from earlier design notes, still relevant):

- Keep **system/tool instructions** in English when that improves model comprehension (especially for smaller or local models).
- Add or keep a **clear instruction** that conversational replies to the user should match the user’s language when that differs from the prompt language.
- Users can still override behavior with **additional instructions** in Settings.

This is separate from gettext: gettext handles fixed UI strings; the model handles free-form chat language.

## Future considerations (not implemented as a tracked roadmap)

These are useful directions, not commitments:

- **Pluralization**: Expose `ngettext` (and possibly `npgettext`) from `i18n.py` anywhere English uses “one vs many” or per-locale plural rules.
- **Message context**: Use `pgettext` / `msgctxt` where the same English word must translate differently (e.g. “Table” in Writer vs Calc).
- **RTL locales**: Arabic, Hebrew, etc. may need layout or UNO adjustments beyond string translation; test dialogs and sidebar with RTL UI.
- **Automated checks**: Optional tests or lint rules that flag unwrapped user strings in high-traffic UI modules; CI jobs that run `extract-strings` and fail on accidental POT drift if desired.
- **Translation platform**: Weblate, Crowdin, or Transifex if community scale grows; export `writeragent.pot` as the handoff artifact.
- **String freeze**: Before a release aimed at translators, freeze `msgid` churn to reduce merge noise.
- **Coverage reporting**: Extend or reuse the logic in `translate_missing.py`’s status output to publish a simple per-language table in docs or CI logs.

For day-to-day contributor steps (extract, merge, Poedit, compile), see [`plugin/locales/README.md`](../plugin/locales/README.md).
