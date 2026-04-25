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

- **All settings (Doc tab)**: `doc.grammar_proofreader_*` in [`plugin/modules/doc/module.yaml`](../plugin/modules/doc/module.yaml) — enable (default **off**), debounce (ms), max characters, max response tokens, optional model (empty = same as chat `text_model`). The Doc tab also inlines Calc’s **Max Rows Display** (`calc.max_rows_display` via `config_inline: doc` in [`plugin/modules/calc/module.yaml`](../plugin/modules/calc/module.yaml)).
- **Diagnostics**: logger name `writeragent.grammar` — `INFO` lines prefixed `[grammar]` for each `doProofreading` call, cache hit/miss, worker skip/supersede, LLM request/result counts, and `WARNING` with stack trace on worker failure. Ensure `init_logging` has run (first grammar call attempts it); see `writeragent_debug.log` under the LO user config directory (see AGENTS.md).

### 2.3 Runtime behavior (summary)

- **`doProofreading`** is synchronous from LibreOffice’s perspective. To avoid UI freezes, on a **cache miss** it returns **empty errors immediately** and schedules an LLM call via [`run_in_background`](../plugin/framework/worker_pool.py) (`plugin.framework.worker_pool`). When results arrive, they are stored in the in-process cache; **the next** LO proofreading pass can return `SingleProofreadingError` rows (pull model — there is no push callback to force a redraw).
- **Debouncing** is applied **inside the background job** (sleep then check sequence number) so rapid LO calls do not spawn unbounded parallel requests.
- **LLM**: [`LlmClient.chat_completion_sync`](../plugin/modules/http/client.py) with a small system prompt requiring a single JSON object `{"errors":[{"wrong","correct","type","reason"},...]}`; user message is the **checked substring only** (not the whole document).
- **`TextMarkupType.PROOFREADING`**: resolved with `uno.getConstantByName("com.sun.star.text.TextMarkupType.PROOFREADING")` (avoids fragile `TextMarkupType` submodule imports for typecheckers).

### 2.4 Tests

- Unit: [`plugin/tests/test_grammar_proofread_engine.py`](../plugin/tests/test_grammar_proofread_engine.py).
- UNO (native runner): [`plugin/tests/uno/test_ai_grammar_proofreader.py`](../plugin/tests/uno/test_ai_grammar_proofreader.py) — cache path and `ignoreRule` filtering.

### 2.5 Risks (still relevant)

| Risk | Mitigation shipped / notes |
|------|----------------------------|
| Token cost / privacy | Master switch **off** by default; user must enable on Sidebar; Writer tab documents that checked text is sent to the configured endpoint. |
| UI freeze | No blocking HTTP in `doProofreading`; work on worker thread. |
| Stale underlines | Cache keyed by doc id + range + locale + **content hash**; text change → miss until a new analysis completes. |
| Concurrent chat agent | Separate `LlmClient` instance from sidebar; no explicit queueing — see **future work** (§5). |

---

## 3. Original sidebar vision (Track B) — unchanged intent, not built

The following remains a **valid product direction** but is **not** what Track A implements:

- Poll **current paragraph** (e.g. via `XTextViewCursor`), debounce on typing pauses.
- Post suggestions into the **chat sidebar** (overwrite/update a block, status line: typing / analyzing / N issues).
- Integration sketch that was considered: `realtime_checker.py`, `panel.py` / `SendButtonListener`, `queue_executor` for UNO reads on the main thread.

Reuse from Track A when implementing Track B: **JSON schema**, debounce **ideas**, and **`LlmClient`** — but the **integration surface** is chat UI, not `doProofreading`.

---

## 4. Optional reference: `GrammarChecker.py`

The standalone [`GrammarChecker.py`](../GrammarChecker.py) (root of repo) was used historically as a prompt/threading reference. It is **not** bundled as WriterAgent product code. Track A does **not** call it.

---

## 5. Future work (suggested backlog)

### Native grammar (Track A) — hardening and product

1. **429 / backoff**: exponential backoff and cooldown in the grammar worker; optionally skip scheduling when sidebar chat is mid-request (shared policy flag).
2. **Locales**: extend `LinguisticWriterAgentGrammar.xcu` and `getLocales()` / `hasLocale()` beyond English once validated.
3. **Refresh UX**: LO only shows new squiggles on **subsequent** proofreading passes; document for users; optional future hook if LO exposes a safe “invalidate proofreading” API worth researching.
4. **Rule IDs / Ignore**: persist ignored rules across sessions if product wants parity with Lightproof-style UX.
5. **Optional model / temperature**: surface more controls in Settings if needed (currently optional grammar model + shared endpoint).

### Sidebar assistant (Track B)

1. **`realtime_checker` module** + wiring in `panel_factory` / `panel` / `send_handlers` as originally sketched.
2. **Main-thread UNO** for paragraph reads; **worker** for LLM; clear **Stop** / lifecycle when panel closes.
3. **Anti-noise**: single updatable block in chat history; cap frequency.

### Docs / agents

- Keep [`AGENTS.md`](../AGENTS.md) in sync when behavior or config keys change (per project rules).

---

## 6. Revision history (high level)

- **Earlier draft**: Described only sidebar polling + chat append (Track B).
- **Current**: Track A **shipped** (Lightproof-style linguistic + LLM + cache); Track B **deferred**; this document updated to match reality and list follow-ups.
- **2026-04 debugging**: Locale list fixed to Lightproof-style `en-US en-GB`; lazy imports; [`ai_grammar_proofreader_stub.py`](../plugin/modules/writer/ai_grammar_proofreader_stub.py) added for manual bisect. Misleading crashes included LanguageTool/JVM and native stacks during Writing Aids.
- **2026-04 resolution**: LibreOffice calls `createInstanceWithArgumentsAndContext` with extra args; proofreaders must implement `__init__(self, ctx, *args)` (real + stub). With that fix, default `make manifest` bundles `LinguisticWriterAgentGrammar.xcu` again; temporary `WRITERAGENT_ENABLE_LINGUISTIC_GRAMMAR_XCU` / `WRITERAGENT_LINGUISTIC_GRAMMAR_STUB` manifest switches were removed.
