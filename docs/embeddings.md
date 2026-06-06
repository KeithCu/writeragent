# Embeddings — Development Plan

> **Status (2026-06):** **Phase B shipped** — per-folder `index.db`, `search_embeddings` tool, background folder indexer ([`embeddings_cache.py`](../plugin/doc/embeddings_cache.py), [`embeddings_indexer.py`](../plugin/doc/embeddings_indexer.py), [`embeddings_periodic.py`](../plugin/doc/embeddings_periodic.py), [`embeddings_service.py`](../plugin/framework/client/embeddings_service.py)). **Dedicated embeddings subprocess + 60 s in-RAM corpus cache** — embed/index/search use `worker_pool=WORKER_POOL_EMBEDDINGS` ([`venv_worker.py`](../plugin/scripting/venv_worker.py), [`embeddings_index.py`](../plugin/scripting/embeddings_index.py)); Calc/chat stay on the default pool. **Incremental maintenance:** filesystem **mtime** vs **`last_indexed_at`** per file, paragraph **`content_hash`** diff, and a **periodic background tick** (`EMBEDDINGS_INDEX_INTERVAL_S`, default 5 min) for the active folder when `DOCUMENT_RESEARCH_SEARCH_MODE=embeddings`. **Phase A:** host `embed_texts()` + trusted venv encode ([`embedding_client.py`](../plugin/framework/client/embedding_client.py)). **Search mode:** compile-time `DOCUMENT_RESEARCH_SEARCH_MODE` in [`constants.py`](../plugin/framework/constants.py) — `"grep"` (default) or `"embeddings"` (mutually exclusive tool registration). Bench harness: [`scripts/bench_embeddings.py`](../scripts/bench_embeddings.py). **Scope (MVP):** one cache per **filesystem folder** (all indexable siblings in that directory) — not per-file caches, not a global index, not in-document RAG storage. **On-disk storage:** single `index.db` per folder (locators + vectors) using standard SQLite; host and embeddings subprocess open the same file directly and pass references (db path / folder key) rather than bulk corpus data. **Supported path today:** normalized float32 vectors in **`chunks.embedding` BLOB** + NumPy dot + top-k in the embeddings venv ([bench](#benchmark-on-your-machine): sub-ms at ~350 paragraphs). **sqlite-vec vec0 is optional** — code probes and uses it when installed, but **do not rely on it** for MVP or dogfood; revisit only if folder-sized profiling shows NumPy search is too slow. Embed compute uses the embeddings warm-worker Pickle5 path. **Phase C live hooks (`XProofreading`, write-tool dirty marks) are not planned** — periodic mtime refresh is sufficient (see [Phase C](#phase-c-incremental)).

**Related:** [cython-extension.md](cython-extension.md) · [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md) · [multi-document-dev-plan.md](multi-document-dev-plan.md) · [langchain-plan.md](langchain-plan.md) (chat memory / summarization only)

---

## Problem

The expensive case is **many documents**, not one.

Today the **outer** [document_research](../plugin/doc/document_research.py) sub-agent discovers siblings with `list_nearby_files`, guesses filenames from vague user language, opens candidates, and greps with `search_in_document` / full reads. That is **better than opening 100 files blindly**, but still slow, token-heavy, and weak on paraphrase ("remote work" vs "WFH policy" in an oddly named `Notes_v3.odt`).

**Embeddings** replace that **outer-layer grep** with semantic lookup over a **per-directory index**: one `index.db` per filesystem folder ([Corpus storage](#corpus-storage)) — float vectors in **`chunks.embedding` BLOBs** (NumPy search in the venv) plus locators back to `doc_url`, paragraph, offset (not a second full-text cache). Optional sqlite-vec `vec0` exists in code when installed but is **not required**. The outer agent searches **that folder’s cache** and gets ranked hits **without opening LO**; it then opens **one or a few** files and hands precise locations to the inner read agent. Opening one file at a known locator is cheap compared to opening dozens and searching each.

### Why embeddings (semantic search) vs pure lexical/grep, and why the difference is bigger for office documents than code

Recent experience with AI coding agents shows a split in retrieval strategy:

- Code is highly literal and structured. Developers (and agents) usually want an exact function name, variable, string literal, or symbol. Language servers, AST indexes, and fast `grep` (or ripgrep) are extremely effective. Paraphrase is uncommon — you rarely ask for "a function somewhat like authentication"; you ask for the auth handler or grep for "auth". Because code changes frequently, pre-built vector indexes also suffer from staleness and re-indexing cost.

- Office documents (Writer prose, Calc tables with natural-language labels and notes, Draw captions and diagrams, policy files, fiction, research notes, meeting minutes) are different. Content is natural language or semi-structured text. Users describe what they want with vague or high-level language ("the Q4 revenue figures", "the remote-work policy", "the section on expense approvals from last year"). Filenames are often unhelpful or historical. The same idea can be expressed with synonyms, paraphrases, or completely different wording across files.

In short: code search is mostly *lexical lookup of known identifiers*; document search is often *semantic discovery of unknown phrasing*.

This project already exploits the practical consequence of ODF files being ZIP archives: a naive "list siblings then open-and-grep many" path pays repeated unzip + XML parse costs for every candidate. The per-directory embeddings index lets the outer agent perform a cheap semantic ranking *before* paying the extraction cost for most files. Hits return locators; only the top 1–few documents are opened, and the inner agent then uses precise tools (`search_in_document`, range reads, outline navigation, etc.) on the live content.

Industry data on coding agents is consistent with a hybrid view rather than "embeddings are dead":

- Tools like Cursor measure clear gains (+12.5% agent accuracy in their tests) from adding semantic search (custom-trained embeddings) alongside grep, especially for conceptual queries in large codebases. They explicitly state that the *combination* of semantic search and grep produces the best results.
- Other agentic systems (e.g. Claude Code style) favor pure iterative tool use (grep/glob/read + reasoning) for code precisely because of the literal, structured nature of source and the ability of a strong model to explore on the fly.

For the document use cases this index targets — cross-sibling discovery in Writer/Calc/Draw folders — the semantic component has stronger justification than it does for pure code. Fiction writing, policy/legal work, research corpora, creative projects, and any domain where meaning is expressed in varied natural language all amplify the value of embeddings over keyword-only approaches. The design here deliberately scopes embeddings to the outer routing layer only; it never replaces the precise read tools used once the right file(s) are open, and it maintains exactly one index type per directory (no parallel FTS "double cache").

The result is a router that handles the paraphrase/fuzzy-name problem that defeats filename filters and broad greps, while still letting the agent fall back to exact search inside the small number of opened documents.

**Within a single already-open document**, normal search remains enough (`search_in_document`, outline, sheet navigation). **Cross-folder semantic routing for document_research is the main win.**

**One index type per directory:** do **not** maintain FTS5 (or any parallel keyword corpus) alongside embeddings in that folder’s cache — that would be a **double cache** of the same content. Embeddings are the cross-file search layer for **that directory**; after a hit, use existing read tools on the opened file for literals and detail.

This choice also aligns with the domain analysis above: once the right small set of documents has been selected semantically, the agent has cheap, precise, always-fresh lexical tools inside those opened files. There is no need (and no desire) to duplicate a full-text index in the embeddings cache.

**Chat history vs document embeddings:** Sidebar history (`writeragent_history.db`) is unrelated. This is **corpus routing memory**, not turn memory.

---

## Primary use case: outer document_research replaces grep

[multi-document-dev-plan.md](multi-document-dev-plan.md) uses a **two-tier** delegate: **outer** lists/opens/orchestrates; **inner** runs read tools on one opened file. **Embeddings target the outer tier** — the first sub-agent that today picks files and greps.

```mermaid
flowchart LR
  User["User task on active doc"]
  Main[Main chat]
  Outer["Outer document_research sub-agent"]
  Index["Embeddings index\nvectors + locators only"]
  Hits["Top-k: doc_url + para + offset"]
  Inner["Inner sub-agent\none opened file"]
  Read["Read at locator"]
  Main -->|"delegate"| Outer
  Outer -->|"embed query — not grep"| Index
  Index --> Hits
  Hits -->|"open 1–few URLs"| Outer
  Outer -->|"delegate_read_document"| Inner --> Read
```

**Before (outer):** `list_nearby_files(filter="budget")` → guess → open → `search_in_document` / `get_document_content` on each candidate.

**After (outer):** `search_embeddings("Q4 revenue figures")` → `[Budget_2026.ods, sheet hint / loc, score], …` → `delegate_read_document` on **top hits only** → inner `read_cell_range` / `get_document_content` at the referenced region.

Main chat may also call the index before delegating, but the **major integration point is the outer document_research tool surface** — smarter, faster file pick, no filename lottery.

The rationale for preferring a semantic router here (rather than relying solely on lexical discovery) is discussed in the Problem section: office documents are natural-language heavy and ODF extraction has per-file cost, unlike the more literal, identifier-driven nature of code search where pure grep + structure often suffices.

### After the hit: open one file, dig up truth

The index is **not** a document store. It holds:

- Normalized **float32 vectors** in sqlite-vec **`vec0`** (fallback: embedding BLOB + NumPy search — [Corpus storage](#corpus-storage)).
- **Locators** — enough to find the passage again in LO (paragraph + offset, and Calc/Draw-specific fields as needed later).
- **No duplicated chunk text** in the cache (text is read at index time only to **encode** in the venv or cloud API, then discarded from persistent storage).

Lookup returns **where to look**; opening **one** (or a few) files and reading at that locator is intentional and cheap. That beats opening many files and running semantic search inside each.

---

## Vision

- **One minimal index** — vectors + locators; no duplicate FTS/text cache.
- **Outer document_research** queries embeddings instead of grep across opened siblings.
- **Open one (or few) files** at known locations — not semantic search inside every file in the folder.
- Optional in-document injection on main chat send for edge-case huge single files (low priority).

---

## User-facing modes

| Mode | User experience | Priority |
|------|-----------------|----------|
| **Outer semantic find** | document_research outer agent: `search_embeddings` → ranked `doc_url` + locators → open top hits | **Primary** |
| **Index folder / corpus** | Background embed on open/save; revision-keyed invalidation | **Primary** |
| **Main → delegate with hits** | Main chat runs index first, passes paths/locators into delegate task | **Primary** |
| **Cross-doc Q&A** | Top-k locators across corpus, then inner reads on opened files | **Primary** |
| **In-document RAG on send** | Optional chunk inject beside `[DOCUMENT CONTENT]` for one huge file | **Secondary** |

**Benefits available with today's stack (no LangChain):**

- **`sentence-transformers` + NumPy in the user venv** — tier-one **MVP** embedder (offline, batch CPU, no per-paragraph API cost). See [Local embedders (MVP)](#local-embedders-mvp).
- Cloud embed APIs (OpenRouter / Together / Ollama) when no venv or user prefers hosted models.
- [`list_nearby_files`](../plugin/doc/document_research.py) + read-only extract for **indexing**; index for **lookup** before any of that on query.
- **Pickle5 IPC** into the warm venv worker for encode + sqlite-vec KNN (NumPy fallback — [`bench_embeddings.py`](../scripts/bench_embeddings.py) validates the fallback path).
- Stdlib SQLite on host for locator metadata; **sqlite-vec `vec0`** for vectors + search in the venv (same `index.db` file).

---

## Development plan {#development-plan}

**Goal:** outer [document_research](../plugin/doc/document_research.py) calls `search_embeddings(query, k)` → ranked hits **within the active folder’s cache** (`doc_url` + paragraph locators) → open top files → inner read at offset. No parallel FTS index; no chunk text on disk.

### Scope: per-directory cache only (MVP) {#per-directory-cache}

The **only** persisted index shape for now:

| In scope | Out of scope (later or never) |
|----------|-------------------------------|
| **One cache per directory** — all LibreOffice siblings in the same folder share one cache under `writeragent_embeddings/<key>/` | Per-**file** sidecars or per-document `.db` files |
| Folder key = normalized path of the **directory** being searched (parent of active doc + siblings) | Single global index across `~/Documents` |
| `search_embeddings` searches **that directory’s** vec0 index | Cross-directory search in one query |
| Locator rows identify **which file** each vector belongs to (`doc_url`) | Storing full text in the cache |
| Background worker builds/refreshes **the whole directory cache** | In-document-only embed cache beside `[DOCUMENT CONTENT]` (Phase D — separate) |

**Mental model:** the cache mirrors “everything in this folder that document_research could grep today” — one semantic index for that **directory of files**, not one index per open document and not one index for the entire machine.

**Rule:** **each directory gets its own cache.** Work in `/projects/reporting/` uses only `writeragent_embeddings/<key-for-reporting>/`. Work in `/projects/legal/` uses a **different** `<key-for-legal>/` — separate `index.db`, built and refreshed independently. No sharing across directories in MVP.

```text
/home/user/projects/reporting/          ← real user folder (many .odt/.ods)
  Budget.odt
  Notes_v3.odt
  Q4.ods

~/.config/.../user/writeragent_embeddings/
  <key-for-reporting>/                  ← ONE cache for that directory
    index.db                            ← locators + vec0 vectors for ALL files above
```

### What is shipped

| Item | Status |
|------|--------|
| [`scripts/bench_embeddings.py`](../scripts/bench_embeddings.py) | **Done** — batch encode + vectorized search via warm worker |
| `sentence_transformers` on venv whitelist | **Done** — [`sandbox_imports.py`](../plugin/scripting/sandbox_imports.py) |
| `get_safe_module` bypass for ST | **Done** — avoid hang on import ([`local_python_executor.py`](../plugin/contrib/smolagents/local_python_executor.py)) |
| [`embedding_client.py`](../plugin/framework/client/embedding_client.py) | **Done** — `embed_texts()` via venv RPC (Phase A; HTTP deferred) |
| [`embeddings_index.py`](../plugin/scripting/embeddings_index.py) | **Done** — trusted batch encode module (Phase A; index/search in Phase B) |
| Config `embedding_model` / `embedding_provider` | **Done** — defaults in [`config.py`](../plugin/framework/config.py); Settings UI deferred |

See [Benchmark on your machine](#benchmark-on-your-machine) for sample numbers (349 paragraphs, dot+top-k **0.17 ms** median on Arch).

### Transport: warm-worker IPC (MVP — keep)

Reuse [`PythonWorkerManager`](../plugin/scripting/venv_worker.py) / `run_code_in_user_venv` — same Pickle5 path as `=PYTHON()` and `run_venv_python_script`.

The persistent index is a single on-disk SQLite file (`index.db`) that **both processes open directly** from the filesystem. Standard `sqlite3` (no extensions) already supports multiple processes reading and writing the same DB file concurrently (readers + one writer at a time; WAL mode is an optional improvement). We pass lightweight *references* (the `folder_corpus_key` or full path to `index.db`) rather than shipping the corpus, vectors, or full result matrices on every operation.

Typical flow:

1. **Host** extracts paragraph text (Writer read-only extract / ODT unzip for tests) and does filesystem mtime / content_hash comparisons using its own stdlib `sqlite3` connection to `chunks`.
2. **Host → venv (RPC stub):** for indexing, send only the changed paragraphs (via worker **`data=`** Pickle5) + the db path / folder reference. For search, send the query text (or pre-embedded vector) + `k` + the db path / folder reference.
3. **Venv (trusted module):** the fixed stub receives the reference, opens the *same* `index.db` file with stdlib `sqlite3` (standard SQLite concurrency works across the two processes), loads `sqlite_vec` if available, lazily loads the `SentenceTransformer`, does batch `encode` when needed, then performs vec0 DML/search or the BLOB + NumPy fallback entirely against the on-disk data. Only small results travel back (top-k locators+scores, or write confirmations).
4. **Session:** reuse worker `session_id` so the loaded `SentenceTransformer` survives across calls ([`EMBEDDINGS_WORKER_SESSION_PREFIX`](../plugin/framework/constants.py)). Embeddings RPC uses a **separate warm child** (`worker_pool=WORKER_POOL_EMBEDDINGS`) from Calc `=PYTHON()` and chat scripts — see [Dedicated embeddings subprocess](#dedicated-embeddings-subprocess) and [In-worker corpus cache](#embeddings-in-worker-cache).

The **LLM / `=PYTHON()` sandbox** blocks `open()`, `sqlite3`, etc. in **user-submitted** scripts — that rule does **not** apply to shipped `plugin.scripting.*` modules invoked from the host. Trusted embeddings code may `open()` `index.db` (by the path reference passed from the host) and call `sqlite_vec.load()` inside e.g. `plugin.scripting.embeddings_index`. Bulk *source text for embedding* still flows over IPC **`data=`** at index time; the persistent vectors and locators live in the shared DB file. On disk, the corpus is under `writeragent_embeddings/<folder_corpus_key>/`.

**Not pursuing for MVP:** host Cython `top_k_dot`, `/tmp` mmap in worker, LangChain vectorstores — see [Future optimizations](#future-optimizations).

### Dedicated embeddings subprocess {#dedicated-embeddings-subprocess}

**Status: shipped.**

Embeddings encode, index persist, and `knn_search` run in a **second** warm venv child ([`PythonWorkerManager`](../plugin/scripting/venv_worker.py) keyed by `WORKER_POOL_EMBEDDINGS` + python exe). Calc `=PYTHON()`, chat `run_venv_python_script`, and notebooks stay on `WORKER_POOL_DEFAULT`.

| Concern | Default pool (Calc / chat) | Embeddings pool |
|---------|---------------------------|-----------------|
| **Calc / user NumPy** | Unaffected by long folder re-embed jobs | Folder cold-build / batch re-embed runs here |
| **Repeated `search_embeddings`** | N/A | In-RAM corpus cache ([below](#embeddings-in-worker-cache)) avoids re-reading BLOBs within TTL |
| **Model load** | User script traffic only | `SentenceTransformer` stays warm for document_research bursts |

**Implementation:**

- Same `scripting.python_venv_path` interpreter, same Pickle5 stub protocol ([`embeddings_service.py`](../plugin/framework/client/embeddings_service.py) / [`embedding_client.py`](../plugin/framework/client/embedding_client.py) → [`embeddings_index.py`](../plugin/scripting/embeddings_index.py)).
- Host passes `worker_pool=WORKER_POOL_EMBEDDINGS` on the two RPC entry points only.
- Optional Settings later: dedicated venv path vs shared venv (same packages, different process — not required).
- **Not** a third HTTP stack or a different embedding library — process isolation + caching only.

#### In-worker corpus cache (RAM) {#embeddings-in-worker-cache}

**Status: shipped** (BLOB + NumPy path only; vec0 `MATCH` unchanged).

Persistent storage remains **`index.db` on disk** (source of truth). The RAM cache is a **read-through / invalidation layer inside the embeddings subprocess** so hot queries avoid re-reading the whole BLOB column on every `search_embeddings` call.

**Problem today (BLOB + NumPy path):** for a folder with *N* chunks, each search roughly: open SQLite → `SELECT … embedding FROM chunks` for all rows → `np.stack` → dot + top-k. At 100 longdoc-sized files (*N* ≈ 35k) that is **~50+ MiB read from disk per query** even when the index has not changed since the last query five seconds ago. Encode cost dominates cold builds; **repeat search** on an unchanged index is where RAM caching helps.

**Proposed behavior:**

| Layer | Role |
|-------|------|
| **`index.db` (disk)** | Durable locators + vectors; incremental indexer writes here; search **always valid** against disk if RAM cache is cold or evicted |
| **In-worker cache (RAM)** | Keyed by `(folder_corpus_key or db_path, embedding_model)` → `{corpus_matrix, chunk_ids, locators, loaded_at, corpus_meta_fingerprint}` |

**Cache policy:**

- **Load on first search** for a folder key; reuse for subsequent `knn_search` in the same embeddings process.
- **TTL `EMBEDDINGS_CORPUS_CACHE_TTL_S` (60 s)** since last access — per-folder sliding window; document_research delegation bursts stay hot without holding matrices forever.
- **Invalidate** on `index_paragraphs` / `delete_paragraphs` RPC for that `db_path`, or when fingerprint (`chunk_count` + `corpus_meta.updated_at`) changes on next access.

**What this is not:**

- Not a second on-disk index or duplicate of chunk text — only **float matrices + locator tuples** already in `index.db`.
- Not a substitute for sqlite-vec at huge *N* — at very large corpora, profile vec0 or partial loading; the 60 s RAM window targets **typical folder** repeat-query latency.
- Not shared across processes — cache lives in the **embeddings-only** subprocess; the general sandbox worker never sees it.

**Expected win:** second and subsequent searches on the same folder within the TTL drop disk BLOB reads and matrix rebuild — query path becomes encode query + in-memory dot + top-k (bench-class **sub-ms to low-ms** for folder-sized *N* when matrix is warm).

See also [Index size growth](#index-size-growth) for when RAM footprint matters.

### Phase A — Embed client + config **(shipped — venv-only)**

- [x] Host [`embedding_client.py`](../plugin/framework/client/embedding_client.py) — `embed_texts(ctx, texts) -> EmbeddingBatch` via venv RPC
- [x] Config: `embedding_model`, `embedding_provider` in [`config.py`](../plugin/framework/config.py) (`local` only implemented; HTTP tier deferred; **no Settings UI yet** — edit `writeragent.json` or use defaults)
- [x] Trusted venv module [`embeddings_index.py`](../plugin/scripting/embeddings_index.py) + fixed host stub — see [Trusted extension code in the venv](enabling_numpy_in_libreoffice.md#trusted-extension-code-in-the-venv)
- [x] Tests: mocked venv RPC + mocked SentenceTransformer ([`test_embedding_client.py`](../tests/framework/test_embedding_client.py), [`test_embeddings_index.py`](../tests/scripting/test_embeddings_index.py))
- Default model: `all-MiniLM-L6-v2` ([`DEFAULT_EMBEDDING_MODEL`](../plugin/framework/constants.py)) until multi-model bench says otherwise

#### Phase A — what exists today (handoff for Phase B)

| Piece | Location | Contract |
|-------|----------|----------|
| Host API | [`embedding_client.embed_texts`](../plugin/framework/client/embedding_client.py) | `EmbeddingBatch(model, dim, vectors, indices)` — `vectors` are L2-normalized float32 nested lists; `indices` maps each vector back to the input list position (empty strings skipped) |
| Model config | `get_embedding_model(ctx)` | Reads `embedding_model` from config; falls back to `all-MiniLM-L6-v2` |
| Venv encode | [`embeddings_index.embed_texts`](../plugin/scripting/embeddings_index.py) | Same shape as worker `result` dict; lazy `SentenceTransformer` cache per model name |
| IPC transport | `run_code_in_user_venv` + fixed stub | `session_id=f"embeddings:{model_slug}"` reuses loaded model across calls; timeout from `scripting.python_exec_timeout` |
| Whitelist | [`sandbox_imports.py`](../plugin/scripting/sandbox_imports.py) | `plugin.scripting.embeddings_index` allowed for stub import only |

**Not built in Phase A (do not assume these exist):** `index.db`, folder corpus key helper, paragraph chunker, `knn_search`, `search_embeddings` tool, background indexer, sqlite-vec / vec0 DML, host `sqlite3` locator writes.

**Phase B should reuse `embedding_client.embed_texts`** for any host-side batch encode during indexing (e.g. tests, small batches). Index-time embed at scale and query-time search should add new functions on **`embeddings_index`** (venv opens `index.db`, writes vec0 or BLOBs, runs KNN) — same trusted-module pattern as encode, new fixed stubs from a future host `embeddings_service.py` or similar.

### Phase B — Minimal index + `search_embeddings` tool {#phase-b}

**Shipped.**

**Goal:** outer [document_research](../plugin/doc/document_research.py) calls `search_embeddings(query, k)` → ranked hits in the **active folder’s** cache → open top files → inner read at locator.

**Search mode (compile-time):** `DOCUMENT_RESEARCH_SEARCH_MODE` in [`constants.py`](../plugin/framework/constants.py) — `"grep"` exposes `grep_nearby_files`; `"embeddings"` exposes `search_embeddings` only (see [Search mode flag](#search-mode-flag) below).

**Suggested implementation order** (each step should have tests before moving on):

1. **Folder key + cache paths (host, stdlib only)** — new module e.g. `plugin/doc/embeddings_cache.py`:
   - `folder_corpus_key(directory_path) -> str` — stable hash/normalized path (same sibling scope as [`list_nearby_files`](../plugin/doc/document_research.py))
   - `index_db_path(ctx, folder_key) -> Path` under `…/user/writeragent_embeddings/<folder_key>/index.db` (beside `writeragent.json`)
   - Host creates `chunks` + `corpus_meta` tables ([Corpus storage](#corpus-storage)); no vec extension on host

2. **Paragraph chunker + locator capture (host)** — extract indexable paragraphs from siblings (reuse document_research read-only extract / ODT path from bench); per paragraph: `doc_url`, `para_index`, `char_start`, `char_end`, `content_hash` ([Chunking](#chunking) — paragraph grain for MVP)

3. **Extend `embeddings_index` (venv)** — add encode+persist and search alongside existing `embed_texts`:
   - `index_paragraphs(db_path, model, rows)` — batch embed changed texts, write `vec_chunks` (vec0) or `chunks.embedding` BLOB fallback ([Search fallback](#search-fallback))
   - `knn_search(db_path, query_text, k)` or `knn_search(db_path, query_vec, k)` — vec0 `MATCH` when `sqlite_vec` loads; else NumPy dot + top-k (port search half of [`bench_embeddings.py`](../scripts/bench_embeddings.py))
   - Probe sqlite-vec once at module load; log fallback at debug

4. **`search_embeddings` tool** — register on outer document_research surface ([`document_research_tools.py`](../plugin/doc/document_research_tools.py)); resolve folder from active doc; call venv `knn_search` with `index.db` path reference; return `{doc_url, para_index, char_start, char_end, score}[]`

5. **Background folder indexer (host thread + venv IPC)** — [Background folder indexer](#background-folder-indexer): cold build + mtime/hash incremental refresh; **must not block** tool loop; enqueue on document_research start or first `search_embeddings` miss

6. **Prompt / delegate wiring** — mode-specific hints in [`specialized_base.py`](../plugin/doc/specialized_base.py) via [`get_document_research_workflow_hint`](../plugin/doc/document_research.py)

**Phase B checklist:**

- [x] Paragraph chunker with **locator capture** (`para_index`, `char_start`, `char_end`, `content_hash`)
- [x] Persist per-folder `index.db` under profile cache dir ([Corpus cache layout](#corpus-cache-layout), [Corpus storage](#corpus-storage))
- [x] **`search_embeddings`** on outer document_research tool surface (mutually exclusive with grep via `DOCUMENT_RESEARCH_SEARCH_MODE`)
- [x] Open top 1–few hits → `delegate_read_document` → inner read at locator (prompt guidance)
- [x] Search executes in the venv (worker opens the shared `index.db` by the folder/db reference passed over the RPC stub): sqlite-vec `vec0` KNN when available; else NumPy dot + top-k against the on-disk BLOBs ([Search fallback](#search-fallback))
- [x] Background **index maintenance worker** (separate from agent tool loop) — [Background folder indexer](#background-folder-indexer)

### Search mode flag {#search-mode-flag}

Cross-file discovery tools are **mutually exclusive** at build time:

| `DOCUMENT_RESEARCH_SEARCH_MODE` | Registered | Hidden (`ToolBaseDummy`) |
|---------------------------------|------------|----------------------------|
| `"grep"` (default) | `grep_nearby_files` | `search_embeddings` |
| `"embeddings"` | `search_embeddings` | `grep_nearby_files` |

Edit the constant in [`constants.py`](../plugin/framework/constants.py) before `make release`. No Settings UI yet. `list_nearby_files` and `delegate_read_document` are always available.

### Corpus cache layout {#corpus-cache-layout}

**Per-directory only:** one **`index.db`** per indexed **folder**, holding locators and **`vec0` vectors** for **every indexable file in that folder** ([Scope](#per-directory-cache)). Schema: [Corpus storage](#corpus-storage).

Keep vectors **out of the user’s document folders** — but **do not hide** the cache. Under the WriterAgent user profile (same tree as `writeragent.json`), use a normal, user-visible directory:

```text
…/user/writeragent_embeddings/
  <folder_corpus_key>/          # hash or normalized path of indexed sibling directory
    index.db                    # SQLite: chunks (locators) + vec0 (embeddings) + corpus_meta
```

| Table / object | Contents |
|----------------|----------|
| **`chunks`** | One row per indexed paragraph — `chunk_id`, `doc_url`, `para_index`, offsets, `content_hash`, **`file_mtime`**, **`last_indexed_at`**, optional `embedding` BLOB (fallback path) |
| **`vec_chunks` (`vec0`)** | sqlite-vec virtual table — `chunk_id`, normalized float32 **embedding**; KNN via `MATCH` |
| **`corpus_meta`** | `embedding_model`, `dim`, `schema_version`, storage backend flag (`vec0` vs `blob_fallback`) |

One **`index.db` per directory** (sibling folder around the active doc), never per open document and never one file for the whole profile. Settings / help: *semantic search cache for a folder — vectors from files in that directory only; delete a subfolder under `writeragent_embeddings/` to force re-index for that directory.*

**Linux example:** `~/.config/libreoffice/4/user/writeragent_embeddings/` (or `…/24/user/` depending on profile).

### Background folder indexer {#background-folder-indexer}

Indexing and refresh run on a **background maintenance worker** (host thread + venv IPC) — **not** inside the document_research tool loop and not blocking the outer agent’s LLM turns. **Wakeups:**

| Trigger | When |
|---------|------|
| **Periodic tick** | Every `EMBEDDINGS_INDEX_INTERVAL_S` (default **300 s / 5 min**) for the **active document’s folder** — started once per process from sidebar wiring ([`embeddings_periodic.py`](../plugin/doc/embeddings_periodic.py)) when `DOCUMENT_RESEARCH_SEARCH_MODE=embeddings` |
| **document_research** | Outer delegate starts in a folder ([`specialized_base.py`](../plugin/doc/specialized_base.py)) |
| **search miss** | `search_embeddings` against empty/stale cache ([`document_research_search_tool.py`](../plugin/doc/document_research_search_tool.py)) |

Only one folder job runs at a time per folder key (`_inflight` guard in [`embeddings_indexer.py`](../plugin/doc/embeddings_indexer.py)); periodic ticks are no-ops while a job is already queued or running.

**Two modes:**

| Mode | When | Work |
|------|------|------|
| **Cold build** | No cache for folder, or `embedding_model` changed | Index **all** indexable siblings — full paragraph extract, batch embed, write cache |
| **Incremental refresh** | Cache exists | Per file: compare **file mtime** vs **`last_indexed_at`**; only if stale, extract paragraphs and **paragraph-hash diff** (below) |

**Incremental refresh (default once cache exists):**

1. List sibling files in the folder (same extensions as `list_nearby_files`).
2. For each `doc_url`, read filesystem **mtime** (last modified) and compare to **`last_indexed_at`** stored in the cache for that file.
3. **`mtime ≤ last_indexed_at`** (and model unchanged) → **skip file** — no extract, no embed.
4. **File may have changed** → read-only extract (same path as document_research) → compute **`content_hash` per paragraph** → compare to locator rows.
5. Send **only paragraphs with new or changed hashes** to the embedder (batch RPC). Unchanged paragraphs keep existing vectors.
6. The background worker passes the changed paragraphs + a reference to the folder's `index.db` to the venv. The trusted module opens the DB, batch-embeds the changed paragraphs, and patches `vec0` (or the `embedding` BLOB column) plus locator rows in one transaction (`UPDATE`/`DELETE`/`INSERT`); sets **`last_indexed_at`** and **`file_mtime`**. The host may also perform some `chunks` writes directly using its stdlib connection to the same file. See [Search fallback](#search-fallback).
7. **Save with unchanged content:** if mtime bumped but hash diff finds nothing to embed or delete, the host calls **`mark_file_indexed`** ([`embeddings_indexer.py`](../plugin/doc/embeddings_indexer.py)) to advance `last_indexed_at` / `file_mtime` without a venv RPC — avoids re-scanning the same file on every periodic tick.

Search always uses the **current** index ([Always search](#always-search-update-in-the-background)); maintenance catches up in the background via **mtime + hash diff** on the periodic and event-driven wakeups above. Index may be a few minutes stale — acceptable for cross-file semantic find.

```mermaid
flowchart TB
  Wake["Wakeup:\nperiodic tick,\ndocument_research,\nor search miss"]
  Worker["Background index\nmaintenance worker"]
  Cold{"Cache exists\nfor folder?"}
  Full["Cold: index all\nsiblings"]
  Inc["Incremental:\nmtime vs last_indexed"]
  Hash["Paragraph hash\ndiff per stale file"]
  Embed["Venv batch embed\nchanged paras only"]
  Wake --> Worker
  Worker --> Cold
  Cold -->|no| Full
  Cold -->|yes| Inc
  Inc --> Hash
  Full --> Embed
  Hash --> Embed
```

Do not block `search_embeddings` or document_research on embed completion; enqueue work and return ranked hits from whatever index is on disk.

### Corpus storage (BLOB + NumPy default) {#corpus-storage}

**Default (supported path today):** one `index.db` per directory. Locator rows and **normalized float32 vectors** live in **`chunks`** (`embedding` BLOB column). Search in the venv: load BLOBs → `np.stack` → `np.dot` + top-k ([bench path](#benchmark-on-your-machine)). Incremental maintenance uses **`UPDATE` / `DELETE` / `INSERT`** — on-disk size tracks **live chunk count × dim**, not edit history. **No append-only vector logs or snapshot chains.**

**Optional later:** sqlite-vec **`vec0`** (`vec_chunks`) when installed — code auto-probes at worker startup ([Search fallback](#search-fallback)). **Do not treat vec0 as a dependency** for MVP, dogfood, or venv setup; folder-sized corpora are expected to stay on the NumPy path unless profiling says otherwise.

```mermaid
flowchart LR
  subgraph primary [Default today]
    NumPy["NumPy np.dot + top-k\nBLOB column in chunks"]
  end
  subgraph optional [Optional later]
    Vec0["vec0 KNN\nsqlite-vec in venv"]
  end
  Query["search_embeddings"] --> Try{"sqlite_vec\nimportable?"}
  Try -->|no| NumPy
  Try -->|yes| Vec0
```

The worker is given a path/reference to the DB file and performs open / search (or DML) locally against the on-disk data. No full corpus matrix is shipped over IPC for search.

#### Shared invariants

| Concern | Rule |
|---------|------|
| Scope | One `index.db` per **directory** under `writeragent_embeddings/<folder_corpus_key>/` |
| Metadata | Locators in `chunks`: `doc_url`, `para_index`, offsets, `content_hash`, `file_mtime`, `last_indexed_at`, `embedding_model` |
| Incremental logic | mtime skip → hash diff → batch embed **changed paragraphs only** ([Background folder indexer](#background-folder-indexer)) |
| Model change | Cold rebuild entire folder cache |
| Search latency class | Lazy ~60 s background OK; search reads **current** index, may be briefly stale |

#### Schema (vec0 path)

```sql
-- Host creates locator table (stdlib sqlite3 on index worker thread)
CREATE TABLE chunks (
  chunk_id INTEGER PRIMARY KEY,
  doc_url TEXT NOT NULL,
  para_index INTEGER NOT NULL,
  char_start INTEGER,
  char_end INTEGER,
  content_hash TEXT NOT NULL,
  file_mtime REAL,
  last_indexed_at REAL,
  embedding_model TEXT NOT NULL,
  embedding BLOB  -- optional mirror for NumPy fallback; omit if vec0-only
);
CREATE TABLE corpus_meta (key TEXT PRIMARY KEY, value TEXT);

-- Venv fixed RPC: sqlite_vec.load(db) then create vec0 (dim fixed at cold build)
CREATE VIRTUAL TABLE vec_chunks USING vec0(
  chunk_id INTEGER PRIMARY KEY,
  embedding float[384]  -- dim from embedding_model / corpus_meta
);
```

| Operation | vec0 path |
|-----------|-----------|
| **Changed paragraph** | `UPDATE vec_chunks SET embedding = ? WHERE chunk_id = ?`; sync `chunks.content_hash` |
| **New paragraph** | `INSERT INTO chunks …`; `INSERT INTO vec_chunks …` |
| **Deleted paragraph** | `DELETE FROM vec_chunks WHERE chunk_id = ?`; `DELETE FROM chunks …` |
| **Search** | `SELECT chunk_id, distance FROM vec_chunks WHERE embedding MATCH ? ORDER BY distance LIMIT ?` in venv RPC |

NumPy arrays pass straight into sqlite-vec (`embedding.astype(np.float32)` — see [sqlite-vec Python docs](https://alexgarcia.xyz/sqlite-vec/python.html)).

**Host vs venv (shared DB file):** the *same* `index.db` file is the coordination point and is opened by both processes. The host uses plain stdlib `sqlite3` (no loadable extensions required) to manage the `chunks` locator/metadata table, perform mtime + hash diff decisions in the background indexer, and orchestrate work. The trusted module in the venv is given a path/reference over the RPC, opens the identical file, and (if `sqlite_vec` is importable) loads the extension to create/use the `vec0` virtual table for storage and KNN. Even without sqlite-vec the worker opens the same DB to read/write the `embedding` BLOB column and runs the NumPy path locally. SQLite's normal multi-process concurrency rules apply; the worker performs the vector-sensitive DML and search while the host owns most metadata logic. LLM / `=PYTHON()` scripts remain sandboxed — they must not import `sqlite3` or open index paths directly.

#### Search fallback {#search-fallback}

At worker startup (or first index open), **probe** `import sqlite_vec` and `sqlite_vec.load()` on a throwaway `:memory:` connection. **Most installs should omit sqlite-vec** and stay on the BLOB + NumPy path.

| Condition | Persist | Search |
|-----------|---------|--------|
| **sqlite-vec not installed / load fails (expected MVP)** | `chunks.embedding` BLOB only; `corpus_meta.storage_backend=blob_numpy` | Load all BLOBs → `np.stack` → `np.dot` + top-k ([bench path](#benchmark-on-your-machine)) |
| **sqlite-vec OK (optional)** | `vec_chunks` vec0 (+ optional BLOB mirror) | vec0 `MATCH` KNN in venv |

Log once at debug level when vec0 is unavailable or when vec0 is used. Do **not** fail indexing if `sqlite-vec` is missing — that is the **normal** supported configuration today.

**Anti-pattern — do not use:** append-only vector logs, dual-file `vectors.bin` sidecars that grow on every edit without reclaim, or versioned snapshot chains.

#### Installing sqlite-vec in the user venv {#installing-sqlite-vec}

> **Optional — not required for MVP.** Skip this section unless profiling on **your** folder corpora shows NumPy BLOB search is too slow. Minimum venv: **`pip install numpy sentence-transformers`** ([Venv setup](#local-embedders-mvp)).

WriterAgent reads **`scripting.python_venv_path`** ([enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md)) — install packages **into that venv**, not system Python. The PyPI wheel bundles the sqlite-vec loadable extension; `pip install sqlite-vec` is the supported path when you choose to enable vec0 ([upstream install guide](https://github.com/asg017/sqlite-vec/blob/main/site/getting-started/installation.md)).

**All platforms (recommended):**

```bash
# Replace with your actual venv path from WriterAgent Settings
VENV=/path/to/your/writeragent/venv

"$VENV/bin/pip" install numpy sentence-transformers sqlite-vec
# sentence-transformers pulls PyTorch CPU; first run downloads model weights.
```

**Verify:**

```bash
"$VENV/bin/python" -c "
import sqlite3, sqlite_vec
db = sqlite3.connect(':memory:')
db.enable_load_extension(True)
sqlite_vec.load(db)
db.enable_load_extension(False)
print('vec_version=', db.execute('select vec_version()').fetchone()[0])
"
```

**Arch Linux notes:**

Arch marks system Python as [externally managed (PEP 668)](https://peps.python.org/pep-0668/) — **`pip install` on `/usr/bin/python3` fails** unless you use a venv. That matches WriterAgent’s design: always use the configured venv subprocess, never LibreOffice’s embedded interpreter for sqlite-vec.

1. **Use the WriterAgent venv (required):**

```bash
# Example: venv already pointed at by scripting.python_venv_path
VENV="$HOME/Desktop/Python/venv"   # adjust to your path

"$VENV/bin/pip" install numpy sentence-transformers sqlite-vec
```

2. **If the venv has no pip** (fresh `python -m venv` on Arch sometimes needs ensurepip):

```bash
pacman -S python-pip    # optional: pacman helper; still install into venv below
"$VENV/bin/python" -m ensurepip --upgrade
"$VENV/bin/pip" install numpy sentence-transformers sqlite-vec
```

3. **AUR (`python-sqlite-vec`) — not a substitute:** [`python-sqlite-vec`](https://aur.archlinux.org/packages/python-sqlite-vec) installs into **system** site-packages via an AUR helper (`yay -S python-sqlite-vec`). WriterAgent’s warm worker uses **`scripting.python_venv_path`**, so you still need **`pip install sqlite-vec` inside that venv**. The AUR package is only relevant if you deliberately run the venv’s Python against system packages (unusual — do not rely on it).

4. **SQLite version:** vec0 works best with SQLite **≥ 3.41**. Check the **venv** interpreter, not system `sqlite3`:

```bash
"$VENV/bin/python" -c "import sqlite3; print(sqlite3.sqlite_version)"
```

Python 3.12+ venvs on Arch usually ship a recent SQLite. If `enable_load_extension` is missing (some macOS system Pythons), use Homebrew Python or `pysqlite3` — see [sqlite-vec Python docs](https://alexgarcia.xyz/sqlite-vec/python.html).

**Do not** vendor sqlite-vec into the OXT or load it in LibreOffice’s embedded Python — venv trusted module only ([Trusted extension code in the venv](enabling_numpy_in_libreoffice.md#trusted-extension-code-in-the-venv)).

#### Rejected alternatives (historical)

| Alternative | Why not default |
|-------------|-----------------|
| Dual-file `index.db` + `vectors.bin` | Two-file sync; append-only `.bin` risk |
| BLOB-only + always IPC full matrix | Works as **fallback**, but vec0 avoids loading all vectors at large N |
| Full sidecar rewrite each batch | Write amplification on small edits |

### Phase C — Incremental maintenance {#phase-c-incremental}

> **Superseded (likely indefinitely):** live edit hooks are **not on the roadmap**. Periodic background refresh ([Background folder indexer](#background-folder-indexer)) — **mtime vs `last_indexed_at`**, paragraph **`content_hash`** diff, and **`mark_file_indexed`** when content is unchanged — is the maintained strategy. A few minutes of index staleness is acceptable for cross-file semantic find.

**Shipped via Phase B + periodic worker:**

- [x] Paragraph `content_hash`; skip embed when hash unchanged
- [x] Vector patch in place (`vec0` + `chunks`, or BLOB fallback) per [Corpus storage](#corpus-storage)
- [x] Periodic background folder indexer (`EMBEDDINGS_INDEX_INTERVAL_S`)
- [x] `mark_file_indexed` when mtime changes but hashes match

**Not planned (original Phase C design — kept as historical notes below):**

- [ ] **`XProofreading` change hook**
- [ ] **~60 s debounced worker** per `doc_url` on every keystroke
- [ ] Dirty marks from write tools
- [ ] Supersede keys like [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py)

#### `XProofreading` incremental hook {#xproofreading-incremental-hook}

> **Historical / not planned.** The design below described a parallel proofreading hook for ~1-minute-fresh indexes. **Periodic mtime refresh replaces this** — less code, no grammar coupling, and acceptable latency for document_research.

Writer already calls [`doProofreading`](../plugin/writer/locale/ai_grammar_proofreader.py) on the **`XProofreading`** linguistic path whenever the user types — that is how the native grammar proofreader learns which **text slice** changed. Embeddings maintenance **reuses that entry point** but is a **separate code path**:

| | Grammar proofreader | Embeddings indexer |
|--|---------------------|-------------------|
| **UNO entry** | `XProofreading.doProofreading` | Same call site (parallel hook) |
| **Work** | LLM grammar JSON + squiggles | Paragraph hash diff → venv batch re-embed |
| **Latency** | ~1 s quiet window | **~60 s** quiet window before any embed work |
| **User-visible** | Underlines | None (background index only) |

**Do not** run embed logic inside the grammar proofreader class or share grammar’s sentence queue. Add a thin **embeddings listener** invoked from the same `doProofreading` dispatch (or shared pre-hook) that:

1. Maps the proofread buffer slice to **paragraph index + normalized text** (same BreakIterator / paragraph boundaries grammar already uses — see [`grammar_proofread_text.py`](../plugin/writer/locale/grammar_proofread_text.py)).
2. Compares `content_hash` to the locator row for `(doc_url, para_index)`.
3. On mismatch, **marks paragraph dirty** and resets a **60 s idle timer** for that document — no venv call yet.

When the timer fires (document quiet for **one minute**), drain all dirty paragraphs for that `doc_url` in one batch embed RPC, patch `vec_chunks` + `chunks`, update locator rows. Supersede inflight work if the user keeps typing (same supersede pattern as grammar’s `enqueue_seq`, different timeout constant).

Grammar can be off while embeddings indexing stays on (separate config flags). External saves and non-Writer edits still converge via **mtime + hash diff on open** and the background folder indexer.

### Phase D — Optional later

- Main chat runs index before delegate; pass locators in task string
- In-document chunk inject beside `[DOCUMENT CONTENT]` for one huge file ([Within-document retrieval](#within-document-retrieval-secondary))
- Cloud embed tier-two when no venv ([Cloud embedding APIs](#cloud-embedding-apis-tier-two))

---

## Within-document retrieval (secondary)

For the **active document only**:

- Writer/Calc already expose fast keyword/outline search to tools and users (`search_in_document`, outline helpers, sheet navigation).
- Injecting extra chunks from an embedding index on every chat send is **optional** — useful when the 8k excerpt misses a distant section in a **single** 200-page file, not the usual case.
- Implement after the **corpus index** proves value; same chunker and storage, scoped by `doc_url`.

---

## Architecture

WriterAgent runs NumPy, **sqlite-vec**, and **sentence-transformers** **only in the user venv subprocess** ([`PythonWorkerManager`](../plugin/scripting/venv_worker.py)). LibreOffice's embedded interpreter stays stdlib — no NumPy or sqlite-vec in-process.

```mermaid
flowchart TB
  subgraph host [LO_host]
    Outer["Outer document_research"]
    IndexDB["index.db (shared on-disk file)\nlocators + vec0 / BLOBs"]
    RPC["embed / search RPC (pass db reference)"]
  end
  subgraph venv [Warm_venv_worker]
    ST["SentenceTransformer\nbatch encode"]
    Search["open same DB file\nsqlite-vec KNN or NumPy"]
  end
  Outer --> RPC
  RPC -->|"Pickle5: texts (index) or query+ref (search)"| ST
  ST -->|"worker opens IndexDB by ref"| Search
  Search -->|"small results (locators+scores)"| RPC
  RPC --> Outer
```

**Split responsibilities (shared on-disk DB + reference passing):**

```
┌─────────────────────────────────────────────────────────────┐
│ LibreOffice host (embedded Python — stdlib)                  │
│  • Chunk / extract paragraphs; compute mtime + content_hash  │
│  • Open index.db with plain sqlite3 for `chunks` + meta      │
│  • Background maintenance worker (orchestration + decisions)  │
│  • Pass db_path / folder reference + texts or query over RPC │
│  • Optional HTTP embed when no venv (tier two)               │
└───────────────────────────┬─────────────────────────────────┘
                            │ Pickle5 RPC (texts or query + db reference)
┌───────────────────────────▼─────────────────────────────────┐
│ User venv — warm worker (trusted module, same =PYTHON() venv)│
│  • Opens the *same* index.db file by the reference passed in  │
│  • sentence-transformers — lazy load, batch encode             │
│  • sqlite-vec (if present) — vec0 storage + KNN in the DB    │
│  • or BLOB column + NumPy dot+top-k against the opened DB    │
│  • Returns only compact locator lists / small results        │
└─────────────────────────────────────────────────────────────┘
```

The `index.db` lives on the filesystem and is the single source of truth for both locators and vectors. Standard SQLite (no vec extension) is sufficient for the host and for the fallback search path; both processes can safely open it at the same time.

**MVP path:** the worker receives a *reference* to the shared on-disk `index.db` (plus small payloads) over the RPC and opens the file itself. Encode and search (vec0 `MATCH` when available, otherwise BLOBs + NumPy dot + top-k) happen inside the trusted module in the venv. The bench validates the pure-NumPy path at 349 paragraphs (dot+top-k **0.17 ms** median). See [Development plan](#development-plan), [Installing sqlite-vec](#installing-sqlite-vec), and [Trusted extension code in the venv](enabling_numpy_in_libreoffice.md#trusted-extension-code-in-the-venv). The on-disk DB (opened from both sides via standard sqlite3) is what enables passing references instead of bulk vector data.

**Do not** add `sqlite3` / `os` to the **LLM** import whitelist to support embeddings — implement `open()` and vec0 inside a shipped `plugin.scripting.*` module called from a fixed host stub.

---

## How embeddings work

### Meaning signatures

An embedding is a fixed-length list of floats (e.g. 384 or 1536) representing a chunk of text in multi-dimensional space. Chunks from **many files** live in one index; a query vector compares against all of them to surface the best **document + passage** matches.

- "The dog is barky" and "Canine vocalization" are **close together**.
- "The dog is barky" and "Pythons are interpreted languages" are **very far apart**.

### Closeness = angle, not words

We compare **angles** between vectors. If two vectors point in roughly the same direction, the texts have similar meanings.

- **Dot product**: multiply values at each index and sum.
- **Cosine similarity**: dot product of two **normalized** vectors (length 1.0).

> **Optimization:** Normalize vectors **once** when stored (or when received from the API). Cosine search then reduces to a fast **dot product** scan.

---

## Why NumPy stays in the venv {#why-numpy-stays-in-the-venv}

NumPy carries a heavy "tax" inside a LibreOffice `.oxt`:

- **Binary size**: ~50–100 MB per platform.
- **Complexity**: packaging for Windows, macOS (Intel + Silicon), and Linux (x86 + ARM) is a maintenance nightmare.

**WriterAgent's solution (shipped):** NumPy and **sentence-transformers** run **only in the user venv subprocess** — see [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md). Host↔venv uses **Pickle5** by default (3.11–3.14). For MVP, **encode and search both stay in the venv** over IPC ([Development plan](#development-plan)).

---

## Embedding inference

Two tiers. **Shipped today (Phase A):** local **`sentence-transformers`** in the configured venv only — via [`embedding_client.embed_texts`](../plugin/framework/client/embedding_client.py). **Tier two (not implemented):** OpenRouter / Together / Ollama HTTP when no venv.

**Current dispatch:** `embedding_provider` must be `local` (default). Host calls `run_code_in_user_venv` with a fixed stub → [`embeddings_index.embed_texts`](../plugin/scripting/embeddings_index.py). Requires `scripting.python_venv_path` (or LO fallback interpreter) with `pip install sentence-transformers numpy`.

**Future dispatch (when HTTP ships):** if venv + local model → venv RPC; else if chat endpoint supports embeddings → HTTP; else prompt user to configure venv or API.

**Phase B note:** indexing and search should call **new** `embeddings_index` functions for vec0/BLOB persist and KNN; keep using `embedding_client.embed_texts` only where the host needs raw vectors without touching `index.db`.

---

## Local embedders (MVP) {#local-embedders-mvp}

### Why sentence-transformers is tier one

- **Already fits the venv bridge** — same `PythonWorkerManager` + Pickle5 path as NumPy calc scripts; nothing heavy in LibreOffice.
- **Offline indexing** — embed a whole folder without API keys or rate limits; incremental paragraph re-embed stays cheap.
- **CPU-viable** — many small models encode hundreds of paragraphs in seconds on a laptop when you **batch** and use **NumPy dot products** for search (not Python loops).
- **Same stack as dedup/search prototypes** — proven patterns from [`embeddings_dedup.py`](file:///home/keithcu/Desktop/LinuxReport/embeddings_dedup.py) (LinuxReport project): lazy-loaded model, batch `encode(..., convert_to_tensor=False)`, L2-normalized vectors, `np.dot` for cosine.

### Performance lessons (slow first version → fast second)

The LinuxReport dedup code documents a real refactor:

| Approach | Behavior | Typical cost (200 texts) |
|----------|----------|---------------------------|
| **Slow (v1)** | Per-text encode or Python loop over pairs | ~1.5–2.0 s |
| **Fast (v2)** | Batch `encode` + `np.stack` + matrix `np.dot` | ~0.002 s (~**700–800×** in their benchmark) |

WriterAgent should **never** embed or rank one paragraph at a time in a Python loop for corpus work. MVP pipeline:

1. **Lazy-load** one `SentenceTransformer` per worker process (amortize model load).
2. **Batch** all paragraphs needing embed in one `encode(valid_texts, convert_to_tensor=False)` call.
3. **Normalize once** → float32 in **`chunks.embedding` BLOB** (and vec0 when sqlite-vec is installed).
4. **Query:** encode query → NumPy `np.dot` + top-k in venv ([bench sample](#benchmark-on-your-machine)); vec0 `MATCH` only when sqlite-vec is present.

Optional in-worker **text hash cache** during a single index pass (like LinuxReport's `embedding_cache` dict) avoids re-encoding identical paragraphs across files; persistent dedup uses **`content_hash`** in SQLite instead of storing raw text.

### Model shortlist (beyond legacy MiniLM-only defaults)

`all-MiniLM-L6-v2` (384-dim, ~22M params) is the old default everyone knows — still a solid **baseline**, but test alternatives on **your** CPU before locking config:

| Model (HF id) | Dim | Lean / quality | Notes |
|---------------|-----|----------------|-------|
| **`all-MiniLM-L6-v2`** | 384 | Fastest baseline | LinuxReport default; good for benchmarking “classic” speed. |
| **`BAAI/bge-small-en-v1.5`** | 384 | Fast, strong retrieval | Popular RAG choice; often beats MiniLM on MTEB retrieval at similar size. |
| **`intfloat/e5-small-v2`** | 384 | Fast | Prefix `"query: "` / `"passage: "` at encode time (library or prompt wrapper). |
| **`Snowflake/snowflake-arctic-embed-xs`** | 384 | Fast, newer | Competitive small encoder; worth A/B vs MiniLM. |
| **`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`** | 384 | Medium speed | Non-English folders. |
| **`all-mpnet-base-v2`** | 768 | Slower, higher quality | When CPU budget allows; ~2× dim → larger `index.db`. |
| **`BAAI/bge-base-en-v1.5`** | 768 | Medium–slow | Step up from bge-small when quality gaps show in testing. |
| **`nomic-embed-text-v1.5`** | 768 | Via ST or Ollama | Long-context friendly; heavier — profile before corpus index. |

**Ollama-local** (`nomic-embed-text`, `embeddinggemma`, …) is an alternative **local** path without `sentence-transformers` in venv — still tier one “local”, different packaging. Pick one local stack per install (ST in venv **or** Ollama HTTP to localhost), not both for the same index.

Store **`embedding_model`** in config as the HuggingFace id (local) or provider model string (cloud). Changing model requires cold rebuild of folder `index.db` ([Corpus storage](#corpus-storage)).

### Venv setup (MVP)

**Minimum (required):**

```bash
# In the venv referenced by scripting.python_venv_path
pip install sentence-transformers numpy
# PyTorch CPU wheel is pulled by sentence-transformers; first run downloads model weights.
```

**Optional:** `pip install sqlite-vec` — enables vec0 KNN when load succeeds; see [Installing sqlite-vec](#installing-sqlite-vec). Not needed for dogfood.

Warm worker loads the model once; subsequent index batches reuse it (same pattern as LinuxReport's global `embedder` lazy init).

### Benchmark on your machine {#benchmark-on-your-machine}

Run [`scripts/bench_embeddings.py`](../scripts/bench_embeddings.py) — document-sized encode + query timing via the **warm venv worker** (Pickle5 IPC, not worker-side file I/O):

```bash
python scripts/bench_embeddings.py
python scripts/bench_embeddings.py --models all-MiniLM-L6-v2,BAAI/bge-small-en-v1.5
```

Uses [`scripts/longdocsample.odt`](../scripts/longdocsample.odt) (349 non-empty paragraphs). Flow:

1. Host extracts paragraphs (stdlib ODT unzip) and passes the text list via worker **`data=`**.
2. **Encode bench:** lazy `SentenceTransformer`, one batch `encode(all_paragraphs)`; `corpus_matrix` stays in worker session.
3. **Search bench:** median over `--search-iters` (default 50) for query encode, `np.dot` + top-k, and combined query time.
4. Host optionally writes `/tmp/writeragent_embed_paragraphs.json` and `/tmp/writeragent_embed_sidecar.bin` for inspection.

**Sample result (Arch Linux, `/home/keithcu/Desktop/Python/venv`, 2026-06):**

| Metric | `all-MiniLM-L6-v2` |
|--------|-------------------|
| Paragraphs / dim | 349 / 384 |
| Sidecar | 0.51 MiB |
| Model load | 2.810 s |
| Batch encode (corpus) | 1.062 s |
| Query encode (median) | 3.715 ms |
| Dot + top-k (median) | 0.167 ms |
| Query total (median) | 3.879 ms |

Top hit for query *"offline-first data collection systems KoboToolbox"*: para 245 (0.84), then title para 0 (0.76). Dot+top-k at sub-ms validates the vectorized search path from LinuxReport [`embeddings_dedup.py`](file:///home/keithcu/Desktop/LinuxReport/embeddings_dedup.py).

Repeat with **2–3 models** from the [shortlist](#local-embedders-mvp); record dim, encode s, query ms, sidecar MB (`N × dim × 4`). Log machine, Python version, and BLAS backend when comparing runs.

**Reference implementation:** [`embeddings_dedup.py`](file:///home/keithcu/Desktop/LinuxReport/embeddings_dedup.py) — `get_embeddings`, `_compute_cosine_similarities` (batch + NumPy). Port the **batch/NumPy** shape into WriterAgent's venv embed module as the **search fallback**; primary persist/search uses sqlite-vec `vec0` ([Corpus storage](#corpus-storage)).

---

## Cloud embedding APIs (tier two)

Use when no venv, or when you want hosted large models (e.g. OpenRouter `text-embedding-3-large`) without local GPU/CPU load.

### OpenRouter

- Endpoint: `POST https://openrouter.ai/api/v1/embeddings`
- Same API key as chat. Request: `model`, `input` (string or array of strings).
- Optional: `dimensions`, `encoding_format`, `input_type`, `provider`.

### Together AI

- Endpoint: `{configured_endpoint}/embeddings` (OpenAI-compatible).
- Models: e.g. BAAI/bge-large-en-v1.5, togethercomputer/m2-bert-80M-8k-retrieval.

### Ollama (local HTTP, no ST venv)

- Endpoint: `POST {base}/api/embed`
- Models: `nomic-embed-text`, `all-minilm`, `embeddinggemma` — local process, not LibreOffice.

### Config

- **`embedding_model`** + **`embedding_model_lru`** (mirror [`get_image_model`](../plugin/framework/client/model_fetcher.py)).
- **`embedding_provider`**: `local` (sentence-transformers in venv) | `openrouter` | `together` | `ollama` — auto-detect from model id / endpoint when unset.

---

## Index size growth {#index-size-growth}

On-disk size scales with **indexed paragraph count**, not raw `.odt` bytes. Vectors dominate.

**Formula (BLOB path, float32):**

```text
vector_bytes ≈ num_chunks × dim × 4
```

Default model `all-MiniLM-L6-v2` (384-dim) → **~1.5 KiB per non-empty paragraph** + small locator overhead per row.

**Example unit:** [`scripts/longdocsample.odt`](../scripts/longdocsample.odt) — **349** non-empty paragraphs → **~0.51 MiB** vectors only ([benchmark](#benchmark-on-your-machine)).

| Files (longdoc-sized) | Paragraphs | Vector data | `index.db` (rough) |
|----------------------|------------|-------------|---------------------|
| 1 | 349 | 0.5 MiB | ~0.6 MiB |
| 10 | 3,490 | 5 MiB | ~6 MiB |
| 100 | 34,900 | 51 MiB | ~55–65 MiB |

768-dim models double vector storage. Short documents contribute far less. Incremental edits patch rows in place — size tracks **live** chunk count, not edit history.

**RAM (search, BLOB path):** each cold load materializes roughly the vector column into a NumPy matrix — e.g. **~50–60 MiB** peak for the 100-longdoc example. That motivates [in-worker corpus cache](#embeddings-in-worker-cache) for repeat queries without re-reading disk every time.

---

## Persistence — keep the cache small {#minimal-index}

**Goal:** one compact **`index.db`** per directory. Vectors in **`vec0`**; metadata in **`chunks`** — locators only in row columns, **no FTS shadow index**. See [Corpus storage](#corpus-storage).

### What we store

| Part | Contents | Size driver |
|------|----------|-------------|
| **`vec_chunks` (vec0)** | Normalized float32 embeddings (primary) | `n × dim × 4` bytes |
| **`chunks` rows** | `chunk_id`, `doc_url`, locators, `content_hash`, optional `embedding` BLOB (fallback) | Tiny per row |

### Locator fields (dig up original text later)

Persist enough to re-read from LO after opening **one** file:

- **`doc_url`** — which file.
- **`doc_revision`** — invalidate when file changes.
- **`para_index`** — paragraph (or outline node) in Writer; analogous anchor for Calc sheet / Draw page when extended.
- **`char_start`**, **`char_end`** — character offsets within that paragraph (or range within sheet cell block).
- **`chunk_id`** — joins `chunks` to `vec_chunks` vec0 row.

At **index time**, extract a chunk of text → **venv batch encode** (MVP) or HTTP embed → write vector + locator → **do not** persist the chunk body. Optional: keep a **short hash** of source text to detect drift; not the text itself.

At **query time**, top-k returns locators → outer agent opens `doc_url` → inner agent uses existing read tools (`get_document_content` with range, `search_in_document` near offset, `read_cell_range`, …) to fetch **live** text.

### Host metadata schema

Locator columns in **`chunks`** — `writeragent_embeddings/<folder_corpus_key>/index.db` ([Corpus cache layout](#corpus-cache-layout), [Corpus storage](#corpus-storage)):

```text
(chunk_id, doc_url, doc_revision, embedding_model,
 para_index, char_start, char_end, content_hash,
 file_mtime, last_indexed_at)
```

Vectors live in **`vec_chunks`** vec0 (same DB). Fallback mode also fills **`chunks.embedding`** BLOB. Extend with Calc/Draw locator columns when those index paths ship; same “reference only” rule.

### Modes

1. **On-disk corpus (default)** — `index.db` with vec0 + chunks; scales to folder-sized corpora.
2. **In-memory subset (optional later)** — bounded “recent N” only; see [HNSW](#hnsw-and-hnsw-lite) in Future optimizations.

### Versioning

Re-index entire doc when `embedding_model` changes. For day-to-day edits, **`content_hash` per paragraph** drives incremental embed ([Incremental updates](#incremental-updates)); `doc_revision` / mtime catches files edited outside WriterAgent.

### Vendoring patterns (no LangChain dependency)

Reference implementations to adapt:

- **langchain_core.vectorstores.in_memory** — dump/load pattern; replace body with `index.db` vec0 layout.
- **langchain_community.vectorstores.sklearn** — `BinaryVectorSerializer` — useful for **NumPy fallback** only.
- **langchain_community.vectorstores.sqlitevec** — primary reference for vec0 integration.

**SQLite note:** Search runs in a **trusted venv module** — sqlite-vec `MATCH` by default; NumPy when [Search fallback](#search-fallback) is active. Host stdlib `sqlite3` may maintain `chunks` locators on the index worker thread ([Trusted extension code in the venv](enabling_numpy_in_libreoffice.md#trusted-extension-code-in-the-venv)).

---

## Indexing pipeline

**Build the minimal corpus index:**

1. **Discover** — document_research in folder → check per-folder cache; if missing, background scan of all siblings ([Background folder indexer](#background-folder-indexer)); else `list_nearby_files` scope for incremental work.
2. **Chunk in memory** — ~500-character windows with paragraph/offset tracking ([Chunking](#chunking)).
3. **Embed** — venv `sentence-transformers` batch (MVP) or cloud HTTP; normalize float32; **discard chunk text** after encode.
4. **Persist** — `vec_chunks` + `chunks` row + **`content_hash`** ([Corpus storage](#corpus-storage)); skip embed when hash unchanged ([Incremental updates](#incremental-updates)).
5. **Outer lookup** — `search_embeddings(query, k)` → locators → open **1–few** files → inner read at offset.

**Optional — active document only:** same pipeline for one `doc_url`; inject live-fetched text on main send (**secondary**).

---

## Incremental index maintenance {#incremental-updates}

The corpus index must stay **current without full re-embeds**. Grammar proofreading already solves a related problem: detect what changed, queue work, supersede stale jobs, write results to a cache — but on a **sentence** cadence with ~**1 s** quiet windows because users want squiggles immediately ([realtime-grammar-checker-plan.md](realtime-grammar-checker-plan.md), [`grammar_work_queue.py`](../plugin/writer/locale/grammar_work_queue.py)). **Embeddings are the opposite latency class:** stale-by-a-minute is acceptable; cost is **CPU batch encode** (local) or HTTP embed batches (cloud) for changed paragraphs only — not per-keystroke work.

### Paragraph hash (primary) vs sentence hash

Store a **content fingerprint per indexed unit** alongside each locator row:

| Granularity | Fingerprint key | Re-embed when | Notes |
|-------------|-------------------|---------------|-------|
| **Paragraph (default)** | `hash(normalized_para_text)` | Paragraph body changes | Matches Writer paragraph boundaries; fewer rows than sentences; aligns with chunker `\n\n` splits. |
| **Sentence (optional)** | `hash(normalized_sentence_text)` | Sentence changes | Finer invalidation inside long paragraphs; more index rows and API calls — use only if profiling shows paragraph grain is too coarse. |

**Schema addition:** `(para_index, content_hash)` — or `(para_index, sent_index, content_hash)` if sentence grain ships later. On index pass, compute hash from extracted text; **skip encode** when hash matches the stored row for that `(doc_url, para_index, embedding_model)`.

Normalized text for hashing should match what the chunker sees (tracked-deletion-free string where grammar uses [`get_string_without_tracked_deletions()`](../plugin/doc/document_helpers.py) — same stability goal as proofreader sentence keys).

### Always search; update in the background

**Lookup never blocks on re-embed.** `search_embeddings` reads the **current** `index.db` (vec0 or BLOB fallback + `chunks` locators):

- **Unchanged paragraphs** — existing vectors remain valid (hash match).
- **Changed paragraphs** — old vectors may still rank until the incremental worker replaces them; locators may drift slightly if paragraph boundaries moved — **re-resolve offset on open** via inner read tools (same as today when structure shifts).
- **New paragraphs** — no row yet → optional low-priority enqueue; search may miss until embedded.
- **Deleted paragraphs** — tombstone or delete locator rows on next maintenance pass.

This is intentional: **semantic find stays fast**; index converges asynchronously.

### Where edits are observed

**Primary — typing in Writer (`XProofreading`):** hook **`doProofreading`** alongside grammar ([XProofreading incremental hook](#xproofreading-incremental-hook)). Separate embeddings listener; **wait 60 s** after last change before batch re-embed. Not sentence-speed — find-doc can be ~1 min stale.

**Secondary — WriterAgent write tools:** after successful `apply_document_content` / Calc·Draw write tools, mark `(doc_url)` dirty (same debounced worker).

**Tertiary — folder / open path:** background folder indexer on document_research; hash diff on doc open; mtime sweep for files edited outside WriterAgent.

**Do not** duplicate UNO mutation listeners everywhere — the proofreading API already delivers paragraph-scale text on every edit when linguistic checking is active; embeddings can subscribe in parallel even when grammar LLM is disabled.

### Debounced worker (~1 minute, not grammar-speed)

Mirror grammar queue **patterns**, not timings. Embeddings **must not** run on every `doProofreading` call — only after **~60 s** with no further dirty marks for that `doc_url`:

| Aspect | Grammar proofreader | Embeddings index |
|--------|---------------------|------------------|
| **User expectation** | Errors visible within ~1 s | Find-doc can be ~1 min stale |
| **Quiet/coalesce window** | ~1 s batch drain (`GRAMMAR_WORKER_PAUSE_TIMEOUT_S`) | **~60 s** (configurable) per `(doc_url)` |
| **Work unit** | Sentence | Paragraph (default) |
| **Supersede** | `inflight_key` + `enqueue_seq` — newest wins | Same idea: `{doc_url}|{para_index}|{embedding_model}` |
| **API call** | Small grammar LLM per batch | **Local:** CPU batch encode in venv; **cloud:** HTTP embed batch for changed hashes only |

On dirty signal: bump `enqueue_seq` for affected paragraph keys; worker waits until the doc is **idle ~60 s**, drains the batch, re-extracts only paragraphs whose **hash ≠ stored hash**, calls `embed_texts` in batch, patches `vec_chunks` + `chunks` ([Corpus storage](#corpus-storage)).

Do **not** embed on every keystroke — that would duplicate grammar's stampede problem at encode cost (local CPU or cloud quota).

### Vector patch strategy

Apply patches with **in-place update semantics** — size tracks live corpus, not edit history ([Corpus storage](#corpus-storage)).

| Backend | Changed paragraph | New paragraph | Deleted paragraph |
|---------|-------------------|---------------|-------------------|
| **vec0 (default)** | `UPDATE vec_chunks …`; sync `chunks.content_hash` | `INSERT` into both tables | `DELETE` from both |
| **BLOB fallback** | `UPDATE chunks SET embedding=?, content_hash=?` | `INSERT` row with BLOB | `DELETE` row |

**Anti-pattern — do not use:** append-only vector logs or dual-file `vectors.bin` sidecars that grow on every edit.

Keep **locators** in sync when paragraph indices shift after large edits (re-walk paragraph list on full doc hash mismatch).

### Fleet / multi-writer note

If **all edits flow through WriterAgent**, each installation updates its local index for docs it modifies — no central server required. Two machines editing the same `file://` URL via sync (Nextcloud, etc.) rely on **revision / mtime + hash diff on open** to reconcile; last writer's embedding pass wins per paragraph hash. Document the conflict model; do not promise CRDT merge in v1.

### Phasing

- **Phase B:** `index.db` + vec0; background folder indexer; NumPy [Search fallback](#search-fallback); hash columns stored; periodic mtime refresh.
- **Phase C (live hooks):** not planned — superseded by periodic background indexer + mtime/hash diff (see [Phase C](#phase-c-incremental)).

---

## Chunking {#chunking}

Naive character splits destroy meaning. Vendor MIT **RecursiveCharacterTextSplitter** logic (~100 lines) — no langchain package.

- **Repository:** [langchain-text-splitters](https://github.com/langchain-ai/langchain/tree/master/libs/text-splitters/langchain_text_splitters)
- **Key file:** `recursive_character.py` — separators `["\n\n", "\n", " ", ""]`, `chunk_overlap` for context bridging.
- **Index-time only:** while splitting, record **paragraph index and char offsets** for each chunk so locators can be stored without keeping chunk text on disk.

---

## Corpus intelligence

The index is a **router**, not a library mirror.

### Outer agent: semantic find replaces grep

- **Before:** filename filter + `search_in_document` across many opens.
- **After:** one embedding query → ranked files + paragraph/offset → open winners → inner read.

Opening **one** file at a known locator is the designed happy path. Opening **many** files without the index is what we eliminate.

Pairs with [multi-document-dev-plan.md](multi-document-dev-plan.md): embeddings upgrade the **outer** tier; inner read tools unchanged.

(See also the discussion in the Problem section above on why semantic search has a larger relative benefit for office/document content than for code search, where literal identifiers and structure make pure lexical tools unusually strong.)

### Thematic clustering (future)

K-Means on document-level vectors to group files by topic without manual folders.

### Synthesis and gap analysis (research)

Compare document vectors to find "semantic delta" — what is in document A but missing from draft B.

---

## Future optimizations {#future-optimizations}

Try these **only when profiling on multi-file corpora** shows IPC NumPy search or encode latency is insufficient. MVP stays on warm-worker Pickle5 IPC ([Development plan](#development-plan)).

### Dedicated embeddings worker {#future-dedicated-worker}

See [Planned: dedicated embeddings subprocess](#dedicated-embeddings-subprocess) and [In-worker corpus cache (RAM)](#embeddings-in-worker-cache). Summary: second `PythonWorkerManager` (embeddings-only) + optional ~60 s TTL RAM cache of `(folder_key → corpus_matrix)` so Calc `=PYTHON()` never queues behind batch embed and repeat `search_embeddings` skips re-loading BLOBs from disk.

### Choosing a search backend {#choosing-a-search-backend}

| Scenario | Default today | Optional later |
|----------|---------------|----------------|
| Single doc / folder, hundreds–few k chunks | NumPy BLOB dot + top-k in venv | sqlite-vec `vec0` if installed |
| Large corpus, 5k+ chunks | NumPy BLOB (profile first) | vec0 or HNSW in venv (research) |
| No venv | HTTP embed + stdlib loop on host | Cython top-k |

| Approach | Role | Notes |
|----------|------|-------|
| **Venv + NumPy BLOB** | **Default** persist + search | Shipped bench path; sub-ms at N≈350 |
| **Venv + sqlite-vec** | Optional vec0 when installed | Faster at very large N — enable only after profiling |
| **Host Cython top-k** | Optional in-process search | [`writeragent_vec_search`](#cython-surface-area) |
| **Parallel FTS + embeddings** | **Don't** | Double cache |

### Host Cython `top_k_dot` {#cython-surface-area}

Mirror [`writeragent_vec`](../native/writeragent_vec/) — one hot function: scan row-major normalized float32 vectors, top-k by dot product. Layout: `native/writeragent_vec_search/` → `plugin/contrib/vec_search/`. Wire with `try: import writeragent_vec_search` and stdlib fallback.

### sqlite-vec in venv {#sqlite-vec-in-venv}

**Primary storage and search** — see [Corpus storage](#corpus-storage) and [Installing sqlite-vec](#installing-sqlite-vec). `sqlite-vec` indexes floats you already have — it does **not** embed text. User `pip install sqlite-vec` in the configured venv; do **not** vendor into OXT or LO process. See [sqlite-vec Python docs](https://alexgarcia.xyz/sqlite-vec/python.html).

### ONNX runtime

`onnxruntime` + exported ONNX weights can shrink dependencies vs full PyTorch for a **fixed** model. Defer — batched `sentence-transformers` is already fast enough ([Benchmark](#benchmark-on-your-machine)).

### HNSW and hnsw-lite {#hnsw-and-hnsw-lite}

Approximate nearest neighbor for bounded in-RAM subsets — not for full corpus streaming search on disk. PyPI: `hnsw-lite`. Rebuild from stored vectors on load; do not persist graphs by default.

### Advanced research

- Document-level vectors, K-Means clustering, semantic “gap analysis” between drafts
- Dedicated embeddings subprocess + TTL RAM corpus cache ([Dedicated embeddings subprocess](#dedicated-embeddings-subprocess), [In-worker corpus cache](#embeddings-in-worker-cache))
- Optional dedicated worker `action` for embed/search (see [Trusted extension code in the venv](enabling_numpy_in_libreoffice.md#trusted-extension-code-in-the-venv)) if stub overhead matters

---

## Related docs

| Topic | Doc |
|-------|-----|
| Cython build matrix | [cython-extension.md](cython-extension.md) |
| Venv / NumPy boundary | [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md) |
| Multi-file discovery | [multi-document-dev-plan.md](multi-document-dev-plan.md) |
| Chat memory / summarization | [langchain-plan.md](langchain-plan.md) |
| Realtime grammar / hash patterns | [realtime-grammar-checker-plan.md](realtime-grammar-checker-plan.md) |
| User profile memory | [agent-memory-and-skills.md](agent-memory-and-skills.md) |
