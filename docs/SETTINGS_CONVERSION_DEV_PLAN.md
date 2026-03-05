# Settings Conversion & Tools Options Schema — Dev Plan

This document is a proposed development plan for making **LocalWriter → Settings** the single, full-featured configuration UI (with tabs and working comboboxes) and for deciding what to do with the **Tools → Options** schema and generated AI page. Use it as a reference when you’re ready to implement.

---

## 1. Goal

- **Primary config UX**: One dialog — **LocalWriter → Settings** — with multiple tabs (Chat/Text, Image, optionally Advanced/MCP). Real comboboxes for endpoint, text model, image model; plain-text API key; shared settings (temperature, timeouts, etc.). All values stored in top-level `localwriter.json` keys.
- **Tools → Options**: No longer the place for AI configuration. Either remove the AI Options page or replace it with a minimal “Configure AI in LocalWriter → Settings” message.
- **Schema clarity**: Decide how the **module.yaml / generate_manifest / Options** pipeline fits going forward so we don’t maintain two competing “sources of truth” for AI settings.

---

## 2. Current State (Short)

| Surface | Source of truth | How it works |
|--------|------------------|---------------|
| **LocalWriter → Settings** | `get_settings_field_specs()` + `SettingsDialog.xdl` + `apply_settings_result()` | Dialog opened from menu; `legacy_ui.settings_box()` loads XDL, populates controls (endpoint/text/image combos via `populate_endpoint_selector`, `populate_combobox_with_lru`, `populate_image_model_selector`), reads on OK and writes top-level config. **Comboboxes work.** |
| **Tools → Options → LocalWriter** | `plugin/modules/*/module.yaml` → `scripts/generate_manifest.py` → `plugin/_manifest.py` + generated XDL in `build/generated/dialogs/` | Each module’s `config` block defines fields; generator emits manifest and one XDL “page” per module (and per list_detail). `OptionsHandler` loads/saves by `module.field` via ConfigService. **Combobox dropdown does not work** in Options (LO limitation). |

So today there are **two** ways to change AI-related settings:

1. **Settings dialog** — full experience, writes `endpoint`, `text_model`, `api_keys_by_endpoint`, etc.
2. **Tools → Options → AI** — same keys (via ConfigService `ai.*` → top-level mapping) but worse UX (no real combo dropdown, API key display quirk).

The “schema” that was generated for Tools Options is:

- **module.yaml** (`plugin/modules/ai/module.yaml`): defines `config:` with fields like `endpoint`, `text_model`, `image_model`, `api_key`, `temperature`, …
- **Generated manifest** (`plugin/_manifest.py`): `MODULES` list includes the `ai` entry with that `config` dict.
- **Generated XDL** (`build/generated/dialogs/`): one Options page for the `ai` module, layout derived from the schema (widget types, labels, options_provider for combo/select).

That schema is used **only** by the Tools → Options flow. The Settings dialog does **not** read from it; it uses a separate “schema” (the Python list from `get_settings_field_specs` and the hand-authored `SettingsDialog.xdl`).

---

## 3. The “Big Issue”: What to Do With the Schema

### 3.1 What the schema is used for today

- **ConfigService**  
  `set_manifest()` is called with a structure derived from the manifest (e.g. `{ "core": { "config": {...} }, "ai": { "config": {...} }, ... }`). That populates `_defaults` and `_manifest` so ConfigService knows defaults and access control. **In practice**, the bootstrap path may not call `set_manifest()` with the full MODULES; the code that does is in tests. So at runtime, Options may still work because the handler passes full keys like `ai.endpoint` and ConfigService’s **ai.* → top-level mapping** handles them.

- **Options UI**  
  The Options handler uses the manifest to know which controls to create and how to load/save. So the **ai** `config` in the manifest directly drives “what the AI Options page looks like” and which keys are written (e.g. `ai.endpoint` → then mapped to `endpoint`).

- **Other modules**  
  Core, Calc, Writer, Chatbot, Tunnel, etc. each have their own `config` in module.yaml. That schema is still useful for **their** Options pages (log level, max rows, max tool rounds, tunnel settings, etc.). So the **pipeline** (module.yaml → generate_manifest → _manifest.py → OptionsHandler) should stay for everything that is not AI.

### 3.2 Options for the AI schema

| Option | Description | Pros | Cons |
|--------|-------------|------|------|
| **A. Remove AI config from schema** | Delete (or empty) the `config:` block under the `ai` module in `plugin/modules/ai/module.yaml`. Regenerate manifest. | No duplicate AI UI in Options; one clear source of truth (Settings). | AI no longer appears in Tools → Options at all (or page is empty). Need to ensure no code relies on `ai.*` schema for defaults. |
| **B. Minimal AI Options page** | Replace the ai `config` with a single read-only or message field, e.g. “Configure endpoint, models, and API key in **LocalWriter → Settings**.” Regenerate. | Users who open Tools → Options still see an AI entry with a clear pointer. | Slightly more to maintain (one minimal page). |
| **C. Keep current AI schema but don’t promote it** | Leave the current ai config and generated page as-is. | No immediate code change. | Two UIs for the same settings; confusion and combobox/password quirks in Options. |

**Recommendation:** **A** or **B**. Prefer **A** (remove AI config from schema) for simplicity; use **B** if you want a visible “go to Settings” in Tools → Options.

### 3.3 How the schema would be used in the future (if kept)

- **For non-AI modules:** The schema (module.yaml `config`) remains the source of truth for:
  - Which fields exist on each Tools → Options page
  - Defaults and types
  - Options for select/combo (including options_provider)
  - ConfigService defaults/access if `set_manifest()` is ever wired from MODULES at startup

- **For AI:** The “schema” for AI settings would **not** be module.yaml. It would be:
  - **Field list**: `get_settings_field_specs()` (and any future tab-specific lists).
  - **Layout**: `SettingsDialog.xdl` (and any new tabs you add).
  - **Apply logic**: `apply_settings_result()` plus helpers in config.py (e.g. endpoint resolution, LRU updates, api_keys_by_endpoint).

So going forward you’d have:

- **Options schema** (module.yaml + generator): used for **Tools → Options** for Core, Calc, Writer, Chatbot, Tunnel, etc. **Not** used for AI.
- **Settings “schema”** (Python + XDL): used for **LocalWriter → Settings** for AI (and anything else you put in that dialog).

The ConfigService **ai.* → top-level** mapping can stay as long as something (e.g. a minimal Options page or tests) still uses `ai.*` keys; if you fully remove the AI Options page and any references to `ai.endpoint` etc., you could remove that mapping later to simplify.

---

## 4. Conversion Plan: One Dialog, More Tabs

### 4.1 Target UX

- **Single entry point**: User opens **LocalWriter → Settings** (or equivalent menu item). No need to go to Tools → Options for AI.
- **Tabs** (conceptually; you may already have 2):
  - **Chat / Text**: Endpoint (combo), Text model (combo), Image model (combo), API key (plain text), API type, temperature, chat max tokens, context length, request timeout, additional instructions, MCP enable/port, etc.
  - **Image**: Image provider (Horde vs endpoint), image model (if endpoint), base size, aspect, Horde-specific options, etc.
  - **Advanced** (optional): Custom models, models file, or other knobs you’ve commented out.
  - **MCP / HTTP** (optional): If you want a dedicated tab; or keep MCP on the first tab.

Your current `SettingsDialog.xdl` already has two tabs (Chat/Text and Image Settings) and uses real comboboxes; the conversion is mainly about (1) making sure all AI config lives there and is complete, and (2) stopping the Tools Options AI page from being the main (or a competing) UI.

### 4.2 Implementation steps (ordered)

1. **Audit and complete the Settings dialog**
   - Ensure `get_settings_field_specs()` and `apply_settings_result()` cover every key you care about (endpoint, text_model, image_model, api_key, temperature, timeouts, image provider, MCP, etc.).
   - Ensure `SettingsDialog.xdl` has the right controls and tab layout; fix API key so it’s a normal text field (plain text) if you want to avoid the “only 4 chars” display issue.
   - In `legacy_ui.settings_box()`, ensure endpoint/text/image combos are populated via the same helpers you like (presets + LRU, etc.) and that changing endpoint refreshes model lists and API key.

2. **Decide AI Options fate**
   - **Option A**: In `plugin/modules/ai/module.yaml`, remove the entire `config:` block (or leave it empty). Run `scripts/generate_manifest.py`. The AI Options page will disappear (or show no fields).
   - **Option B**: Replace the ai `config` with a single static text or message field (e.g. “For endpoint, model, and API key, use LocalWriter → Settings.”). Regenerate. Adjust XDL/generator if needed so that one field is shown.

3. **Regenerate and test**
   - After changing module.yaml, run the manifest generator. Confirm that:
     - Tools → Options no longer shows a full AI form (A or B).
     - LocalWriter → Settings still opens, populates, and saves all AI-related settings correctly.

4. **ConfigService and ai.* mapping**
   - If you removed the AI Options page (A): Nothing should call ConfigService with `ai.endpoint` etc. from the UI. You can keep the mapping for backward compatibility (e.g. old config or tests) or remove it and any `AI_SIMPLE_FIELDS` usage to simplify.
   - If you kept a minimal page (B): The handler might still write one key (e.g. a dummy); the mapping can stay or be narrowed.

5. **Documentation**
   - Update AGENTS.md (or user docs) to state that **AI configuration is done in LocalWriter → Settings**, not in Tools → Options. Optionally mention that Tools → Options still exists for other modules (Core, Calc, Writer, Chatbot, Tunnel).

6. **(Optional) Extra tabs**
   - Add an “Advanced” tab to the Settings XDL for custom models / models file when you re-enable them.
   - Add an “MCP” tab if you want to separate MCP/HTTP from the first tab.

---

## 5. Summary Table

| Item | Action |
|------|--------|
| **Settings dialog** | Keep as primary AI config UI; ensure it has all fields, working combos, plain-text API key, and clear tabs. |
| **Settings “schema”** | `get_settings_field_specs` + `apply_settings_result` + `SettingsDialog.xdl` are the source of truth for AI (and anything else in that dialog). |
| **Tools Options schema (module.yaml)** | Keep for **non-AI** modules, and add per-module `settings` annotations there to drive non-AI sections in the Settings dialog. For **ai**: remove config (Option A) or replace with minimal “use Settings” message (Option B). |
| **generate_manifest.py / _manifest.py** | Keep; they still drive Options for other modules and the extension structure. |
| **OptionsHandler** | Keep; it still serves Core, Calc, Writer, Chatbot, Tunnel, etc. |
| **ConfigService ai.* mapping** | Keep for now if anything still uses `ai.*`; remove later if you fully drop the AI Options page and clean up. |
| **Options providers** (`get_endpoint_options`, `get_text_model_options`, `get_image_model_options`) | Only needed if you keep an AI Options page (e.g. Option B). If you remove the AI page, they can be deleted or left unused. |

---

## 6. Files to Touch (Checklist)

- `plugin/modules/ai/module.yaml` — remove or shrink `config` (Step 2).
- `plugin/framework/settings_dialog.py` — any new keys or apply logic (Step 1).
- `extension/LocalWriterDialogs/SettingsDialog.xdl` — layout, API key as text, extra tabs if desired (Steps 1, 6).
- `plugin/framework/legacy_ui.py` — ensure `settings_box()` populates and reads all controls correctly (Step 1).
- `plugin/modules/core/services/config.py` — optional: remove `AI_SIMPLE_FIELDS` and ai.* mapping if AI Options is gone and nothing uses it (Step 4).
- `scripts/generate_manifest.py` — run after module.yaml change (Step 3).
- `AGENTS.md` — document “AI config in Settings only” (Step 5).

This gives you a single, tabbed Settings dialog as the main config UX and a clear path for how the Tools Options schema is used (for other modules only; AI is driven by the Settings dialog instead).

---

## 7. Non-AI Modules: Annotations for Settings Dialog

The plan above focuses mostly on AI. For **other services** (core, writer, calc, chatbot, tunnel, http/mcp), we can reuse their existing `config` entries in each `module.yaml` by adding lightweight annotations, without creating a new central schema file.

### 7.1 Per-module annotations in module.yaml

For non-AI modules only, extend selected `config` fields with a small `settings` block that tells the Settings dialog generator/runtime whether and where to surface them, for example:

```yaml
config:
  log_level:
    type: string
    default: WARN
    widget: select
    label: "Log Level"
    public: true
    options: [...]
    settings:
      include: true         # show this in the Settings dialog
      page: "general"       # logical tab id (e.g. general, writer, calc, network)
      group: "core"         # optional sub-group label
      order: 10             # ordering within the group
```

Key points:

- **Annotations live next to the existing params** in each module’s `module.yaml`; there is no new mega-schema file.
- Modules that should not surface anything in Settings simply omit `settings` blocks.
- AI stays **custom**: do not add `settings` blocks for AI fields unless you explicitly want them in a generic non-AI tab.

### 7.2 Collecting annotated fields

Add a small helper (or reuse logic from `generate_manifest.py`) that:

- Scans all `plugin/modules/**/module.yaml` manifests.
- For each module and each `config` field with `settings.include: true`, produces a record like:

```python
{
  "module": "core",
  "field": "log_level",
  "full_key": "core.log_level",
  "page": "general",
  "group": "core",
  "order": 10,
  "widget": "select",
  "type": "string",
  "label": "Log Level",
}
```

This can be done at build time (emitting a JSON file under `build/generated/`) or at runtime when the Settings dialog opens.

### 7.3 Integrating into the Settings dialog

Rather than generating an entirely new dialog, the Settings dialog can:

- Keep AI tabs as they are today (custom Chat/Text and Image tabs).
- Add an additional tab, e.g. **“Other Settings”**, whose layout you control in `SettingsDialog.xdl`.

On that tab:

- Give controls IDs that encode the module and field, e.g. `core__log_level`, `writer__max_content_chars`, `calc__max_rows_display`.
- At runtime in `legacy_ui.settings_box()`:
  - Use the annotated field list to find which settings should appear and which control IDs to look for.
  - For each annotated field:
    - If a matching control exists, populate it via `services.config.proxy_for(module).get(field_name, default)`.
  - On OK, read those control values and call `proxy.set(field_name, value)` per module.

This gives you:

- **Custom AI UI**: unchanged, still hand-authored and tailored.
- **Schema-driven non-AI sections**: each service declares its own user-facing settings via `settings` annotations in its own `module.yaml`, and the Settings dialog binds to them automatically.

The Tools → Options flow for those modules continues to use the same `config` entries and generated XDL; the annotations are purely extra metadata for the Settings dialog, not a replacement for the Options schema.

---

## 8. Turning Tools → Options On/Off with a Flag

You may want to **disable the entire Tools → Options integration for now** to simplify builds and gently migrate toward a Settings-only configuration UX, while still keeping all the code paths around for future use.

### 8.1 Design: build-time flag, no code deletion

Introduce a **build-time flag** that controls whether the extension registers any Tools → Options pages at all:

- Example env var: `LOCALWRITER_ENABLE_OPTIONS=true/false` (default `false` while you experiment).
- When `false`:
  - The build still compiles all Python, YAML, and helper modules (`options_handler.py`, `generate_manifest.py`, etc.).
  - But it **skips generating or installing** the registry fragments and dialog definitions that LibreOffice uses to show Tools → Options pages.
- When `true`:
  - The existing behavior is restored: `generate_manifest.py` emits the XCS/XCU/XDL needed for Tools → Options, and `manifest.xml` includes the relevant config/Options registrations.

This way you do **not** remove any code; you only change whether LO ever sees the configuration that wires Tools → Options to the extension.

### 8.2 Where to hook the flag in the build

Places to gate on `LOCALWRITER_ENABLE_OPTIONS`:

- **`scripts/generate_manifest.py`**:
  - Today it always:
    - Writes `build/generated/registry/*.xcs` / `*.xcu` for module configs.
    - Writes `build/generated/dialogs/*.xdl` for Options pages.
    - Writes `build/generated/OptionsDialog.xcu` (top-level Options registration).
    - Updates `extension/META-INF/manifest.xml` with the generated registry/dialog entries.
  - Add a simple conditional near the end:
    - If `LOCALWRITER_ENABLE_OPTIONS` (or similar flag) is **false**:
      - Still generate `_manifest.py` (runtime manifest for services/tools) because the rest of the extension uses it.
      - **Skip** writing:
        - `build/generated/registry/*.xcs` / `*.xcu`.
        - `build/generated/dialogs/*.xdl` (Options pages, not your custom `LocalWriterDialogs/*.xdl`).
        - `build/generated/OptionsDialog.xcu`.
        - The extra `manifest.xml` entries that point at the Options registry resources.
    - If the flag is **true**:
      - Keep current behavior (all artifacts emitted, Tools → Options on).

- **`Makefile` / `scripts/dev-deploy.sh` / `scripts/install-plugin.sh`**:
  - Ensure they pass the flag into `generate_manifest.py`, e.g.:
    - `LOCALWRITER_ENABLE_OPTIONS=0 python3 scripts/generate_manifest.py`
    - Or rely on the environment (`os.environ`) inside `generate_manifest.py`.

### 8.3 Runtime behavior when Options are disabled

When the flag is off:

- LibreOffice will **not load any Options pages** for LocalWriter because:
  - There are no `OptionsDialog.xcu` entries to attach to the Tools → Options tree.
  - There are no registry XCS/XCU entries declaring LocalWriter config pages.
  - `manifest.xml` does not reference those registry resources.
- The UNO component `org.extension.localwriter.OptionsHandler` still exists in the code, but LO never asks for it (no Options pages → no handler instances).
- All configuration is thus done through:
  - **LocalWriter → Settings** (the dialog you control and can expand).
  - Any future schema-driven non-AI settings you pull into that dialog via annotations.

When the flag is turned back on, the build regenerates the registry/dialog artifacts and LO will once again show the Tools → Options entries exactly as before.

### 8.4 Migration path

1. **Initial phase**: Set `LOCALWRITER_ENABLE_OPTIONS=0` in your dev build scripts.
   - Verify that Tools → Options no longer shows LocalWriter pages.
   - Confirm that Settings dialog covers all config you care about (especially AI).
2. **Enhancement phase**: Implement the non-AI annotations + “Other Settings” tab so Settings can replace most of the useful Options entries.
3. **Optional re-enable**: If you later decide some Options integration is still useful (e.g. for Calc/Writer-specific knobs), flip the flag back on and selectively clean up module.yaml `config` blocks so Options pages are minimal and not overlapping with Settings.

This gives you a **one-switch control** for the entire Tools → Options experience, without deleting or heavily refactoring the existing infrastructure.
