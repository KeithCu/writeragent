# Localization Plan for LocalWriter

This document outlines a high-level plan for adding multi-language support (localization/l10n and internationalization/i18n) to the LocalWriter extension.

## 1. Extension Metadata & Menus (`Addons.xcu`)
The LibreOffice menu integration is defined natively in the XML configuration files (`Addons.xcu`).
- **Approach:** LibreOffice supports localized child elements (`<value xml:lang="...">`) mapped to properties in `.xcu` files.
- **Action:** Add `xml:lang` entries for each supported locale for every menu entry (e.g., "Chat with Document", "Edit Selection", "LocalWriter Settings"). LibreOffice will automatically show the correct one based on the user's UI language.

## 2. Detecting the User's Locale
The Python backend needs to determine the current UI language of the LibreOffice instance to serve the correct translations for dialogs, errors, and system prompts.
- **Approach:** Query the LibreOffice configuration via the UNO API.
- **Action:** Create a utility function (e.g., in `core/config.py` or a new `core/i18n.py`) that reads the `/org.openoffice.Setup/L10N` property `ooLocale` (which returns strings like `"en-US"`, `"de"`, `"fr"`).

## 3. Translating Python Strings and UI Elements
Any strings surfaced to the user from Python code (error messages in `core/api.py`, sidebar panel labels like "Send" and "Stop" in `chat_panel.py`) must be wrapped in a translation function commonly named `_()`.

### Option A: Simple Dictionary File (JSON)
Load a `locales.json` file mapping keys to nested locale objects or vice versa.
- **Pros:** Very simple to implement, zero standard library overhead, easily readable and editable by non-developers, requires no compilation. It is a very simple structure for a small number of strings.
- **Cons:** No built-in pluralization, no standard translation tooling support. You have to write a custom string formatter if variables need to be injected into strings.

### Option B: Python's `gettext` Module (Recommended)
Use GNU's `gettext` format (`.po`/`.mo` files), which is Python's standard library solution (`import gettext`).
- **Pros:** Industry standard, supports complex pluralization rules natively, and many mature open-source tools for translators (e.g., Poedit, Weblate, Crowdin) can extract strings from your source code automatically and help manage translations.
- **Cons:** Requires a compilation step (`.po` -> `.mo`) during the `build.sh` script, slightly more complex initial setup.

**Difference & Recommendation:** A JSON dictionary is basically just a basic key-value lookup you'd implement yourself. `gettext` is a robust system designed specifically for software translation. Given that you may want community contributions for translations, `gettext` is highly recommended because you can just drop a `.pot` file onto a translation platform and the community can handle the rest.

## 4. Dialog Localization (`.xdl` Files)
Both `SettingsDialog` and `EditInputDialog` use XDL layouts which have hardcoded labels. While LibreOffice *does* support parallel `.default` locale files for `.xdl`, managing them programmatically is often easier for Python extensions.
- **Approach:** Programmatically inject translated strings during dialog initialization.
- **Action:** In the dialog creation logic (`MainJob.__init__` or similar), loop through the controls (e.g., labels, button text, frames) by their IDs and set their `Model.Label` or `Model.Text` properties using your `_()` translation function before displaying the dialog.

## 5. AI System Prompts and Output Language
A unique aspect of an AI extension is ensuring the LLM understands its role and replies in the correct language.
- **The Challenge:** Complex instructions (like the detailed descriptions of Calc and Writer tools) are often better understood by local or smaller LLMs if they are written in English.
- **Action / Policy:** 
    1. Keep the base system prompts (e.g., tool schemas, formatting instructions) in English.
    2. Dynamically append a strong localized directive to the end of the system prompt (e.g., `"IMPORTANT: All your conversational responses to the user MUST be in {user_language}."`).
    3. Allow users to override this behavior in the Settings. If a user natively speaks French and prefers to rewrite the system instructions fully in French within their "Additional Instructions" box, they can do so.

## 6. Implementation Steps
1. **Infrastructure:** Add `core/i18n.py` to get the locale from UNO, and setup the `gettext` (or JSON) loading logic. Expose the `_()` macro.
2. **Tagging:** Go through `core/api.py`, `chat_panel.py`, `main.py` and wrap user-facing strings in the `_("Text")` function.
3. **Translation Files:** Generate the base translation file (e.g., `messages.pot`), and perhaps create the first translation (e.g., Spanish or German) to test the pipeline. Update `build.sh` to compile `.po` to `.mo` if using `gettext`.
4. **Dialogs:** Update the dialog initialization code to read from the translation function and overwrite the English defaults in the `.xdl` files.
5. **Menu:** Manually update `Addons.xcu` with supported languages.
6. **Prompts:** Inject the language directive into the `get_chat_system_prompt_for_document` logic based on the detected `ooLocale`.
