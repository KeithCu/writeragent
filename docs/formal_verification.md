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

To implement FV, we must isolate our verifiable code from our axiomatic environment (UNO).

### Phase 1: Segregation of Pure Logic (The "Hexagonal" Core)
Verification requires deterministic boundaries. Our current architecture already has pockets of pure logic (e.g., `plugin/framework/url_utils.py`, `plugin/modules/calc/address_utils.py`, and `plugin/framework/pricing.py`).

Moving forward, complex transformations (like AST parsing, text delta computation, or HTML sanitization) must be strictly decoupled from UNO calls. We extract data from UNO, pass it into pure, verifiable Python functions, and pass the output back to UNO.

### Phase 2: Axiomatic Definition via Contracts
We begin by establishing the formal properties of our pure functions using type hints and `deal` contracts. This shifts our development model from "writing tests that pass" to "defining invariants that must never fail."

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

## Conclusion

By adopting concolic execution (CrossHair) and Design by Contract (`deal`), we can elevate the reliability of WriterAgent's pure algorithmic core from "empirically tested" to "mathematically robust." We acknowledge the intractability of verifying the entire application, and instead focus our SMT solvers exclusively on the pure data-transformation pipelines that feed our axiomatic UNO environment.
