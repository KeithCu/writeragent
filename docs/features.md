# WriterAgent — Feature docs index

Product overview lives in the root [README](../README.md). This page maps each area to deeper topic docs.

**Getting started:** [Install and usage](install-troubleshooting.md) (menus, Settings, Harper grammar, `writeragent_debug.log`). Full `writeragent.json` keys: [writeragent-config-schema.md](writeragent-config-schema.md).

## Writer

| Topic | Docs |
|-------|------|
| Specialized toolsets | [writer/specialized-toolsets.md](writer/specialized-toolsets.md) |
| Page layout | [writer/page-api-reference.md](writer/page-api-reference.md) |
| Shapes | [draw/shape-support.md](draw/shape-support.md) |
| Bookmarks | [writer/bookmarks-api-reference.md](writer/bookmarks-api-reference.md) |
| Footnotes | [writer/footnotes-api-reference.md](writer/footnotes-api-reference.md) |
| Track changes | [writer/tracking-api-reference.md](writer/tracking-api-reference.md) |
| Grammar pipeline | [writer/grammar-checker-plan.md](writer/grammar-checker-plan.md) |
| Math / TeX | [writer/math-tex.md](writer/math-tex.md) |
| Jupyter `.ipynb` import (File → Open; WriterAgent and LibrePy) | [writer/jupyter-notebook-import.md](writer/jupyter-notebook-import.md) |
| Styles / LLM HTML | [writer/llm-styles.md](writer/llm-styles.md) · [LLM_STYLES.md](../LLM_STYLES.md) |
| Reviewable edits | [writer/reviewable-agent-edits.md](writer/reviewable-agent-edits.md) |
| Rich-text sidebar | [chat/rich-text-control-sidebar.md](chat/rich-text-control-sidebar.md) |
| Chat sidebar | [chat/sidebar-implementation.md](chat/sidebar-implementation.md) · slash `/` popup (commands mostly stubs): [chat/slash-commands.md](chat/slash-commands.md) |

## Calc

| Topic | Docs |
|-------|------|
| NumPy / `=PY()` | [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md) |
| LibrePy (Python-only OXT) | [scripting/librepy-split.md](scripting/librepy-split.md) · [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md) |
| Data shapes | [calc/py-data-shapes.md](calc/py-data-shapes.md) |
| Domain helpers (Viz, Math, Quant, …) | [scripting/numpy-domains.md](scripting/numpy-domains.md) |
| Analysis tools | [calc/analysis-tools.md](calc/analysis-tools.md) · [calc/analysis-sub-agent.md](calc/analysis-sub-agent.md) |
| Specialized toolsets | [calc/specialized-toolsets.md](calc/specialized-toolsets.md) |
| Sheet → Python | [calc/spreadsheet-to-python-import.md](calc/spreadsheet-to-python-import.md) (prototype, low priority) |
| Conditional formatting | [calc/conditional-formatting.md](calc/conditional-formatting.md) |
| Sheet filters | [calc/sheet-filter.md](calc/sheet-filter.md) |
| Serialization | [scripting/numpy-serialization.md](scripting/numpy-serialization.md) |

### Analysis helpers

| Helper | Purpose |
|--------|---------|
| `describe_data` | Extended EDA + column quality |
| `kpi_summary` | Aggregate mean/min/max/sum |
| `detect_outliers` | IQR, z-score, or isolation forest |
| `quick_stats` | Compact metric card |
| `format_currency` / `format_percent` | Display formatters |
| `clean_and_prepare` | Dedupe, simple imputation |
| `pivot_aggregate` | Pivot table wrapper |
| `group_summary` | Group-by aggregates |
| `compare_periods` | YoY/QoQ/MoM |
| `correlation_matrix` | Top correlated pairs |
| `run_regression` | OLS via statsmodels |
| `cluster_numeric` | KMeans centroids |
| `monte_carlo` | Monte Carlo resampling |
| `calc_goal_seek` | Single-variable what-if (native Calc) |
| `calc_solver` | Constrained optimization on formulas (native Calc) |

Contracts and RPC: [calc/analysis-tools.md](calc/analysis-tools.md).

## Multi-modal

| Topic | Docs |
|-------|------|
| Web research | [chat/search.md](chat/search.md) · [chat/search-engine-integration.md](chat/search-engine-integration.md) |
| Image generation | [images/generation.md](images/generation.md) |
| Vision / OCR | [images/recognition.md](images/recognition.md) |
| Audio | [chat/audio-architecture.md](chat/audio-architecture.md) |

## Cross-document & intelligence

| Topic | Docs |
|-------|------|
| LO-DOM | [writer/lo-dom-semantic-tree.md](writer/lo-dom-semantic-tree.md) |
| Embeddings / FTS | [embeddings.md](embeddings.md) |
| Multi-document | [chat/multi-document-dev-plan.md](chat/multi-document-dev-plan.md) |
| Memory | [hermes-agent-patterns.md](hermes-agent-patterns.md) |
| Librarian | [chat/librarian-onboarding.md](chat/librarian-onboarding.md) |
| Localization | [localization.md](localization.md) |

## Draw / Impress

| Topic | Docs |
|-------|------|
| Specialized toolsets | [draw/impress-specialized-toolsets.md](draw/impress-specialized-toolsets.md) |
| Shapes | [draw/shape-support.md](draw/shape-support.md) |
| PPT-Master | [ppt-master-integration-plan.md](ppt-master-integration-plan.md) |

## MCP & integrations

| Topic | Docs |
|-------|------|
| MCP protocol | [mcp-protocol.md](mcp-protocol.md) |
| Cursor plugin | [cursor-libreoffice](https://github.com/KeithCu/cursor-libreoffice) |
| LibreOffice skill | [libreoffice-skill](https://github.com/KeithCu/libreoffice-skill) |
| Config examples | [CONFIG_EXAMPLES.md](../CONFIG_EXAMPLES.md) |

## Engineering

| Topic | Docs |
|-------|------|
| Architecture | [writeragent-architecture.md](writeragent-architecture.md) |
| Streaming / threading | [framework/streaming-and-threading.md](framework/streaming-and-threading.md) |
| Formal verification | [framework/formal-verification.md](framework/formal-verification.md) |
| Test architecture | [test_architecture_analysis.md](test_architecture_analysis.md) |
| LLM hacks | [chat/llm-hacks.md](chat/llm-hacks.md) |
| Benchmarks | [eval/benchmarks.md](eval/benchmarks.md) · [scripts/prompt_optimization/](../scripts/prompt_optimization/README.md) |
| Type checking | [framework/type-checking.md](framework/type-checking.md) |
