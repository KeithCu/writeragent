# OS Secret Storage (`keystore.py`)

WriterAgent stores LLM API keys (`api_keys_by_endpoint`) and `widget: password` module settings (such as `mcp.tunnel_provider_token`) in the platform's native credential store when available, keeping only a reference token in `writeragent.json`.

Implementation: [`plugin/framework/keystore.py`](../../plugin/framework/keystore.py), wired through [`plugin/framework/config.py`](../../plugin/framework/config.py) and [`plugin/framework/config_schema.py`](../../plugin/framework/config_schema.py).

## Backends

All items share the service name `"WriterAgent"`.

| Platform | Backend | Library / API | Label |
|---|---|---|---|
| Linux / BSD | Secret Service (gnome-keyring, Plasma `ksecretd`) | `ctypes` on `libsecret-1.so.0` + `libglib-2.0.so.0` (`org.freedesktop.Secret.Generic`, `SECRET_SCHEMA_DONT_MATCH_NAME`) | `Secret Service` |
| macOS | Keychain | `ctypes` on `Security.framework` and `CoreFoundation.framework` (`SecItemCopyMatching`, `SecItemAdd`, `SecItemDelete`) | `Keychain` |
| Windows | Credential Manager | `ctypes` on `advapi32.dll` (`CredReadW`, `CredWriteW`, `CredDeleteW`, `CRED_PERSIST_ENTERPRISE`, target `WriterAgent/<name>`) | `Credential Manager` |

## Reference format and naming

- **JSON value:** `keystore:<8 hex>` (e.g. `keystore:9f3a1c2b`). A fresh random token is generated on each write so `config.save()` detects a change and emits `config:changed`.
- **Key names inside the store:**
  - Endpoint API keys: `api_key:<normalized_endpoint_url>`
  - Password module fields: `<module>.<field>` (for example, `mcp.tunnel_provider_token`)
- Any pasted value that already starts with `keystore:` is stored verbatim as a reference.

## Migration and fallback

- **Migration trigger:** Pressing **OK** in Settings (`apply_settings_result` in [`plugin/chatbot/settings_dialog.py`](../../plugin/chatbot/settings_dialog.py)) calls `migrate_secrets_to_keystore()`, which moves all remaining non-empty plain-text API keys and `widget: password` config values into the OS store.
- **Fallback:** If no OS store is available (for example headless Linux without a Secret Service daemon) or if a write fails, `set_secret()` sets a session-wide `_writes_failed` flag and `config.save()` keeps the secret in plain text in `writeragent.json`. Settings displays the active location (`Secret Service`, `Keychain`, `Credential Manager`, or `plain text`) on the API Key label and the Provider config tooltip.
- **Unreadable refs:** If a `keystore:` ref cannot be read from the OS store, the getter returns `""` and the Settings field appears empty, while the `keystore:` ref in `writeragent.json` is preserved unless the user types a new key or clears it.

## Disabling for tests

Set `WRITERAGENT_KEYSTORE=0` in the environment to disable the OS store and keep secrets in plain text. `tests/conftest.py` and `plugin/testing_runner.py` (when not using `--user-profile`) set this automatically so tests never touch the developer's login keyring.

## Inspecting or deleting stored items

- **Linux (Secret Service):**
  - Inspect: `secret-tool lookup service WriterAgent username api_key:<normalized_url>`
  - Delete: `secret-tool clear service WriterAgent username api_key:<normalized_url>`
- **macOS (Keychain):** Open **Keychain Access** and search for `WriterAgent`.
- **Windows (Credential Manager):** Open **Credential Manager** → **Windows Credentials** → **Generic Credentials** and look for `WriterAgent/...`.

## Downgrade and portability notes

- **Downgrading** to an older WriterAgent version that does not understand `keystore:` references will send the literal `keystore:<hex>` string as the API key or tunnel token, causing authentication failures until the key is re-entered in Settings.
- **Copying `writeragent.json`** to another machine or OS user account no longer transfers secrets; re-enter keys on the target machine.
- **Multiple LibreOffice profiles** under the same OS user account share the same OS store items per normalized endpoint URL; clearing a key in one profile deletes that shared OS store item for every profile on that OS account.
