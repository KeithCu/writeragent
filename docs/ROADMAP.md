# WriterAgent Roadmap

**Last Updated**: 2026-08-30
**Status**: Active Development

Backlog of *still-open* work. Day-to-day orientation: [`AGENTS.md`](../AGENTS.md).
Entry points and topic hubs: [`repo-map.md`](repo-map.md). Product map: [`features.md`](features.md).
Do not duplicate those catalogs here.

## Immediate focus

- Keep shrinking boilerplate and fixing stale `docs/` paths when you touch a topic.
- Optional: section markers in megamodules (`content.py`, `document_helpers.py`, `format.py`, `panel.py`, …) before any large splits.
- Close the UNO fidelity gaps below (Writer / Calc / Draw / Impress).

`TestingFactory` / `with_native_doc` / `execute_tool` are the standard test path. Do not reintroduce per-file `@setup` / `@teardown` lifecycle.

## Product gaps

Primary product focus: features the agent still cannot drive. Shipped domains (shapes, fields, indexes, track changes, mail merge, styles list/create/update/import) stay in [`features.md`](features.md) and the toolset docs — not here.

**Writer** — [`writer/specialized-toolsets.md`](writer/specialized-toolsets.md)

- Bibliographies
- Watermarks
- Sections lifecycle (create, multi-column, conditional visibility, password); read-only inspection exists
- Style preview
- Batch section rewriting (heading-based whole-document processing)

**Calc** — [`calc/specialized-toolsets.md`](calc/specialized-toolsets.md)

- Macros and VBA compatibility
- Scenarios
- External data (SQL / web)
- Table slicers
- Sheet protection

**Draw / Impress** — [`draw/impress-specialized-toolsets.md`](draw/impress-specialized-toolsets.md)

- Slide animations
- Layer management
- Slide show controls
- Audio / video insertion
- 3D shape manipulation

Sibling-folder multi-document reads are shipped. Extra directories, `@` mentions, and headless opens live in the multi-document plan below — not as a second gap list.

## Living plans

Open work already has a topic doc. Do not copy those Open lists here.

| Doc | Remaining (one line) |
|-----|----------------------|
| [chat/multi-document-dev-plan.md](chat/multi-document-dev-plan.md) | Prompt integration, hidden-open hardening, `@` mentions |
| [chat/writer-calc-peer-messaging.md](chat/writer-calc-peer-messaging.md) | Design only: peer-ask gateway between Writer/Calc sidebar loops |
| [chat/responses-api-plan.md](chat/responses-api-plan.md) | Opt-in Responses API + `previous_response_id` (unstarted) |
| [scripting/monaco-editor-dev-plan.md](scripting/monaco-editor-dev-plan.md) | Syntax squiggles, range picker, Flatpak extra windows |
| [eval/eval-dev-plan.md](eval/eval-dev-plan.md) | Live 17-task ranking run; multimodal eval; LO ranking still out of scope |
| [eval/benchmark-cli-dev-plan.md](eval/benchmark-cli-dev-plan.md) | Ad-hoc `--task` / `--document` examples |
| [eval/dspy-prompt-optimization-plan.md](eval/dspy-prompt-optimization-plan.md) | Run MIPROv2; apply winning instruction |
| [writer/grammar-checker-plan.md](writer/grammar-checker-plan.md) | Invalidation, non-English locales, logging leftovers |
| [writer/html-style-model-plan.md](writer/html-style-model-plan.md) | Post-v1: UNO paragraph-style index (drop dual XHTML+FODT export) |
| [images/diffusers-comfyui-dev-plan.md](images/diffusers-comfyui-dev-plan.md) | ComfyUI backend unstarted |
| [framework/robustness-roadmap.md](framework/robustness-roadmap.md) | Recovery/retries, user-visible resilience, adversarial verification |

## Engineering leftovers

Not blocking product work. **One item per session.** Each can change user-visible or threading behavior; a naive patch is worse than leaving it.

| Session | Notes |
|---------|--------|
| **Connect vs read timeout** — [`http_transport.py`](../plugin/framework/client/http_transport.py), `LlmClient._timeout` | One value (default 120s) is connect **and** each stream read. Separate short connect vs Stop-aware read; do not set read timeout to `None` without a working Stop path. |
| **Tool schemas for Gemini/Groq** — [`tool.py`](../plugin/framework/tool.py) `validate` | Union collapse is recursive; empty `properties` still skips the unknown-key check, so hallucinated kwargs can reach `execute`. |
| **Close / termination veto** — [`uno_listeners.py`](../plugin/framework/uno_listeners.py) `_catch_and_log` | Re-raise `CloseVetoException` / `TerminationVetoException` only when a real listener must block close. |
| **Extra instructions in the system prompt** — [`prompts.py`](../plugin/framework/prompts.py) | Memory is already wrapped; `additional_instructions` is still concatenated as raw text. |

Shipped and removed from this table: Stop/cancel, missing-API-key 401, drain-handler `job_done`.

## Out of scope / not now

Visible so they are not re-invented as high-priority programs:

- Agent personality system; voice interface; real-time collaborative editing
- Usage analytics dashboard; in-app rating / feedback product
- UI theme editor; document template marketplace; offline mode
- Third-party plugin / extension API
- Tool registry versioning; config profiles (schema lives in `config_schema.py`)
- Memory search / expiration / compression (experimental `memory.py`, not active)
- Performance-profiling program; Draw/Impress pytest page stubs (factory `doc_type` is enough)
- MCP specialized-tool opt-in, domain switching, and document targeting (already implemented)

## Docs policy

Goal: **great living docs**, not a dump directory and not an exploding count of one-shot checklists.

- **Stay visible:** do **not** mass-move feature plans into `archive/` just to shrink `ls docs/`.
- Fold only when two files are the **same topic** and the content already lives in a hub; then delete the leftover so links do not 404.
- Keep **large feature hubs** large when the code is large (NumPy: `enabling_numpy_in_libreoffice.md`, `scripting/numpy-serialization.md`, `scripting/numpy-domains.md`).
- **Archive only** files that are truly dead (“we looked at X and said no”) with no remaining pointer value. Do not archive a doc that still has an Open/next list.
- [`repo-map.md`](repo-map.md) Deep dives is the catalog of **hubs**, not every markdown file. Root [`AGENTS.md`](../AGENTS.md) keeps invariants only (Hermes 20k cap).
