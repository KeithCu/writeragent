# Localization (i18n)

WriterAgent uses standard Python `gettext` for localizing its user interface, including dynamically translated LibreOffice menus and custom dialogs. This guide explains how to generate, update, and compile translations.

## Adding or Updating Translations

Translations are stored in the `plugin/locales/` directory.

### 1. Extract Strings
To generate a new `.pot` (Portable Object Template) file from the source code, run the standard `xgettext` tool from the repository root:

```bash
xgettext -d writeragent -o plugin/locales/writeragent.pot $(find plugin -name "*.py")
```

### 2. Create a New Language File
To start translating a new language (e.g., German - `de`), create the directory structure and copy the `.pot` file to a `.po` file:

```bash
mkdir -p plugin/locales/de/LC_MESSAGES/
cp plugin/locales/writeragent.pot plugin/locales/de/LC_MESSAGES/writeragent.po
```

### 3. Translate
Open the `writeragent.po` file in a text editor or a tool like [Poedit](https://poedit.net/) and translate the `msgid` strings into `msgstr`.

### 4. Compile Translations
LibreOffice's Python runtime requires the compiled binary `.mo` files to load translations efficiently. Compile your `.po` file using `msgfmt`:

```bash
msgfmt -o plugin/locales/de/LC_MESSAGES/writeragent.mo plugin/locales/de/LC_MESSAGES/writeragent.po
```

The resulting `writeragent.mo` file will be packaged inside the LibreOffice extension (`.oxt`) during the build process and loaded automatically based on the user's LibreOffice UI locale setting.
