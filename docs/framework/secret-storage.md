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
  - Keyed entry secret fields (`secret_fields` in `module.yaml`): `<module>.<key>:<entry>.<field>` (for example, `mcp_client.servers:<server>.token`)
- Any pasted value that already starts with `keystore:` is stored verbatim as a reference.

## Migration and fallback

- **Migration trigger:** Pressing **OK** in Settings (`apply_settings_result` in [`plugin/chatbot/settings_dialog.py`](../../plugin/chatbot/settings_dialog.py)) calls `migrate_secrets_to_keystore()`, which moves all remaining non-empty plain-text API keys, `widget: password` config values, and `secret_fields` entry values (such as `mcp_client.servers` tokens) into the OS store. Orphan keyring items from legacy unnormalized endpoint slots are deleted only after the config write succeeds; removing or renaming a keyed entry deletes its keystore items when saving.
- **Fallback:** If no OS store is available (for example headless Linux without a Secret Service daemon) or if a write fails, `set_secret()` sets a session-wide `_writes_failed` flag and `config.save()` keeps the secret in plain text in `writeragent.json`. Settings displays the active location (`Secret Service`, `Keychain`, `Credential Manager`, or `plain text`) on the API Key label and the Provider config tooltip.
- **Unreadable refs:** If a `keystore:` ref cannot be read from the OS store, the getter returns `""` and the Settings field appears empty, while the `keystore:` ref in `writeragent.json` is preserved unless the user types a new key. A field that opened blank because the store was locked carries `keyring_locked=True` and is skipped on OK even if the store is unlocked in the meantime; for the API Key field, OK also checks `api_key_locked_for_endpoint` for the endpoint being saved so switching to a locked endpoint in the dialog never wipes its key either (unlock first, reopen or re-sync, then clear to delete). For `mcp_client.servers`, an unreadable ref blocks renaming the server in Settings and causes MCP calls to fail closed with `MCP_CLIENT_CONFIG` naming the server and keyring backend rather than connecting without auth. For `mcp.tunnel_provider_token`, an unreadable ref causes token-using tunnel providers (Cloudflare, Bore, Ngrok) to refuse to start rather than starting without their token.

## Locked keyring and threads

- **No caching of locked reads:** A locked or cancelled read is not cached in `_cache`, so unlocking later in the session succeeds on the next read. Reads within `LOCKED_RETRY_S` (5 s) return `None` without prompting again because Settings reads the same key several times while opening (`settings_dialog._get_core_field_specs`, `dialog_views._schedule_initial_models_fetch`, and TTS voice context) and background callers such as the grammar checker read on every request.
- **Shared per-name read lock and Stop cancellation:** Concurrent readers of one secret name serialize on `_read_locks[name]` so one OS read and one unlock prompt serve all callers. On Linux (`_LibSecret`), Send, Extend/Edit, `/formula`, and `/compact` pass their `SendCancellation` so Stop cancels the `GCancellable` and dismisses the prompt, raising `KeystoreLocked` (recording the 5 s retry window without caching `None`). A stopped waiter leaves the lock without marking the name locked. When a later read succeeds after a locked state, `get_secret` emits `keystore:unlocked` so open sidebars post a control refresh.
- **Off-UI-thread key reads:** Chat Send (`tool_loop._do_send_chat_with_tools`), voice STT (`panel_voice._prepare_stt`), Extend/Edit and `/formula` (`selection.create_validated_client`), and `/compact` (`slash_commands.prepare_slash_compact_on_ui_thread`) call `get_api_config(with_api_key=False)` on the UI thread and read the key once on their worker via `fill_api_key`, so an unlock prompt never freezes LibreOffice's UI thread. MCP tunnel sync (`mcp_server._sync_tunnel`) defers `get_secret_config("mcp.tunnel_provider_token")` to its `_start` worker and never calls `get_api_config`.
- **Vision lookup guard:** On the UI thread (`main.is_current()`), `has_native_vision` skips the provider memo (whose key hashes the secret) and background lookup while `api_key_needs_keystore_read` is true, answering from the static catalog until a worker reads the key once.
- **Bug reports:** `bug_report.build_github_issue_url` skips `get_api_key_for_endpoint` while `api_key_needs_keystore_read` is true so error boxes never open an unlock prompt; keys already read this session are still redacted.
- **Settings:** Settings is the intentional UI-thread reader for displaying its own fields.
- **Older libsecret:** `secret_password_searchv_sync` exists since libsecret 0.19.0; on older distributions `_LibSecret` still stores and reads secrets, but cannot distinguish a locked item from a missing one.

## Disabling for tests

Set `WRITERAGENT_KEYSTORE=0` in the environment to disable the OS store and keep secrets in plain text. `tests/conftest.py` and `plugin/testing_runner.py` (when not using `--user-profile`) set this automatically so tests never touch the developer's login keyring. The `--user-profile` mock sidebar suites keep the keystore on, but they store no API key for the mock endpoint.

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
