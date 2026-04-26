# Real-time / AI grammar checking — plan and status

**Status**: Track A shipped — UNO proofreader + engine + Linguistic `GrammarCheckers` XCU are bundled in default builds (`make manifest` / `make build`). Sidebar “living assistant” path (Track B) not built.  
**Authors**: WriterAgent Team  
**Audience**: Developers and PMs aligning on two different surfaces: **Writer linguistic grammar** vs **sidebar chat**.

---

## 1. Two different features (do not conflate)

| Track | UX surface | Status |
|--------|------------|--------|
| **A. Native Writer grammar (Linguistic2)** | Same as other grammar extensions: Writer’s grammar pass, underlines, grammar dialog. Uses `XProofreader` + `Linguistic` / `GrammarCheckers` registry. | **Shipped / experimental** — Python `XProofreader` + Lightproof-style XCU are in the default OXT; users enable LLM work on the Doc tab and pick the active proofreader under Writing aids. Earlier native crashes were fixed by accepting extra UNO constructor args (`__init__(self, ctx, *args)`). |
| **B. Sidebar “living assistant”** | Poll current paragraph, debounce, append/update a block in the chat panel with suggestions. | **Not implemented**; original §3–§6 intent below remains **future work** (see §5). |

Track **A** follows the [lightproof/](../lightproof/) pattern (Python UNO `XProofreader` + `Linguistic.xcu` fuse). It is **not** the same as appending text to the chat sidebar.

---

## 2. What we actually shipped (Track A)

### 2.1 Code and packaging

- **UNO component**: [`plugin/modules/writer/ai_grammar_proofreader.py`](../plugin/modules/writer/ai_grammar_proofreader.py) — `WriterAgentAiGrammarProofreader` (`unohelper` + `XProofreader`, locales, service info). Standalone entrypoint: extends `sys.path` like [`plugin/modules/chatbot/panel_factory.py`](../plugin/modules/chatbot/panel_factory.py) so `import plugin.*` works when LO loads the module.
- **Engine (testable)**: [`plugin/modules/writer/grammar_proofread_engine.py`](../plugin/modules/writer/grammar_proofread_engine.py) — JSON parsing (`safe_json_loads`), offset normalization, in-memory cache, ignore-rule set.
- **Registry**: [`extension/registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu`](../extension/registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu) — fuses `org.extension.writeragent.comp.pyuno.AiGrammarProofreader` under `GrammarCheckers` with `Locales` **`en-US en-GB`** (one `oor:string-list` `<value>`, matching Lightproof). Must stay aligned with `getLocales()` (UNO `Locale` for en-US / en-GB) and [`GRAMMAR_REGISTRY_LOCALE_TAGS`](../plugin/modules/writer/grammar_proofread_engine.py) (unit test enforces parity).
- **Bundle**: [`scripts/manifest_registry.py`](../scripts/manifest_registry.py) — `META-INF/manifest.xml` always lists the Python UNO module and `registry/org/openoffice/Office/LinguisticWriterAgentGrammar.xcu` in default `make manifest` / `make build` output.
- **Stub (optional)**: [`plugin/modules/writer/ai_grammar_proofreader_stub.py`](../plugin/modules/writer/ai_grammar_proofreader_stub.py) is kept in-tree for manual debugging (swap manifest entry by hand if needed); it is not selected by the generator.

### 2.2 Configuration

- **All settings (Doc tab)**: `doc.grammar_proofreader_*` in [`plugin/modules/doc/module.yaml`](../plugin/modules/doc/module.yaml) — enable (default **off**), debounce (ms), wait timeout (ms), optional model (empty = same as chat `text_model`), and `doc.grammar_proofreader_pause_during_agent` (default **off**) to pause grammar API requests while sidebar chat/agent work is active. Proofread span length (500 chars) and LLM max output tokens (512) are **fixed in code** in [`ai_grammar_proofreader.py`](../plugin/modules/writer/ai_grammar_proofreader.py). The Doc tab also inlines Calc’s **Max Rows Display** (`calc.max_rows_display` via `config_inline: doc` in [`plugin/modules/calc/module.yaml`](../plugin/modules/calc/module.yaml)).
- **Diagnostics**: logger name `writeragent.grammar` — `INFO` lines prefixed `[grammar]` for each `doProofreading` call, cache hit/miss, worker skip/supersede, LLM request/result counts, and `WARNING` with stack trace on worker failure. Ensure `init_logging` has run (first grammar call attempts it); see `writeragent_debug.log` under the LO user config directory (see AGENTS.md).

### 2.3 Runtime behavior (summary)

- **`doProofreading`** is synchronous from LibreOffice’s perspective. On a **cache miss**, it starts or joins one in-flight LLM worker for the **full cache key** (doc, locale, slice fingerprint, and sentence bounds), then waits up to `doc.grammar_proofreader_wait_timeout_ms` while pumping LibreOffice events with `processEventsToIdle()`. If the worker finishes in time, the same proofreading call returns `SingleProofreadingError` rows. If the timeout is reached, it returns empty errors and the worker continues caching results for a later LO proofreading pass.
- **Sidebar status**: the proofreader emits `grammar:status` events (`start`, `join`, `request`, `complete`, `timeout`, `skipped`, `failed`) with a three-word preview, checked length, result count/status, and elapsed milliseconds when available. The chat sidebar listens and posts these to the status field unless a chat send/approval is active.
- **Debouncing** is applied **inside the background job** (sleep then check sequence number) so rapid LO calls do not spawn unbounded parallel requests.
- **LLM**: [`LlmClient.chat_completion_sync`](../plugin/modules/http/client.py) with `response_format={"type":"json_object"}` on the OpenAI-compatible path (Together, OpenRouter, etc.; see docstring on `make_chat_request`), a small system prompt requiring a single JSON object `{"errors":[{"wrong","correct","type","reason"},...]}`, and user message the **batched checked slice** `aText[n_start:n_end]` (see §3.1; capped at 500 characters in code). Parser: [`parse_grammar_json`](../plugin/modules/writer/grammar_proofread_engine.py) uses `safe_json_loads` then `json_repair` when needed.
- **`TextMarkupType.PROOFREADING`**: resolved with `uno.getConstantByName("com.sun.star.text.TextMarkupType.PROOFREADING")` (avoids fragile `TextMarkupType` submodule imports for typecheckers).

### 2.4 Tests

- Unit: [`plugin/tests/test_grammar_proofread_engine.py`](../plugin/tests/test_grammar_proofread_engine.py).
- UNO (native runner): [`plugin/tests/uno/test_ai_grammar_proofreader.py`](../plugin/tests/uno/test_ai_grammar_proofreader.py) — cache path and `ignoreRule` filtering.

### 2.5 Risks (still relevant)

| Risk | Mitigation shipped / notes |
|------|----------------------------|
| Token cost / privacy | Master switch **off** by default; user must enable on Sidebar; Writer tab documents that checked text is sent to the configured endpoint. |
| UI freeze | HTTP still runs on a worker thread. `doProofreading` may wait briefly for results, but it pumps LibreOffice events while waiting and falls back to the asynchronous cache path on timeout. |
| Stale underlines | In-process cache: LRU (max **128** entries) keyed by **doc id + locale + SHA256(slice) + `(n_start,n_end)`** so identical substring text at different positions never shares cached absolute offsets. **Cache hit** → no LLM; **miss** → wait-with-event-pump for the in-flight worker (one job per full key), then fall back to cached-later behavior on timeout. Re-proofing the **same** slice at the **same** bounds and text avoids extra API calls until eviction or edits change the slice. See §6 for evolving this. |
| Concurrent chat agent | Optional guard (`doc.grammar_proofreader_pause_during_agent`) can skip grammar worker calls while chat/agent sends are active; grammar and chat/agent LLM requests also share one in-process request lane to avoid overlap races. |

---

## 3. Lightproof-inspired optimizations (Track A)

As of **2026-04-25**, the native grammar checker implements two key optimizations inspired by the `lightproof` project to handle long documents efficiently:

1.  **Paragraph-level batching (Lightproof-aligned)**: On the `nStart == 0` pass, the proofread window is **`aText[0:min(len(aText), 500)]`** (hardcoded in [`ai_grammar_proofreader.py`](../plugin/modules/writer/ai_grammar_proofreader.py)) with `ProofreadingResult` positions finalized via `_finalize_proofreading_sentence_positions` (same idea as [`lightproof/Lightproof.py`](../lightproof/Lightproof.py) after the LO 4 patch). This avoids LLM/cache work on each one-character extension of the sentence.
2.  **Slice fingerprinting + bounds in key**: Cache lookup uses `doc_id` + `locale_key` + **SHA256 of the substring** (`fingerprint_for_text`) **and** Writer’s `(n_start, n_end)` for that pass (`make_cache_key` in [`grammar_proofread_engine.py`](../plugin/modules/writer/grammar_proofread_engine.py)). That detects “same bytes at the same span” for hits without calling the LLM, and avoids wrong underlines when the **same** characters appear elsewhere in the document. Cached `SingleProofreadingError` positions are absolute in the **current** proofread buffer; if the model of truth drifts (e.g. edits shift indices but LO reuses the same bounds), treat as a **future correctness** topic (§6).

---

## 4. Original sidebar vision (Track B) — unchanged intent, not built

The following remains a **valid product direction** but is **not** what Track A implements:

- Poll **current paragraph** (e.g. via `XTextViewCursor`), debounce on typing pauses.
- Post suggestions into the **chat sidebar** (overwrite/update a block, status line: typing / analyzing / N issues).
- Integration sketch that was considered: `realtime_checker.py`, `panel.py` / `SendButtonListener`, `queue_executor` for UNO reads on the main thread.

Reuse from Track A when implementing Track B: **JSON schema**, debounce **ideas**, and **`LlmClient`** — but the **integration surface** is chat UI, not `doProofreading`.

---

## 5. Optional reference: `GrammarChecker.py`

The standalone [`GrammarChecker.py`](../GrammarChecker.py) (root of repo) was used historically as a prompt/threading reference. It is **not** bundled as WriterAgent product code. Track A does **not** call it.

---

## 6. Future work (suggested backlog)

### Native grammar (Track A) — hardening and product

1.  **Persistent Cache (SQLite)**: Move the `_proofread_cache` from memory to the database (reusing `history_db.py`). This allows grammar underlines to appear instantly when re-opening a long document.
2.  **Native Linguistic Integration**: Research using the built-in `SpellChecker` with specialized XML queries (e.g., `<query type='analyze'>`) to perform morphological analysis (stems, part-of-speech) locally before or during LLM work (inspired by `lightproof` morphology caching).
3.  **429 / backoff**: exponential backoff and cooldown in the grammar worker; optionally skip scheduling when sidebar chat is mid-request (shared policy flag).
4.  **Locales**: extend `LinguisticWriterAgentGrammar.xcu` and `getLocales()` / `hasLocale()` beyond English once validated.
5.  **Refresh UX**: LO only shows new squiggles on **subsequent** proofreading passes; document for users; optional future hook if LO exposes a safe “invalidate proofreading” API worth researching.
6.  **Optional model / temperature**: surface more controls in Settings if needed (currently optional grammar model + shared endpoint).
7.  **Multi-span / LRU cache**: **Shipped (baseline):** LRU (128) keyed by `doc_id`, locale, slice fingerprint, and `(n_start, n_end)`. Further ideas: larger cap, TTL, or persistent disk cache (see item 1).
8.  **Document-generation invalidation**: If LO exposes a revision counter, generation id, or “document modified” tick, fold it into the cache key or force miss when the full buffer changes even if a slice string matches (reduces risk of stale absolute offsets after edits above the span).
9.  **Persistent + bounded disk cache**: Extend item (1): cap entries by size/TTL; optional opt-out for privacy; encrypt-at-rest if storing text snippets on disk.
10. **Shared policy with chat**: Baseline shipped: optional pause-during-agent setting + shared in-process LLM request lane. Future expansion: endpoint-aware policy (per provider/model), richer status UX, and adaptive queue/backoff.
11. **Smaller / faster grammar model**: Route grammar-only traffic to a cheaper or local model by default; keep “same as chat” as an override (already partially supported via `grammar_proofreader_model`).
12. **Prompt and schema hardening**: Few-shot examples for edge cases (quotes, lists, track changes); strict JSON recovery; optional `response_format` where the API supports it.
13. **Paragraph batching tuning**: If Writer scheduling or underlines misbehave on some LO versions, compare our capped batch end vs stock Lightproof’s `len(rText)` and adjust `_finalize_proofreading_sentence_positions` only.
14. **Ignore rules & parity**: Persist `ignoreRule` across sessions; locale-specific ignores if the API evolves.
15. **Observability**: Debug metrics (cache hit rate, worker supersede count, p50/p95 latency from schedule → `cache_put`) behind a verbose flag for field debugging.
16. **Accessibility / UX copy**: Clear user-facing text that grammar is **asynchronous** (squiggles after pause); link to Writing aids selection when multiple proofreaders exist.

### Sidebar assistant (Track B)

1.  **`realtime_checker` module** + wiring in `panel_factory` / `panel` / `send_handlers` as originally sketched.
2.  **Main-thread UNO** for paragraph reads; **worker** for LLM; clear **Stop** / lifecycle when panel closes.
3.  **Anti-noise**: single updatable block in chat history; cap frequency.

### Docs / agents

- Keep [`AGENTS.md`](../AGENTS.md) in sync when behavior or config keys change (per project rules).

---

## 7. Revision history (high level)

- **Earlier draft**: Described only sidebar polling + chat append (Track B).
- **2026-04 (Late)**: Paragraph-level batching was attempted then reverted; cache uses **slice fingerprints** (Lightproof-adjacent ideas, see §3).
- **2026-04-26**: Lightproof-style **capped** batching re-enabled on `nStart==0` (`min(len(aText), 500)` + `_finalize_proofreading_sentence_positions`).
- **2026-04 (Mid)**: Track A **shipped** (Lightproof-style linguistic + LLM + cache); Track B **deferred**; this document updated to match reality and list follow-ups.
- **2026-04 debugging**: Locale list fixed to Lightproof-style `en-US en-GB`; lazy imports; stub added.
- **2026-04 resolution**: LibreOffice calls `createInstanceWithArgumentsAndContext` with extra args; proofreaders must implement `__init__(self, ctx, *args)`.
- **2026-04 (doc)**: Expanded §6 backlog (cache evolution, cost control, LO integration); clarified §2.5 / §3 cache behavior vs. one-slot-per-doc limitation.
