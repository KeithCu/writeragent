# Benchmark CLI Development Plan

**Goal**: Package WriterAgent's eval suite as a standalone CLI tool for AI developers to benchmark LLM performance on office automation tasks.

## Overview

The existing evaluation infrastructure in `scripts/prompt_optimization/` already supports:
- Multi-model benchmarking with cost tracking (intelligence-per-dollar)
- LLM-as-a-Judge scoring with weighted criteria (Accuracy, Formatting, Naturalness)
- Multiple backends: in-memory string simulation (`string`) and headless LibreOffice (`lo`)
- Tool loop evaluation matching production chat semantics
- Gold standard generation for reference answers

This document outlines the incremental plan to expose this as a standalone, easy-to-use CLI that AI developers can use without the full WriterAgent codebase.

---

## Required Features for Standalone CLI

### Phase 1: Core Benchmark (MVP)
| Feature | Status | Priority |
|---------|--------|----------|
| Run benchmarks against any OpenAI-compatible endpoint | **Exists** | High |
| Multi-model comparison with cost tracking | **Exists** | High |
| JSON/CSV output formats | **Exists** | High |
| Configurable system prompt | **Exists** | Medium |
| Tool loop evaluation (matching production) | **Exists** | High |
| LLM-as-a-Judge scoring | **Exists** | Medium |

### Phase 2: Usability Improvements
| Feature | Status | Priority |
|---------|--------|----------|
| Single-command install (`pip install writeragent-benchmark`) | **Missing** | High |
| Pre-configured dataset (Writer tasks) | **Exists** | High |
| Custom task support (user-provided prompts) | **Missing** | Medium |
| Progress reporting during long runs | **Partial** | Medium |
| Parallel model evaluation | **Exists** | Medium |

### Phase 3: Advanced Features
| Feature | Status | Priority |
|---------|--------|----------|
| Local model support (Ollama, LM Studio) | **Partial** | Medium |
| Gold standard generation CLI | **Exists** | Low |
| Token penalty configuration | **Exists** | Low |
| Category-specific scoring weights | **Exists** | Low |

---

## Incremental Development Plan

### Phase 1: Extract and Package (1-2 days)

**Goal**: Create a minimal standalone package that can run benchmarks.

#### Step 1: Create package structure
```
benchmark/
├── __init__.py          # Empty or minimal exports
├── __main__.py          # CLI entry point
├── core.py              # Extracted from eval_core.py (remove DSPy dependency)
├── dataset.py           # Copy existing dataset (Writer tasks)
├── models.py            # Model configs (copy from model_configs.py)
├── tools.py             # String-based tool implementations
├── client.py            # Minimal LLM client wrapper
└── cli.py               # Argument parsing and main
```

#### Step 2: Remove DSPy dependency
**Current**: `eval_core.py` uses `dspy.LM`, `dspy.Predict`, `dspy.Module`

**Action**: Replace with direct HTTP calls via `LlmClient` or a minimal wrapper:
- Extract `JudgeSignature` and `JudgeModule` to use direct LLM calls
- Replace `dspy.settings.context(lm=...)` with direct client instantiation
- Use existing `LlmClient` from `plugin/framework/client/llm_client.py`

**Files to modify**:
- `benchmark/core.py`: New file with extracted scoring logic, no DSPy
- `benchmark/client.py`: Wrapper around LlmClient for judge calls

#### Step 3: Create CLI entry point
```python
# benchmark/__main__.py
import argparse
from benchmark.cli import main

if __name__ == "__main__":
    main()
```

**CLI interface** (`benchmark/cli.py`):
```python
parser = argparse.ArgumentParser(description="WriterAgent Benchmark CLI")
parser.add_argument("--endpoint", default="https://openrouter.ai/api/v1")
parser.add_argument("--api-key", help="API key for endpoint")
parser.add_argument("--models", default="openai/gpt-oss-120b,google/gemini-3-flash-preview")
parser.add_argument("--output", "-o", default="benchmark_results.json")
parser.add_argument("--format", choices=["json", "csv"], default="json")
parser.add_argument("--backend", choices=["string", "lo"], default="string")
parser.add_argument("--examples", "-n", type=int, default=None)
parser.add_argument("--parallel", "-j", type=int, default=4)
parser.add_argument("--verbose", "-v", action="store_true")
```

#### Step 4: Create setup.py/pyproject.toml
```toml
# pyproject.toml for benchmark package
[project]
name = "writeragent-benchmark"
version = "0.1.0"
dependencies = [
    "requests>=2.28",
    "pydantic>=2.0",
]

[project.scripts]
writeragent-benchmark = "benchmark.cli:main"
```

### Phase 2: Usability (1 day)

#### Step 5: Add custom task support
```python
# Allow users to provide their own tasks
parser.add_argument("--task", action="append", help="Custom task: 'prompt|document|category'")
```

**Format**: `"Convert this to a table|Item: Apple, Banana\nPrice: 1, 2|structural"`

#### Step 6: Progress reporting
- Use `tqdm` for progress bars during multi-model runs
- Show estimated time remaining
- Display per-model results as they complete

#### Step 7: Configuration file support
```python
# benchmark.yaml
endpoint: https://openrouter.ai/api/v1
api_key: ${OPENROUTER_API_KEY}
models:
  - openai/gpt-oss-120b
  - google/gemini-3-flash-preview
tasks:
  - table_from_mess
  - reformat_resume
backend: string
parallel: 4
```

### Phase 3: Polish (1 day)

#### Step 8: Documentation
- README.md with usage examples
- Examples of interpreting results
- Guide to adding custom models

#### Step 9: Local model support
```python
# Auto-detect Ollama/LM Studio endpoints
parser.add_argument("--local", action="store_true", help="Use local Ollama")
# When --local, default to http://localhost:11434
```

#### Step 10: Pre-built binaries
- Publish to PyPI: `pip install writeragent-benchmark`
- Provide Docker image for reproducible runs

---

## Code Changes Required

### 1. Extract `eval_core.py` → `benchmark/core.py`

**Remove DSPy dependencies**:
- Replace `dspy.LM` with custom `LlmClient` wrapper
- Replace `dspy.Predict` with direct function calls
- Replace `dspy.Module` with plain Python classes

**Keep**:
- `ExampleEval` dataclass
- `score_with_judge` function (modified to use LlmClient)
- `_correctness_breakdown` function
- `_get_tokens_from_pred` function
- `run_eval_on_examples_llm` function
- `summarize_results` function

### 2. Create `benchmark/client.py`

```python
"""Minimal LLM client for benchmarking."""
from typing import Any
import requests
import json

class BenchmarkClient:
    """Lightweight wrapper for OpenAI-compatible API calls."""
    
    def __init__(self, endpoint: str, api_key: str, model: str):
        self.endpoint = endpoint.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.model = model
    
    def chat(self, messages: list[dict[str, Any]], max_tokens: int = 8192) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        response = requests.post(
            f"{self.endpoint}/chat/completions",
            headers=self.headers,
            json=payload,
        )
        response.raise_for_status()
        return response.json()
    
    def get_usage(self, response: dict[str, Any]) -> dict[str, int]:
        """Extract token usage from API response."""
        usage = response.get("usage", {})
        return {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
```

### 3. Create `benchmark/tools.py`

Copy and simplify `string_eval_tools.py`:
- `StringDocState` class
- `DrawDocState` class  
- `CalcStringState` class
- `dispatch_string_tool` function

Remove dependency on `plugin.framework.*` by inlining needed utilities.

### 4. Create `benchmark/dataset.py`

Copy existing dataset from `scripts/prompt_optimization/dataset.py`:
- All example tasks (table_from_mess, reformat_resume, etc.)
- `ALL_EXAMPLES` list
- `to_dspy_examples` → rename to `to_benchmark_examples`

### 5. Create `benchmark/cli.py`

Main CLI logic:
```python
import argparse
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from benchmark.core import run_eval_on_examples_llm, summarize_results
from benchmark.dataset import ALL_EXAMPLES
from benchmark.models import MODEL_BY_ID, get_default_models
from benchmark.client import BenchmarkClient


def run_benchmark(args):
    """Run benchmark for configured models."""
    model_ids = args.models.split(",") if args.models else get_default_models()
    
    # Filter examples if requested
    examples = ALL_EXAMPLES
    if args.examples:
        examples = examples[:args.examples]
    
    results = []
    for model_id in model_ids:
        client = BenchmarkClient(args.endpoint, args.api_key, model_id)
        model_results = run_eval_on_examples_llm(
            examples=examples,
            endpoint=args.endpoint,
            api_key=args.api_key,
            model=model_id,
            backend=args.backend,
            verbose=args.verbose,
        )
        results.append({
            "model": model_id,
            "summary": summarize_results(model_results),
            "details": [r.__dict__ for r in model_results],
        })
    
    # Write output
    output = results
    if args.format == "csv":
        # Convert to CSV format
        pass  # Implement CSV conversion
    
    Path(args.output).write_text(json.dumps(output, indent=2))
    print(f"Results written to {args.output}")


def main():
    parser = argparse.ArgumentParser(description="WriterAgent Benchmark CLI")
    # ... add arguments ...
    args = parser.parse_args()
    run_benchmark(args)


if __name__ == "__main__":
    main()
```

---

## File Structure After Implementation

```
writeragent/
├── docs/
│   └── benchmark-cli-dev-plan.md    # This document
├── scripts/
│   └── prompt_optimization/          # Existing (unchanged)
└── benchmark/                        # NEW: Standalone package
    ├── __init__.py
    ├── __main__.py
    ├── cli.py
    ├── core.py
    ├── client.py
    ├── dataset.py
    ├── models.py
    ├── tools.py
    └── pyproject.toml
```

---

## Testing Strategy

### Unit Tests
1. Test scoring logic independently
2. Test tool execution with mock responses
3. Test token accounting

### Integration Tests
1. Run full benchmark against mock API endpoint
2. Verify JSON/CSV output formats
3. Test parallel execution

### End-to-End Tests
1. Run against real OpenRouter endpoint with test API key
2. Verify results match existing eval suite

---

## Success Criteria

- [ ] `python -m benchmark --help` works
- [ ] `python -m benchmark --models openai/gpt-oss-120b --api-key XXX` runs successfully
- [ ] Results match existing `run_eval_multi.py` output for same model
- [ ] Package installs via `pip install -e .` from benchmark directory
- [ ] Documentation covers basic usage

---

## Dependencies

**Required** (new):
- `requests` - HTTP client
- `pydantic` - Data validation (optional but recommended)

**Existing** (reused):
- No DSPy required for standalone version
- `plugin/framework/client/llm_client.py` can be reused or minimal wrapper created

**Optional** (for local models):
- `httpx` - Async support for better performance
- `tqdm` - Progress bars

---

## Timeline

| Phase | Duration | Deliverables |
|-------|----------|--------------|
| Phase 1: Extract & Package | 1-2 days | Working CLI, basic benchmark |
| Phase 2: Usability | 1 day | Custom tasks, progress reporting, config file |
| Phase 3: Polish | 1 day | Documentation, PyPI package, Docker image |
| **Total** | **3-4 days** | Full standalone benchmark CLI |

---

## Quick Start (After Implementation)

```bash
# Install
cd writeragent/benchmark
pip install -e .

# Run benchmark
writeragent-benchmark \
  --endpoint https://openrouter.ai/api/v1 \
  --api-key $OPENROUTER_API_KEY \
  --models openai/gpt-oss-120b,google/gemini-3-flash-preview \
  --output results.json

# Run with custom task
writeragent-benchmark \
  --endpoint https://openrouter.ai/api/v1 \
  --api-key $OPENROUTER_API_KEY \
  --task "Create a table|Name: Alice, Bob\nAge: 30, 25|structural" \
  --output custom_results.json

# Run with local Ollama
writeragent-benchmark \
  --endpoint http://localhost:11434 \
  --api-key dummy \
  --models llama3.2 \
  --local \
  --output ollama_results.json
```

---

## Notes

1. **No separate repo needed**: Package lives in `writeragent/benchmark/`
2. **Reuse existing code**: Extract from `scripts/prompt_optimization/`, don't rewrite
3. **Minimal dependencies**: Avoid DSPy for standalone to reduce friction
4. **Progressive enhancement**: Start simple, add features incrementally
5. **Backward compatibility**: Existing `run_eval_multi.py` continues to work
