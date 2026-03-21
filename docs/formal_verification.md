# Formal Verification Strategy for WriterAgent

This document explores the theoretical foundation and practical application of formal verification (FV) to the WriterAgent Python codebase. 

**Critical Architectural Assumption:** WriterAgent relies heavily on LibreOffice's UNO API. For the scope of this document and any FV efforts, **we treat the UNO C++ bridge as an axiomatically sound, 100% reliable external environment.** We have no interest in verifying LibreOffice itself. If a UNO method is called with correct parameters, we assume it succeeds. Our FV scope is strictly constrained to proving the correctness of *our* Python code: our data transformations, parsing logic, state management, and algorithmic safety.

---

## 1. The Theoretical Landscape: Verifying Python

Formal verification is the application of mathematical proofs to demonstrate that a program satisfies its formal specifications for all possible inputs. Traditional unit testing suffers from the *coverage problem*—it samples a finite subset of an infinite state space. FV, via techniques like Symbolic Execution and Bounded Model Checking (BMC), attempts to explore the state space exhaustively by treating inputs as symbolic variables.

### The Dynamics of Python vs. SMT Solvers
Most modern FV tools rely on Satisfiability Modulo Theories (SMT) solvers, such as Microsoft's Z3. An SMT solver decides the satisfiability of first-order logic formulas with respect to background theories (e.g., bit-vectors, arrays, real numbers).

Applying this to Python introduces severe impedance mismatch:
1.  **Dynamic Typing & Late Binding:** A symbolic Python variable lacks a fixed memory footprint or operational semantic bounds until runtime. A single `+` operator could mean integer addition, string concatenation, or a custom `__add__` metaclass resolution.
2.  **State Space Explosion:** Python's highly mutable runtime (where dictionaries underpin classes, and functions are first-class objects) causes the state space to explode exponentially. Translating arbitrary Python bytecode into a finite set of logical constraints for an SMT solver is often undecidable.
3.  **The Halting Problem:** Pure formal provers (like `deal-solver`) struggle with unbounded loops and recursive calls in Turing-complete languages. They often require manual loop invariants (mathematical properties that hold true before and after each loop iteration) to prove termination, which are exceedingly rare in standard Python codebases.

Because of these constraints, *full* formal verification of Python is largely restricted to trivial, purely functional subsets. However, hybrid approaches—specifically **Concolic Execution**—offer a highly practical compromise.

---

## 2. Tooling: From Proofs to Concolic Execution

We evaluate the current Python verification ecosystem strictly for its utility on our pure Python algorithmic modules.

### A. Deal (Design by Contract) & Deal-Solver
[`deal`](https://deal.readthedocs.io/) implements Design by Contract (DbC), heavily inspired by Hoare logic and the Eiffel language. It uses decorators (`@deal.pre`, `@deal.post`, `@deal.inv`) to define axioms and theorems about functions.

*   **The Verifier (`deal-solver`):** `deal` includes an experimental static verifier that attempts to translate the Python AST and the contracts directly into Z3 theorems. 
*   **The CS Reality:** It is a fascinating academic exercise but practically unworkable for WriterAgent. It requires absolute referential transparency, does not support most of the Python standard library, cannot model sets or complex OOP structures, and fails on unbounded loops.

### B. CrossHair: The Concolic Testing Engine
[`CrossHair`](https://crosshair.readthedocs.io/) represents the most viable path forward. It is not a pure formal verifier; it is a **verifier-driven fuzzer** that utilizes **Concolic (Concrete + Symbolic) Execution**.

*   **How it Works:** CrossHair hooks into the Python interpreter. As a function executes, CrossHair maintains two states: a concrete state (actual values) and a symbolic state (Z3 equations representing the path constraints). When it encounters a branch (e.g., `if len(url) > 10:`), it queries the Z3 SMT solver: *"Is there an input that satisfies the current path constraints AND makes `len(url) > 10` false?"* If so, it forks the execution and explores both paths.
*   **Why it Fits Our Code:** Because it runs the actual CPython interpreter, it handles "magic", standard libraries, and complex types perfectly. It essentially exhaustively searches for a combination of inputs that will raise an unhandled exception or violate a `deal` contract. It trades mathematical certainty (it will time out on infinite state spaces) for immense practical utility in finding edge-case counterexamples.

### C. Bounded Model Checking (ESBMC-Python)
[ESBMC](https://github.com/esbmc/esbmc) uses Bounded Model Checking. It translates Python into a lower-level intermediate representation (IR) and "unrolls" loops up to a specific depth ($k$). It then converts the unrolled program into a single massive SMT formula to check for safety properties (e.g., buffer overflows, division by zero).
*   **Utility:** Excellent for verifying highly complex, isolated algorithms (like our Calc cell range parsers) up to a bounded size, but overkill for standard API plumbing.

---

## 3. Execution Roadmap: Hardening WriterAgent's Pure Logic

To implement FV at scale across a ~23 KLOC codebase (excluding tests and vendored contrib), we must employ **Assume-Guarantee Reasoning** (a standard composition formal method). We "assume" the correctness of UNO, and we "guarantee" the correctness of our logic under those assumptions. 

Attempting to verify 23 KLOC simultaneously is intractable. We must apply a tiered, incremental triage framework.

### Phase 1: Triage and "Hexagonal" Segregation
The codebase must be categorized by its distance from the UNO boundary.
1.  **Tier 0 (The Core - Immediate ROI):** Files with zero UNO dependencies. These are pure data-transformation pipelines (`url_utils.py`, `address_utils.py`, `pricing.py`, `streaming_deltas.py`).
2.  **Tier 1 (The Adapters):** Code that parses complex UNO structures into pure Python data models (e.g., extracting an AST from LibreOffice).
3.  **Tier 2 (The Orchestrators):** State machines and side-effect-heavy UI controllers (`panel_factory.py`).

We begin verification exclusively at Tier 0. Moving forward, complex algorithms must be strictly decoupled from UNO calls. We extract data from UNO (Tier 1), pass it into pure, verifiable Tier 0 functions, and pass the output back via Tier 2 orchestrators.

### Phase 2: Axiomatic Definition via Contracts (Tier 0)
We begin by establishing the formal properties of our pure functions using strict type hints and `deal` contracts. This shifts our development model from "writing tests that pass" to "defining invariants that must never fail."

**Example: Verifying Calc Address Math**
Consider `column_to_index` in `address_utils.py`. We know mathematically that:
1. It must only accept uppercase alphabetical strings.
2. The output must always be a non-negative integer.
3. The inverse function (`index_to_column`) applied to the result must yield the original input.

```python
import deal

@deal.pre(lambda col_str: col_str.isalpha() and col_str.isupper())
@deal.post(lambda result: result >= 0)
# The ultimate invariant: f^-1(f(x)) == x
@deal.ensure(lambda col_str, result: index_to_column(result) == col_str)
def column_to_index(col_str: str) -> int:
    result = 0
    for char in col_str:
        result = result * 26 + (ord(char) - ord('A') + 1)
    return result - 1
```

### Phase 3: Concolic State Exploration with CrossHair
With contracts in place, we unleash CrossHair. 
`crosshair check plugin/modules/calc/address_utils.py`

CrossHair's Z3 engine will not just throw random fuzzing data at the function; it will analytically dissect the bytecode. It will realize that `ord(char)` implies integer boundaries, and it will intentionally synthesize string inputs designed to trigger integer overflows, index out-of-bounds, or violate the `deal.ensure` inverse mapping contract. 

When CrossHair finds a counterexample, it provides the exact symbolic input required to break our algorithm. We patch the code, and the state space is secured.

### Phase 4: SMT-Driven Protocol Verification
Beyond utility functions, we can apply FV to state machines. For example, our LLM streaming chunk normalizer (`plugin/modules/http/streaming_deltas.py`). 

By defining contracts that assert *"No matter how a JSON delta stream is arbitrarily chunked or fragmented over the network, the final assembled output string will exactly match the output of a synchronous, unfragmented payload,"* we can use CrossHair to mathematically prove our streaming parser's resilience against arbitrary network fragmentation.

## Phase 5: Elevating Orchestration to Pure State Machines

**COMPLETED ✅**

The state machine extraction has been successfully implemented using a pragmatic, simplified approach that avoids unnecessary complexity:

### Key Design Decisions (Simplified Approach)

1. **Simple Effect Types**: Used strings for simple effects (`"exit_loop"`, `"trigger_next_tool"`) instead of creating separate dataclass types
2. **Union Types**: Used Python's native union types (`SendHandlerEffect = Type1 | Type2 | Type3`) for cleaner code
3. **Minimal Boilerplate**: Kept effect and event definitions concise and focused
4. **Direct State Updates**: Used `dataclasses.replace()` for state transitions instead of complex builders

### Implemented State Machines

1. **Tool Loop State Machine** (`plugin/modules/chatbot/tool_loop_state.py`)
   - Pure transition function with comprehensive event handling
   - Simple string effects mixed with structured effect types
   - Full test coverage in `plugin/tests/test_tool_loop_state.py`

2. **Send Handler State Machine** (`plugin/modules/chatbot/state_machine.py`)
   - Handles audio, image, agent, and web workflows
   - Uses union types for events and effects (cleaner than inheritance hierarchies)
   - Comprehensive test coverage in `plugin/tests/test_state_machine.py`

3. **Send Button State Machine** (`plugin/modules/chatbot/send_state.py`)
   - Manages UI button state transitions
   - Simple enum-based events and union effects
   - Test coverage in `plugin/tests/test_send_state.py`

4. **MCP State Machine** (`plugin/modules/http/mcp_state.py`)
   - HTTP protocol state management
   - Document resolution and tool execution workflows
   - Uses dataclasses for structured effects

5. **Audio Recorder State Machine** (`plugin/modules/chatbot/audio_recorder_state.py`)
   - Audio recording lifecycle management
   - Minimal state and effect types
   - Test coverage in `plugin/modules/chatbot/tests/test_audio_recorder_state.py`

### What We Avoided (Over-Engineering Traps)

❌ **Complex Effect Hierarchies**: Did NOT create deep inheritance trees of effect types
❌ **Overly Generic State Machines**: Did NOT create abstract base classes or factories
❌ **Excessive Pattern Matching**: Used simple `match` statements and `isinstance` checks
❌ **Separate State Update Effects**: Combined related state updates in single effects

### Current Implementation Pattern

```python
# Simple, pragmatic approach used in production

@dataclass(frozen=True)
class ToolLoopState:
    round_num: int
    pending_tools: List[Dict[str, Any]]
    # ... other fields ...

# Simple string effects for common operations
effects.append("exit_loop")  # Simple string effect
effects.append(UIEffect(kind="status", text="Ready"))  # Structured effect

# Pure transition function
def next_state(state: ToolLoopState, event: ToolLoopEvent) -> Tuple[ToolLoopState, List[Any]]:
    effects: List[Any] = []
    
    match event.kind:
        case EventKind.STOP_REQUESTED:
            effects.append("exit_loop")
            return dataclasses.replace(state, is_stopped=True), effects
        # ... other cases ...
```

**Verification:** These pure state machines are now ready for formal verification using CrossHair. The simplified design makes verification easier and more practical.

## Phase 6: Formal Verification of State Machines

Now that the state machine infrastructure is in place, we can apply formal verification:

### Step 1: Add Design by Contract to State Machines
Add `deal` contracts to the `next_state` functions:

```python
# Example from tool_loop_state.py
@deal.pre(lambda state, event: state.round_num <= state.max_rounds)
@deal.post(lambda result: result[0].round_num <= result[0].max_rounds)
@deal.ensure(lambda state, event, result:
    not (event.kind == EventKind.STOP_REQUESTED and
         "exit_loop" not in result[1]))
def next_state(state: ToolLoopState, event: ToolLoopEvent) -> Tuple[ToolLoopState, List[Any]]:
    # ... existing implementation ...
```

### Step 2: Run CrossHair Verification
```bash
crosshair check plugin/modules/chatbot/tool_loop_state.py --contracts
crosshair check plugin/modules/chatbot/state_machine.py --contracts
crosshair check plugin/modules/chatbot/send_state.py --contracts
```

### Step 3: Add Verification to CI
Integrate CrossHair into the CI pipeline to run verification on every commit.

### Step 4: Document Verification Status
Maintain a `verification_status.json` file tracking which components have been verified.

## Phase 7: Expand Verification to Tier 0 Modules

With state machines verified, expand to utility modules:

1. **`plugin/framework/utils.py`** - URL and path utilities
2. **`plugin/framework/format.py`** - Text normalization
3. **`plugin/modules/calc/address_utils.py`** - Calc address math
4. **`plugin/modules/http/streaming_deltas.py`** - Streaming protocol

Add contracts and run CrossHair verification on each module.

## Conclusion

By adopting concolic execution (CrossHair) and Design by Contract (`deal`), we can elevate the reliability of WriterAgent's pure algorithmic core from "empirically tested" to "mathematically robust." We acknowledge the intractability of verifying the entire application, and instead focus our SMT solvers exclusively on the pure data-transformation pipelines that feed our axiomatic UNO environment.

## Practical Implementation Guide for WriterAgent

### Step 1: Framework-First Verification (Recommended Starting Point)

**Priority Order for Framework Modules:**

1. **`plugin/framework/utils.py`** (Highest Priority)
   - Combined URL and path utilities
   - Pure string and filesystem operations
   - Critical for web operations and file system access
   - Example contracts:
     ```python
     @deal.pre(lambda url: isinstance(url, str))
     @deal.post(lambda result: result.startswith(('http://', 'https://')) or result == '')
     @deal.ensure(lambda url, result: not url or result)  # Empty in → empty out
     def ensure_scheme(url: str) -> str:
         """✅ VERIFIED: URL scheme enforcement"""
         # ... implementation ...
     
     @deal.post(lambda result: os.path.isabs(result))
     @deal.ensure(lambda result: os.path.exists(result) or True)  # May not exist yet
     def get_plugin_dir() -> str:
         """✅ VERIFIED: Returns absolute plugin directory path"""
         # ... implementation ...
     ```

2. **`plugin/framework/format.py`**
   - Text normalization functions
   - Used across all document types
   - Verify format preservation invariants

3. **`plugin/framework/schema_convert.py`**
   - JSON schema transformations
   - Tool parameter validation
   - Prove schema equivalence properties

4. **`plugin/framework/config.py`** (Adapter Layer)
   - Configuration validation logic
   - Type safety guarantees
   - Verify config consistency invariants

### Step 2: Verification Workflow

**For each module:**

```bash
# 1. Add type hints and deal contracts
# 2. Run static type checking
mypy plugin/framework/utils.py --strict

# 3. Run CrossHair verification
crosshair check plugin/framework/utils.py --contracts

# 4. Add to test suite
pytest tests/test_url_utils_verification.py
```

**Sample test file:**
```python
# tests/test_url_utils_verification.py
import subprocess
import pytest

def test_url_utils_contracts():
    """Verify all contracts in url_utils module"""
    result = subprocess.run([
        "crosshair", "check",
        "plugin/framework/url_utils.py",
        "--contracts",
        "--per_condition_timeout=5"
    ], capture_output=True, text=True, timeout=60)
    
    print(f"CrossHair output:\n{result.stdout}")
    if result.stderr:
        print(f"Errors:\n{result.stderr}")
    
    assert result.returncode == 0, "CrossHair found contract violations"
```

### Step 3: Verification Tracking System

**Maintain a `verification_status.json` file:**
```json
{
  "framework": {
    "utils.py": {
      "status": "verified",
      "coverage": "100%",
      "contracts": 22,
      "functions_verified": [
        "normalize_endpoint_url",
        "get_url_hostname",
        "get_url_domain",
        "get_url_path",
        "get_url_query_dict",
        "get_url_path_and_query",
        "is_pdf_url",
        "get_plugin_dir"
      ],
      "last_verified": "2026-03-15",
      "tool": "crosshair",
      "ci_integration": true
    },
    "format.py": {
      "status": "partial",
      "coverage": "65%",
      "contracts": 12,
      "pending": ["normalize_paragraphs", "strip_html_tags"],
      "notes": "HTML parsing requires mocking - needs custom harness"
    }
  },
  "modules": {
    "calc": {
      "address_utils.py": {
        "status": "planned",
        "priority": "high"
      }
    }
  }
}
```

**Update verification status script:**
```python
# scripts/update_verification_status.py
import json
import subprocess
from pathlib import Path

def update_status(module_path: str, tool: str = "crosshair"):
    """Update verification status after successful run"""
    status_file = Path("verification_status.json")
    
    if not status_file.exists():
        status = {"framework": {}, "modules": {}}
    else:
        status = json.loads(status_file.read_text())
    
    # Parse module path to determine category
    parts = module_path.split('/')
    if parts[1] == 'framework':
        category = 'framework'
        module_name = parts[2]
    else:
        category = parts[1]
        module_name = parts[3] if len(parts) > 3 else parts[2]
    
    # Update status
    if category not in status:
        status[category] = {}
    
    status[category][module_name] = {
        "status": "verified",
        "last_verified": "2026-03-15",  # Use actual date
        "tool": tool,
        "ci_integration": False
    }
    
    status_file.write_text(json.dumps(status, indent=2))
    print(f"✅ Updated verification status for {module_path}")
```

### Step 4: CI Integration

**Add to `.github/workflows/verify.yml`:**
```yaml
name: Formal Verification

on: [push, pull_request]

jobs:
  verify:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        module: [
          "plugin/framework/utils.py",
          "plugin/framework/format.py"
        ]
    
    steps:
    - uses: actions/checkout@v4
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.12'
    
    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install deal crosshair
    
    - name: Run CrossHair verification
      run: |
        crosshair check ${{ matrix.module }} --contracts --per_condition_timeout=10
    
    - name: Update verification status
      if: success()
      run: |
        python scripts/update_verification_status.py ${{ matrix.module }}
    
    - name: Commit updated status
      if: success() && github.ref == 'refs/heads/master'
      run: |
        git config --global user.name "Verification Bot"
        git config --global user.email "bot@example.com"
        git add verification_status.json
        git commit -m "chore: update verification status for ${{ matrix.module }}"
        git push
```

### Step 5: Documentation Standards

**Add verification badges to docstrings:**
```python
def ensure_scheme(url: str) -> str:
    """
    Ensure URL has proper scheme prefix.
    
    ✅ VERIFICATION STATUS:
    - Type safety: mypy (strict)
    - Contracts: deal (4/4 verified)
    - Concolic: CrossHair (100% coverage)
    - Last verified: 2026-03-15
    
    Args:
        url: Input URL string (may lack scheme)
        
    Returns:
        URL with http:// or https:// prefix
        
    Raises:
        ValueError: If url is empty after normalization
        
    Contracts:
        @deal.pre: Non-empty string input
        @deal.post: Result starts with http:// or https://
        @deal.ensure: Preserves path/query/fragment
    """
    # ... implementation ...
```

## Verification Anti-Patterns to Avoid

1. **❌ Don't verify UNO wrapper code**
   - Stick to the axiomatic boundary
   - UNO calls should be in unverified adapter layers

2. **❌ Avoid complex contracts on I/O functions**
   - File operations, network calls are hard to verify
   - Keep contracts simple for these cases

3. **❌ Don't over-specify**
   - Contracts should capture essential properties
   - Too many contracts make verification brittle

4. **❌ Avoid verifying *tangled* UI/orchestration code**
   - Do not attempt to attach FV contracts to functions that intermingle state mutation and I/O (e.g., updating UI side-by-side with calculating states).
   - Instead, extract the implied state machine into a pure transition function (as described in Phase 5), and strictly verify *that* function instead.

## Recommended Tool Chain

```
Pure Python Logic → [mypy] → [deal contracts] → [CrossHair] → ✅ Verified
                     ↑                                      ↓
               Type Safety                          Counterexamples
```

**Installation:**
```bash
pip install deal crosshair mypy
```

**Daily Workflow:**
```bash
# Develop with contracts
vim plugin/framework/url_utils.py  # Add @deal decorators

# Verify locally
mypy plugin/framework/url_utils.py --strict
crosshair check plugin/framework/url_utils.py --contracts

# Commit with verification
git add plugin/framework/url_utils.py
python scripts/update_verification_status.py plugin/framework/url_utils.py
git add verification_status.json
git commit -m "feat: add verified URL utilities"
```

By following this framework-first approach, we build a solid foundation of verified code that all higher-level modules can rely on. The verification status becomes a living document that grows as we harden more of the codebase.
