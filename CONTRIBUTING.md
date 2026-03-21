## Adding Translations

WriterAgent supports localization through standard `gettext` integration. You can easily add new languages and contribute translations to the user interface.

### Translation Workflow

1. **Extract strings**:
   If you have added new features or strings wrapped with `_()`, extract them into the template `.pot` file:
   ```bash
   make extract-strings
   ```

2. **Add a new language**:
   To initialize translation files for a new language (e.g., `es` for Spanish), run:
   ```bash
   make add-language LANG=es
   ```
   This will create a `.po` file in `plugin/locales/es/LC_MESSAGES/writeragent.po`.

3. **Translate**:
   Open `plugin/locales/es/LC_MESSAGES/writeragent.po` and translate the empty `msgstr` strings corresponding to each `msgid`.
   You can edit these files manually or using tools like [Poedit](https://poedit.net/).

4. **Compile translations**:
   Before running the extension or testing the changes, compile the updated `.po` files to `.mo`:
   ```bash
   make compile-translations
   ```

5. **Test**:
   To test your translations, either switch the WriterAgent UI language in the plugin's `Settings` dialog, or run LibreOffice with your target locale, e.g.:
   ```bash
   LANG=es_ES.utf8 make deploy
   ```

6. **Submit PR**:
   When submitting a Pull Request, please make sure to include both the updated source `.po` files and compiled `.mo` files in your commit.
