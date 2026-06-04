# Mayan EDMS Integration — Development Plan

> **Scope note:** This is a *standalone* development plan for adding support for remote document management systems (DMS), using **Mayan EDMS** as the concrete first integration target and reference implementation. It is deliberately kept separate from [multi-document-dev-plan.md](multi-document-dev-plan.md) (the local-filesystem "document_research" track). Future work may generalize "document sources," at which point cross-references can be added from both docs. Do not conflate the two tracks when scheduling or implementing.

> **Living document:** Update this file as phases ship, decisions are made, or scope changes. Link PRs and related topic docs.

---

## Problem

WriterAgent's current document research capabilities (see multi-document-dev-plan.md) are limited to **local files** discoverable via the active document's parent directory (or LO Work folder fallback). Many users and organizations store authoritative documents in dedicated **Electronic Document Management Systems** (EDMS / DMS):

- Versioned storage with retention policies
- Rich metadata, tags, cabinets (folder-like structures), indexes
- Full-text search backed by OCR for scans/PDFs/images
- Access control, workflows, audit
- Centralized "source of truth" rather than scattered local files or email attachments

A user should be able to say things like:
- "Summarize the latest contract in the Legal cabinet from Mayan and insert the key obligations into this report."
- "Pull the Q3 figures from the budget document tagged 'finance:2026' in our DMS and update the table here."
- "Find all policy docs mentioning 'remote work' in Mayan and give me a comparison."

Without native integration, users are forced to manually download files, or the agent cannot reliably "know" what lives in the DMS. Web research is too noisy and lacks the structured metadata/permissions context.

**Goal:** Make external DMS first-class (read-focused initially) citizens for the agent, while reusing the proven two-tier delegation, inner read-tool surface, read-only enforcement, hidden open, status UI, and MCP patterns from local document_research.

**Critical layering (this plan's core recommendation):** All knowledge of specific sources (local filesystem sibling documents + Mayan EDMS + any future sources) lives **only inside the outermost specialized sub-agent** for the `document_research` domain. The main chat agent must remain ignorant of the details — it only ever calls the high-level `delegate_to_specialized_{writer|calc|draw}_toolset(domain="document_research", task="...")` gateway (exactly as it already does for local document research today). The specialized outer sub-agent is the one that receives the rich tool surface (`list_documents`, `search_documents`, `delegate_read_document`, etc.) and the context/prompt guidance telling it "you can research both locally-nearby files and configured Mayan instances." This keeps the main agent's prompt and tool schema simple no matter how many sources are added.

---

## Design Principles (in priority order)

1. **Visibility layering (main agent stays dumb about sources):** The main agent must **never** see Mayan-specific (or source-specific) tools, config details, or prompts. It only knows the abstract concept of "delegate a document_research task." All source awareness (local nearby files + Mayan EDMS instances + future ones) is confined to the **outer specialized `document_research` sub-agent**. This sub-agent gets the full specialized tool surface and explicit instructions like "You have tools for both the local filesystem (same folder as the active document or LO Work dir) and any configured Mayan EDMS instances. Choose the appropriate source(s) based on the task." This is a direct extension of how local-only document_research already works today and prevents the main agent's prompt from exploding as more sources are added.
2. **Reuse, do not duplicate core machinery.** The inner read-only sub-agent, `READ_TOOLS_BY_DOC_TYPE`, `ToolContext.read_only_target`, `open_document_for_read` / close helpers, `DelegateToSpecializedBase`, `SmolAgentExecutor`, chat status blocks (`document_research_chat.py`), and `execute_on_main_thread` for UNO must be shared. A DMS "read" should ultimately produce a real LO model (via temp file download + hidden+ReadOnly load) so the *exact same* production read tools (`get_document_content`, `search_in_document`, `read_cell_range`, `get_document_tree`, etc.) run against it.
3. **Least additional code / complexity (per AGENTS.md).** Prefer extending the existing `document_research` domain (add a `source` parameter) over a brand-new parallel domain + duplicated delegation hints/prompts/status/MCP wiring. Introduce a tiny pluggable "listing + fetch" abstraction only when it clearly reduces net code. Use the project's existing `sync_request` (framework/client/requests.py, already used by aihordeclient) for all HTTP — no new requests/httpx dep.
3. **Read-mostly for Phase 0/1.** DMS documents are opened **read-only** (same guarantees as sibling local files). Write-back / upload new versions / create docs in DMS is valuable later but out of scope for MVP (avoids mutation scope, versioning, checkout/lock semantics, and content round-tripping complexity).
4. **Auth & config hygiene.** Token auth preferred (DRF Token from `/api/v4/auth/token/obtain/`). Store `mayan_*` keys in the existing `writeragent.json` (via `Config` dataclass + manifest registry). Redact tokens in logs (use existing redaction paths). No env-var keys in production. Support self-signed / local installs via optional SSL verify toggle.
5. **Source-aware but local-first.** `document_research` remains the user-facing domain. `list_documents(source="local"|"mayan", ...)` and `delegate_read_document(source=..., id_or_name=...)` (or equivalent). Local remains the zero-config default. Mayan is gated behind config presence + explicit source selection in the task description.
6. **Two-tier delegation preserved.** Outer (orchestrator) does discovery/search + calls inner per document. Inner still gets a narrow read-only allowlist on a concrete LO model. Main chat pays zero schema cost until it delegates `document_research`.
7. **Graceful degradation + clear errors.** If Mayan unreachable / bad creds / doc too large, surface actionable error to the outer agent (and thus user) with `details`. Cap result sizes; prefer metadata/search snippets before full download+open.
8. **MCP and status parity.** Same delegation surface, same chat "Tool: ..." preview blocks, same `X-Document-URL` semantics (active doc only; DMS paths/ids travel inside the delegate task).
9. **Tests required (AGENTS.md).** Matching `test_*.py` names. Unit tests with mocked HTTP (responses or a small stub client). UNO tests for temp-download + hidden open + inner read on the resulting model. Run `make test`.

---

## Mayan EDMS Background (research summary)

Mayan EDMS is a mature, GPL 2.0-licensed, Django-based open-source EDMS (since 2011, current docs reference version 4.11.4). It is positioned as the most advanced, scalable, mature FOSS EDMS, with Docker/Kubernetes deployment focus and a rich plugin + REST API architecture for customization. Official site and docs emphasize its use in government, non-profit, and commercial sectors.

Key relevant capabilities (from repeated searches against official docs.mayan-edms.com, release notes through 2024, and community sources):

- **REST API v4** (`/api/v4/...`): Client-less, self-documenting HTTP API (DRF-based). Swagger UI at `/api/swagger/ui/` and ReDoc on a running instance. Token auth (recommended: `POST /api/v4/auth/token/obtain/`) or Basic; header `Authorization: Token ...`. Many actions return 202 ACCEPTED for background processing (e.g. uploads, some downloads). Pagination standard (count/next/previous/results). Permissions often aligned between UI and API views in later releases.
- **Core objects**: Documents (containers), Document Files (source bytes + pages; multiple per document allowed since 4.0), Document Versions (virtual compositions/mappings of pages for viewing), pages, metadata (arbitrary types), tags, cabinets (hierarchical), indexes, workflows, signatures, etc.
- **Search**: Dynamic search API + advanced scoped search. Full-text via OCR (Tesseract, per-page on versions). Search models e.g. `documents.DocumentSearchResult` (discover pk via `/api/v4/search_models/`). Endpoints like `GET /api/v4/search/{search_model_pk}/` and `/advanced/{...}/`. Supports filters by label, content, metadata (including new source metadata in 4.7), cabinet ID, tag ID, document type ID, etc. Recent releases (4.7) added source metadata content search + dedicated REST endpoints, plus easier ID-based searches.
- **Downloads**: Target *Document Files* (exact original uploaded bytes + original filename/extension retained). Versions export to PDF. In 4.4+ moved to dedicated "document downloads" app with permissions, queuing, and user association. v4.7+ added pluggable download backends (e.g. direct storage, Google Cloud Storage signed URLs) for very large (multi-GB) files to avoid long proxy times. `get_absolute_api_url` helpers added for download links in some releases.
- **OCR / text**: First-class and per-page. OCR content can be read/edited via API. Full-text search and indexing operate on OCR output. Useful for scanned/PDF content.
- **Metadata & organization**: Strong support for document types + metadata types (per-type), cabinets (with mirroring support in some versions), tags, indexes (for dynamic cataloging e.g. by invoice number prefix). Excellent for precise agent queries like "the budget in the Finance cabinet tagged 2026".
- **Python client situation**: No actively maintained high-level official SDK/client for v4. The old `mayan-api_client` (thin slumber wrapper, ~2016) exists on PyPI but is stale. Direct HTTP (or requests in scripts) is the standard path, exactly as done for other external integrations in this project (e.g. AiHorde client using the shared `sync_request`).
- **Deployment & other**: Official Docker image on Docker Hub (millions of pulls). Web-based setup. Focus in 2024 releases on Django LTS updates (to 4.2), packaging size reductions, native email parsing, storage backends, and search refinements. Primary development on GitLab.

Integration is a natural fit because the agent already knows how to *read* (and extract structure) once it has an LO model; Mayan supplies rich discovery (list + powerful search with metadata/cabinets/OCR), provenance, and the raw bytes (or extracted text) needed to "open" for the inner read tools.

For implementation, the live Swagger/Redoc on an instance + the examples in the official REST API chapter are the best sources for exact serializers, filters, and response shapes (they evolve with releases). Basic patterns (list documents, get files for a doc, download a file) are stable enough for planning.

---

## Data Contracts

### Unified listing (preferred for low complexity)

Extend the existing `FileEntry` concept or introduce a small `DocEntry` (or keep `FileEntry` shape + extra fields) returned by listing tools. Add:

- `source`: `"local" | "mayan"`
- `id`: for mayan = document id (int) or uuid; for local = path
- `url_or_ref`: file:// for local; `mayan:doc:<id>` or the canonical API URL for mayan (for logging/traceability)
- `label` / `name`
- `modified`, `size_bytes`
- `doc_type_guess`
- `is_open` (for local only; false for remote)
- Optional: `cabinet`, `tags`, `metadata_summary`, `has_ocr` (from Mayan list/search payload)

Keep the shape stable so outer-agent prompt examples and `list_documents` result handling need minimal change.

### Delegate target

`delegate_read_document(source="mayan", id_or_name="123" | "budget 2026", task="...")`

For mayan, `id_or_name` can be the numeric id, uuid, or a label substring (resolver does a quick list or search to disambiguate, newest or best match wins, like local fuzzy).

### Temp file contract

When reading a Mayan doc:
1. Resolve id → fetch metadata + file list (to get download URL or construct it + original filename/ext).
2. Stream download (auth header) to a `tempfile.mkstemp(suffix=ext_from_mayan, dir=secure_temp)` or LO Work subdir. `delete=False`.
3. Call the *existing* `open_document_for_read(ctx, temp_path)` — it already handles abs paths and does Hidden+ReadOnly.
4. Run inner agent.
5. In finally: close (existing) + `os.unlink(temp_path)` (best-effort, like format.py temp buffers).

Add a helper `download_mayan_document_to_temp(...) -> (temp_path, original_name, opened_for_research_flag?)` or inline in the delegate tool.

Use the same "opened_for_document_research" flag pattern so close is conditional.

---

## Architecture & Reuse Points

### Layering: Main agent vs. outermost document_research sub-agent (must be explicit)

This is the most important point for implementers and for keeping the system maintainable as more sources are added.

- **Main agent** (the one the user chats with directly):
  - Only ever sees core tools for the active document + the `delegate_to_specialized_*_toolset` gateways.
  - The domain enum includes `"document_research"` (already the case).
  - Its high-level prompt (in constants.py / specialized delegation hints) says something like: "When you need information from other documents (local files near the active one, or in configured document management systems), delegate once to `document_research` with a clear task description of what to find and extract."
  - It does **not** know about `list_documents`, `search_documents`, Mayan URLs, cabinets, metadata types, etc.
  - It receives a compact final result (with provenance) and then does writes only on the active document.

- **Outermost / first-level specialized sub-agent** (`domain="document_research"`):
  - This is the agent that **knows everything about sources**.
  - It receives the full set of specialized tools for the domain: the existing local ones (`list_nearby_files` / unified `list_documents`, `grep_nearby_files`, `delegate_read_document`) plus the new Mayan-aware equivalents or source-parameterized versions.
  - Its instructions (built in `DelegateToSpecializedBase` or the document_research path) explicitly tell it:
    - "You can research documents from multiple sources. Local: files in the same directory as the active saved document (or LO Work folder for untitled). Mayan: any configured Mayan EDMS instances (use the mayan client tools for listing, searching by full-text/metadata/cabinet/tag, and delegating reads)."
    - "For most tasks start with the cheapest discovery (list or grep on local, or search on Mayan) before doing full `delegate_read_document` (which does download + hidden open + inner read agent)."
  - This agent does the orchestration across sources if needed (e.g., "check local first, then Mayan"), calls `delegate_read_document` (or a source-aware version) for the inner read-only agents, aggregates, and returns one payload.
  - Status updates for Mayan operations (e.g., "Searching Mayan...", "Downloading file from Mayan doc 123") flow through the existing `chat_append_callback` / status mechanisms, just like local `delegate_read_document` does today.

- **Inner read-only sub-agents** (one per opened document):
  - Completely source-agnostic. They only ever see the narrow read tool allowlist for that `doc_type` on a real LO model. They have no idea whether the model came from a local sibling file or a temp download from Mayan.

This layering is exactly why the two-tier model was chosen for local document_research (see multi-document-dev-plan.md). Adding Mayan (or Nextcloud, SharePoint, etc. later) should only expand what the `document_research` outer agent can do, never pollute the main agent's view.

### Source enablement: "If Mayan is configured, use it instead of local?"

When a user has a Mayan instance configured (URL + valid token), should the system treat Mayan as a *replacement* for local folder discovery, or keep both sources available to the outer agent?

**Recommended behavior (for MVP and beyond):**

- **Both sources are available by default** when Mayan is successfully configured. Local folder discovery (the existing same-directory / Work-folder behavior) remains on unless explicitly disabled.
- The **outer `document_research` sub-agent** (the only place that knows about sources) decides which to use, or whether to use both, based on the task it receives from the main agent plus its own instructions.
- Local is *not* turned off just because Mayan exists. There are legitimate mixed workflows:
  - The active document the user is editing may be a local/unsaved file or a working copy.
  - "Nearby" files (the budget spreadsheet sitting in the same folder as the report) are often still on the local filesystem even when the "official" versions live in Mayan.
  - Users may use Mayan as the archive while still having ad-hoc local documents.

- The outer agent's prompt/instructions (in the `document_research` path) should contain clear guidance such as:
  > "Local sources are for files physically next to the user's active document or in LibreOffice's Work folder. Mayan is the managed, searchable, versioned archive. Prefer Mayan when the user refers to 'official', 'the DMS', 'the company archive', specific cabinets, metadata, or full-text search across many documents. Use local for quick 'the file next to this one' cases. You can use both in one task if needed."

- Provide simple config controls so power users/orgs that have fully migrated can opt out of local:
  - `document_research_local_enabled` (default `true`)
  - Mayan is implicitly enabled when `mayan_base_url` + `mayan_api_token` are non-empty and valid (or add an explicit `document_research_mayan_enabled` boolean).

**UI implications (start minimal):**
- In the Settings dialog, the Mayan section (base URL, token, verify SSL) is sufficient for MVP.
- No extra "Use Mayan instead of local folders" global checkbox is required at first. The dual-source behavior + good outer-agent instructions handle the common case.
- If real usage shows people want an explicit "Primary document research source: Local / Mayan / Both" selector, add it in a later phase (it would be a small addition to the manifest + dialog_views).
- The outer agent can also surface in status/thinking when it chooses one source over the other.

This approach follows the project's "least complexity" rule: enabling Mayan adds capability without forcing the user to make a new global choice or breaking existing local-only behavior. The intelligence lives in the right layer (the outermost document_research sub-agent).

### Scoping searches — the outer agent must not search the entire Mayan library

The user correctly points out a critical practical issue: "the outermost document doesn't know where the files are. when searching I shouldn't probably search through the entire mayan edms."

A production Mayan instance can contain tens or hundreds of thousands of documents across many cabinets, projects, departments, years, etc. Blindly calling list or search without filters would be slow, expensive (tokens + time), noisy (the agent gets overwhelmed with irrelevant results), and poor UX.

**Core principle**: All Mayan list and search operations performed by the outer `document_research` sub-agent **must** use server-side filtering/scoping via the Mayan API whenever possible. The agent should only fall back to broader searches when the task explicitly calls for it (e.g., "find any policy document mentioning remote work across the whole archive").

Mayan provides excellent built-in scoping mechanisms that map well to "project or the equivalent":

- **Cabinets** (hierarchical, the closest thing to "folders/projects"): `/api/v4/cabinets/` to list the tree, `/api/v4/cabinets/{id}/documents/` to list (or search within) documents in a specific cabinet or subtree. Cabinets support nesting (e.g. `/Projects/AcmeCorp/2026/Q4`).
- **Metadata types + values**: Documents have typed metadata. Advanced search supports complex queries like metadata__Project=Acme AND year=2026. There is often a dedicated "Project" metadata type in real deployments.
- **Indexes**: Dynamic, metadata-driven views (e.g. an index template that organizes everything by Project > Document Type > Date). Good for "project-centric" navigation.
- **Tags** and **Document Types**.
- **Advanced search model**: The search API (`/api/v4/search/{model_pk}/` and advanced variant) supports scoped queries with operators.

**Tools the outer agent needs (in the document_research domain)**:
- `list_mayan_cabinets()` — returns the cabinet tree (id, full_path or label path, document_count if available). Supports optional parent filter for lazy loading subtrees.
- `list_mayan_documents(source="mayan", cabinet="Projects/AcmeCorp", metadata={"Project": "Acme2026"}, document_type="Budget", q="revenue", limit=50)` — translates to the best Mayan API call (cabinet-specific endpoint when cabinet is given, otherwise filtered document list or advanced search).
- `search_mayan_documents(query="Q4 results", scope={"cabinet": "/Finance", "metadata": {"Fiscal Year": "2026"}})` — uses the advanced search API with proper scoping.
- Discovery helpers: `list_mayan_document_types()`, `list_mayan_metadata_types()`, `get_mayan_cabinet_tree()` (or reuse the list one).

The thin `mayan_client.py` must implement these using the most efficient endpoint (prefer `/cabinets/{id}/documents/` + filters over a global documents list + client-side filter).

**Instructions for the outer agent** (injected in the document_research prompt block):
> "Mayan instances are often very large. You MUST narrow every list or search using cabinets, metadata, document types, or other filters before calling delegate_read_document on results. 
> Start by calling list_mayan_cabinets() or list_mayan_metadata_types() if you do not yet know the structure. 
> Configured default scopes (see below) should be your first choice unless the user's task clearly asks for something outside them.
> Only do a broad/unscoped search when the task says 'across the whole archive' or equivalent."

**From a UI / configuration perspective (Settings dialog)**

The connection (URL + token) is global for the Mayan instance. Scoping is additional configuration so the outer agent has good defaults without the user having to spell everything out in every chat message.

In the **Document Research** (or dedicated **External Document Sources**) tab of the WriterAgent Settings dialog, under the Mayan section:

- Connection fields (base URL, API token, Verify SSL) — as before.
- **Default research scopes** (new section, shown only when Mayan connection is configured):
  - "Preferred cabinets" — a multi-line text field or repeatable "Add cabinet path" controls. User enters paths like:
    ```
    /Projects/AcmeCorp
    /Finance/Budgets/2026
    /Legal/Contracts
    ```
    These are stored (as list of strings) and injected into the outer agent's context/prompt as "Default cabinets for this Mayan instance".
  - "Project metadata type" — a text field (or combo if we can discover). User enters the internal or display name of the metadata type that represents "project" in their organization (e.g. "Project", "Client Project", "Job Number"). The outer agent can then use `metadata__{that_name}=...` filters intelligently. Provide a helper button "Discover metadata types" that calls the discovery tool (for users setting this up).
  - "Additional default filters" — optional advanced: a small JSON or key-value area for other common metadata (e.g. `{"Department": "Engineering"}`).
  - Checkbox: "Allow unrestricted searches when no scope matches the task" (default: true for flexibility, or false for strict orgs).

These settings are stored in the normal `writeragent.json` (new keys under the config dataclass, e.g. `mayan_default_cabinets: list[str]`, `mayan_project_metadata_type: str`).

When the outer document_research agent is launched, its instructions include a block like:

```
[MAYAN DEFAULT SCOPES]
Preferred cabinets (use these first for most tasks):
- /Projects/AcmeCorp
- /Finance/Budgets

Project metadata type: "Project Code"
```

The agent can still call the discovery tools to explore beyond the defaults (e.g. "the user asked for the Legal project — let me list cabinets to find the right one").

**MVP scope for scoping**:
- Support cabinet paths as the primary scoping mechanism (most intuitive "project or equivalent").
- Basic metadata filtering in the list/search tools.
- The discovery tools (at least cabinets and metadata types).
- Injection of configured defaults into the outer agent's prompt.
- The client code translates high-level params (cabinet path, project=...) into the correct Mayan API calls (list by cabinet id, advanced search, etc.).

Later phases can add index support, saved "research views", per-task scope hints from the main agent, etc.

This keeps searches fast and relevant while still allowing the agent to be powerful when the user needs cross-project or archive-wide research.

### Implementation notes for this layering
- All new Mayan tools (`ListMayanDocuments`, `SearchMayanDocuments`, the extended `DelegateReadDocument` with source support, the thin client) must be registered with `tier = "specialized"` and `specialized_domain = "document_research"` (and `specialized_cross_cutting = True`).
- The prompt-building code in `specialized_base.py` (the `document_research_hint` block) and in constants.py must be the place where source awareness is injected for the outer agent only.
- Main agent's `DELEGATION_USER_FILE_DATA_HINT` and similar should stay high-level and mention "local or DMS documents" without implementation details.

- **Domain**: Start by *extending* `document_research` (add `source` to the relevant tools' parameters and to the outer prompt hints in specialized_base.py). This gives instant:
  - Gateway enum on Writer/Calc/Draw
  - Prompt language ("use list... then delegate_read... for source=mayan")
  - Chat status wiring (extend the existing `document_open_step_chat_text` or add a small branch)
  - MCP surface (one delegation path)
  - `USE_SUB_AGENT` requirement already enforced for the domain
- **New code location**: `plugin/doc/mayan.py` (client + helpers) + `plugin/doc/mayan_tools.py` (the List + Delegate tools, registered under document_research domain, cross_cutting=True) + small updates to `document_research_specialized.py` (pass-through source) and `document_research.py` (optional shared resolver bits).
- Or even smaller: put mayan bits inside `document_research_tools.py` / a `mayan_client.py` submodule to start. Goal: minimal new files.
- **HTTP**: `from plugin.framework.client.requests import sync_request`. Add Mayan-specific thin wrapper in `plugin/doc/mayan_client.py` (or contrib) that:
  - Builds auth header (`Authorization: Token ...`)
  - Handles base URL + `/api/v4/`
  - Provides `list_documents(...)`, `search_documents(...)`, `get_document(...)`, `download_document_file(doc_id, file_id_or_latest) -> bytes + filename`
  - Raises or returns structured errors compatible with `_tool_error`.
- **Config**: Add to `plugin/framework/config.py`:
  ```python
  # Mayan DMS integration (for document_research outer agent)
  mayan_base_url: str = ""
  mayan_api_token: str = ""  # never log raw
  mayan_verify_ssl: bool = True

  # Fine-grained control over sources visible to the document_research outer sub-agent.
  # Local is on by default (zero-config "nearby files" behavior).
  # Mayan becomes available when base_url + token are configured (see layering section above).
  document_research_local_enabled: bool = True
  ```
  Wire the Mayan fields into manifest registry + dialog_views for Settings (Phase 1+). The `document_research_local_enabled` can be a simple checkbox later if needed; for MVP the default is fine.
  Validate that Mayan source is usable only when credentials look present.
- **Registration**: Same `auto_discover` in `plugin/doc/__init__.py`. Marker bases in writer/calc/draw/specialized_base.py (or inherit/extend the existing DocumentResearchBase if we keep it under the same domain).
- **Read-only enforcement**: Already works once we call `open_document_for_read` + set `read_only_target`.
- **Threading**: Download can happen in the async tool worker (before the `execute_on_main_thread` for open). Use the existing SendCancellation / stop_checker paths.
- **Status in chat**: Reuse/extend `plugin/chatbot/document_research_chat.py`. Show "Tool: delegate_read_document (mayan:42 — Budget_2026.pdf)" etc.
- **Prompts / hints**: Update `DELEGATION_USER_FILE_DATA_HINT` and the document_research block in specialized_base.py (and constants.py) to mention `source="mayan"` when configured. Keep local behavior identical for users who never set Mayan.
- **Large file handling**: Query size first (Mayan list includes file size on latest file). Skip or warn for > threshold (reuse Phase 3/4 ideas from local plan). Prefer search snippet + delegate only on match.
- **Error surface**: Use `WriterAgentException` / `format_error_payload` / `_tool_error`. Network errors go through the framework client error paths where possible.

**Why not a completely separate `mayan_documents` domain?** It would duplicate a huge amount of wiring (new base classes in 3 app dirs, new prompt templates, new status formatter, new MCP schema entries, duplicated "how to use list then delegate" instructions, new test files, etc.). Extending the existing research domain is the minimal-code path and semantically correct ("research my documents, wherever they live").

---

## MVP Scope (Phase 0 — Mayan read via document_research)

**User-visible (all of this is hidden from the main agent and only available to the outermost `document_research` specialized sub-agent)**:
- Configure Mayan (base URL + token) in Settings (or direct json for early testing).
- When the main agent does a `document_research` delegation, the outer sub-agent may use `source="mayan"` (or `source="local"`, or both) internally. The main agent never sees or specifies the `source` parameter.
- `list_documents(source="mayan", filter="budget", cabinet="/Finance/Budgets", metadata={"Project": "Acme2026"}, limit=20)` — server-side filtered list using the best Mayan endpoint (cabinet-specific list when cabinet given, advanced search otherwise). Returns compact entries with provenance. Only available to the outer sub-agent.
- `delegate_read_document(source="mayan", id_or_name="123" or "budget q3", task="Extract revenue by month...")` — resolves, downloads latest file to temp, opens hidden RO, runs inner read agent (full read tools for the doc_type), returns compact result + provenance (id + label), cleans up temp.
- Full-text + scoped search via the Mayan advanced search API surfaced as `search_documents(source="mayan", query="remote work policy", cabinet="/Legal/Policies", metadata={"Department": "HR"})` — strongly prefers scoped calls; returns hits with snippets + ids. The outer agent then calls delegate read on promising ids. (Symmetric to `grep_nearby_files` for local.) Discovery tools like `list_mayan_cabinets()` and `list_mayan_metadata_types()` are also available to the outer agent so it can explore structure before searching.
- Works from any active doc type (Writer reading a Mayan .ods budget, etc.).
- Status lines appear in sidebar chat for the delegate step (similar to local `delegate_read_document` today; extend `document_research_chat.py`).
- Clear errors: "Mayan not configured", "Auth failed (check token)", "Document 99 not found or no download permission", "Download too large (45 MB) — skipped".

**Out of scope for MVP**:
- Write/upload back to Mayan.
- Multiple Mayan instances / "named sources".
- Version selection (always latest file for the doc).
- Direct OCR text fetch without download (future optimization; download+open reuses more code today).
- Caching of Mayan metadata/index (Phase 2).
- UI pickers or @-completion for DMS docs.
- Headless separate LO for the temp open (reuse local Phase 4 thinking later).
- Deep workflow / cabinet management tools (list cabinets as a helper is OK).

**Config surface (MVP)**:
- `mayan_base_url` (e.g. `http://localhost:8000` or `https://docs.example.com`)
- `mayan_api_token`
- `mayan_verify_ssl` (default true; false for self-signed dev)
- `document_research_local_enabled` (default true)

Scoping / preferred organization (new in this plan):
- `mayan_default_cabinets`: list of paths (e.g. ["/Projects/AcmeCorp", "/Finance/Budgets"])
- `mayan_project_metadata_type`: string name of the metadata field used for "project" scoping (e.g. "Project")
- Optional later: `mayan_default_document_types`, etc.

The presence of valid Mayan credentials implicitly enables the Mayan source for the outer agent. No separate `mayan_enabled` flag is required (see the "Source enablement" section above for rationale).

Add the Mayan fields (including scoping ones) to `AI_SIMPLE_FIELDS`? No — treat as DMS-specific under a "Document research" or "External sources" section in Settings. Add proper fields + validation in Config. The scoping fields should appear in a "Default scopes for Mayan research" subsection of the dialog.

**Done when (MVP)**:
- Unit tests: mock HTTP responses for list/search/download; resolver; temp download + open path (without real LO for pure unit).
- UNO tests (`test_mayan_uno.py` or extend existing): actual download of a test doc (or fixture) + hidden open + `read_cell_range` / `get_document_content` on the resulting model + cleanup.
- `make test` green.
- Manual: from a real or docker Mayan with a couple of .odt/.ods + a scanned PDF (OCR), delegate a cross-doc read that pulls facts into active LO doc. Verify no focus steal, no leftover temp files, token redacted in logs.
- MCP: a test exercising document_research delegation with mayan source (header points at active doc; task names a mayan id).

---

## Phased Implementation

### Phase 0 — Core read path (MVP)
- Config keys + validation + basic get_mayan_client().
- Thin client: auth, list_documents (with simple filters), search_documents (via search API), get_document_metadata, download_to_temp (or bytes).
- Tools: `ListDocuments` (generalized or mayan-specific under the domain; support source), `DelegateReadDocument` extended with source handling + temp lifecycle.
- Updates to prompt hints, specialized_base document_research block, constants.
- Resolver that can take id/label for mayan.
- Error paths + redaction for token.
- Tests as above.
- **No** Settings UI yet (manual json ok; add later).

### Phase 1 — Polish & usability
- Settings UI tab/section for DMS (manifest registry + dialog_views).
- Better filtering: cabinet, tag, metadata kv in list/search tools (pass through to API).
- Size guard + user-visible warning in result.
- Provenance: always return `{"source": "mayan", "id": 42, "label": "...", "result": "..."}` so main agent can cite.
- Update `DELEGATION_USER_FILE_DATA_HINT` / constants to mention DMS when configured.
- Chat status improvements (include cabinet or "Mayan:42").
- Unit tests for client edge cases (404, 403, large, pagination).
- Basic caching? (simple in-memory per session for metadata of recently listed docs).

### Phase 2 — Metadata index / fast path (like local 7.2)
- Optional: on list/search, surface Mayan-stored metadata + OCR snippets so outer agent can answer "which doc has the clause?" without a full delegate_read (which does the heavy download).
- Cache key by (mayan_id, mtime or checksum from API).
- Similar to proposed local metadata cache.

### Phase 3 — Advanced
- Specific version / file selection.
- Direct OCR text extraction (fetch page OCR content via API, synthesize a minimal "text doc" or feed to a text-only inner path) for huge scanned archives — avoids full binary download when only text needed.
- Write path: `upload_document_to_mayan` (new specialized_control tool? careful with mutation).
- Multi-source (multiple Mayan instances, or mixed local+mayan+nextcloud later).
- UI: DMS browser / picker surfaced from @ or a button (like planned local Phase 5).
- MCP full parity + examples in mcp-protocol.md.

### Phase 4 — Generalization (cross-DMS)
- After Mayan is solid, extract a small `DocumentSource` protocol / registry.
- Local FS becomes one implementation.
- Mayan another.
- Future: Nextcloud/ownCloud WebDAV + search, SharePoint (if auth solved), generic "HTTP folder + search endpoint" (tie into search-engine-integration.md patterns?).
- One `list_documents(source=..., ...)` surface.

---

## Files and Entry Points (proposed)

| Area | Path (minimize new files) |
|------|---------------------------|
| Config + UI scopes | `plugin/framework/config.py` (mayan_* + document_research_* + mayan_default_cabinets etc.), `scripts/manifest_registry.py`, `plugin/chatbot/dialog_views.py` (new subsection for default cabinets/metadata scoping) |
| Client + helpers | `plugin/doc/mayan_client.py` (new) or inline in research module initially |
| Tools (list, delegate, search) | Extend `plugin/doc/document_research_tools.py` + `plugin/doc/document_research_specialized.py`; or small `mayan_tools.py` auto-discovered |
| Domain markers | Minor additions in `plugin/writer/specialized_base.py`, `plugin/calc/base.py`, `plugin/draw/base.py` (or reuse DocumentResearchBase) |
| Open / temp / close extension | `plugin/doc/document_research.py` (add mayan-aware helpers or generalize) |
| Chat status | `plugin/chatbot/document_research_chat.py` (small extension) |
| Prompts / hints | `plugin/framework/constants.py`, `plugin/doc/specialized_base.py` |
| Module wiring | `plugin/doc/__init__.py` (already auto_discovers research modules) |
| Unit tests | `tests/doc/test_mayan_client.py`, extend `tests/doc/test_document_research*.py` |
| UNO / integration tests | `tests/doc/test_mayan_uno.py` (or `test_document_research_mayan_uno.py` following naming rule) |
| MCP test | Extend or add to `tests/mcp/test_mcp_server.py` |
| Docs | This file; later updates to mcp-protocol.md, chat-sidebar-implementation.md if needed; AGENTS.md *only* if global rule changes |

Avoid new top-level module unless it clearly wins on organization.

---

## Test Strategy

- **Unit (pytest)**: Mock `sync_request` (or inject a client stub). Cover list/search response shapes, fuzzy id resolution, download-to-temp path (assert file created + unlinked), error translation to tool payloads, token never appears in error strings that get logged as-is.
- **UNO (testing_runner, @native_test)**: Real temp file round-trip: use a small fixture doc (or create via LO in setup), "upload" notionally by having Mayan serve it (or use local Mayan in CI?), download via client, open_for_read, exercise read tools that match doc_type, verify content, assert cleanup. Also test mixed local + mayan in same outer agent run (mock one source).
- **Integration / acceptance**: Mirror the Calc scenarios from multi-doc plan but with one or more docs living only in Mayan. E.g. active Calc → delegate document_research source=mayan → list or search → delegate_read on budget → extract cells → write to active.
- **MCP**: Document research delegation that specifies a mayan source; verify the active doc context is still the one from the `X-Document-URL` header.
- **Security/negative**: Bad token, unreachable host, permission-denied doc, huge file, malformed search response. Token redaction in `writeragent_debug.log`.
- **Snapshot / prompt**: If prompt text for the domain changes, add/update any existing prompt tests.
- Always: `make test` before calling a phase done. New test files must follow the `test_<module>.py` / `test_<module>_uno.py` convention.

---

## Open Questions (record decisions here)

| # | Question | Notes / Decision |
|---|----------|------------------|
| 1 | Unified `source` param on existing tools vs new tool names? | **Lean toward unified** (list_documents + delegate_read_document take optional source="local"|"mayan"). Reuses all the prompt language, status, MCP, examples. New tools only if the shapes diverge too much. |
| 2 | How to surface cabinets/tags in list results for the agent? | Include compact arrays or "Finance > Q3" string in the returned entries. Keep payload small. |
| 3 | Download strategy for very large binaries? | Size check first. Option to "search snippet only" mode. Future: stream pages or OCR text only. For MVP: error with size in details. |
| 4 | Auth: only Token, or also Basic? | Token primary (recommended in Mayan docs). Support Basic as fallback for simplicity in some self-hosted setups. |
| 5 | Temp dir location & cleanup guarantees? | Reuse TEMP_DIR or a writeragent-specific subdir under LO user config / Work. Best-effort unlink in finally + on process exit hook if possible. Document that crashed runs may leak temps (rare, same as current local hidden opens before close helper). |
| 6 | Should search_documents be a first-class tool on the outer, or just let the agent call Mayan's search via a generic http tool (no)? | First-class `search_documents(source="mayan", query=...)` that returns id+label+snippet. Mirrors `grep_nearby_files`. Essential for "find which doc mentions X" without downloading everything. |
| 7 | Naming in user prompts / domain description | "mayan" as the source value is fine and explicit. Later generalization can map "dms:mayan" or keep source names as registered plugins. |
| 8 | MCP hosts that want DMS | Same as sidebar: pass ids/names inside the delegate task string. Do not overload `X-Document-URL`. Hosts can surface their own DMS browser if they want. |
| 9 | Does Mayan support mounting its document library as a local filesystem / WebDAV like Nextcloud? | **No.** Mayan does not expose its stored documents via a general WebDAV server or FUSE mount for arbitrary file access/browsing (unlike Nextcloud Files). <br><br>Related capabilities that *do* exist (mostly for ingestion, not research access):<br>• **Staging folder / Watch folder sources**: Mayan can poll a local directory (host-mounted via Docker volume, SMB, NFS, etc.) and automatically import new files as documents. Common pattern: mount an external share on the *host OS*, configure it as a staging source in Mayan.<br>• **Staging storage / Watch storage** (v4.5+): Same idea but using Mayan's pluggable storage backends (object storage, remote) — no need for the Mayan host to do an OS-level NFS/SMB mount.<br>• Historical **index mirror via FUSE** (`mountindex`): Allows mounting a *search index* as a read-only FUSE filesystem to browse catalog structure. Not the full document content library and not heavily emphasized in recent releases.<br><br>For the agent's *research* use-case (list/search by metadata/full-text/OCR, then fetch specific document content), the REST API + file download is the correct, primary, and most powerful path. A mount would be more relevant for traditional POSIX tools or human-driven workflows. Ingestion via watch/staging folders remains useful as a complementary human/scan workflow but is orthogonal to the agent being able to *research* existing documents in Mayan. |
| 10 | Source selection policy when both local and Mayan are available | See the dedicated "Source enablement" subsection in the Architecture section. Recommendation: both on by default (local stays cheap and useful); outer agent decides based on task + explicit prompt guidance. Provide `document_research_local_enabled` toggle for full-Mayan migrations. No extra "use Mayan instead of local" checkbox required in MVP UI. |

---

## Related Docs & Prior Art

- [multi-document-dev-plan.md](multi-document-dev-plan.md) — local two-tier, FileEntry, open_for_read, read-only, status, etc. (the thing we reuse heavily).
- [smol-main-chat-tool-architecture.md](smol-main-chat-tool-architecture.md)
- [mcp-protocol.md](mcp-protocol.md) — specialized domain policy, X-Document-URL.
- [agent-search.md](agent-search.md) — sub-agent research pattern (contrast: web is public + smol web tools; DMS is authenticated + structured + reuses LO read tools).
- [search-engine-integration.md](search-engine-integration.md) — JSON-driven HTTP search; some patterns (templating, response_path) may inspire the mayan client or a future generic source.
- [streaming-and-threading.md](streaming-and-threading.md)
- writer-specialized-toolsets.md, calc-specialized-toolsets.md, draw-impress-specialized-toolsets.md
- AGENTS.md (invariants, tests, docs updates, least complexity).

---

## Changelog (this plan)

| Date | Change |
|------|--------|
| 2026-?? | Initial plan created after research on Mayan REST API v4, auth, search, downloads, OCR, and comparison to existing local document_research + web_research + aihorde integration paths. |

---

**Next step after plan approval**: Implement Phase 0 using the reuse-heavy approach above. Add tests first or in lockstep. Update this plan with decisions and links as work lands.

This approach gives users the requested "plugin to document management systems" capability with the smallest delta to the existing high-quality local multi-doc implementation.