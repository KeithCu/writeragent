# Executing Native Python in LibreOffice Calc Cells

This document explains the architectural pattern for allowing users to write and execute native Python code directly inside LibreOffice Calc cells, as inspired by the `LibrePythonista` extension.

## The Core Problem
To evaluate raw Python strings, you typically use `exec()`. However, `exec()` returns `None`. If a user types `x = 10 + 20` into a cell, LibreOffice needs the result (`30`) so it can display it in the spreadsheet. You cannot rely on users awkwardly appending `return 30` to the end of every cell script.

## The Solution: AST Parsing and "The Last Expression" Hack

The most robust way to evaluate a block of Python code and extract its final assigned value or expression is to manipulate the Python **Abstract Syntax Tree (AST)** before compiling the code.

### How It Works
Instead of directly passing the code string to `exec()`, the engine does the following:

1. **Parse into an AST**:
   ```python
   import ast
   tree = ast.parse(user_code_string, mode="exec")
   ```
2. **Pop the Last Node**:
   Identify the very last statement in the user's code. If the last statement is an expression (`ast.Expr`) or an assignment (`ast.Assign`), remove it from the tree.
   ```python
   last_node = tree.body[-1]
   last_expr = None
   
   if isinstance(last_node, (ast.Expr, ast.Assign, ast.AnnAssign)):
       last_expr = tree.body.pop()
   ```
3. **Execute the Bulk of the Code**:
   Compile the remaining nodes normally using `exec()`. This ensures loops, function definitions, and imports all execute properly.
   ```python
   module_body = ast.fix_missing_locations(ast.Module(body=tree.body, type_ignores=[]))
   exec_code = compile(module_body, "<string>", "exec")
   exec(exec_code, globals_dict, local_dict)
   ```
4. **Evaluate the Last Node for the Result**:
   Convert that final popped node back into an expression, compile it in `"eval"` mode, and capture its result. This result is what you return back to the LibreOffice Calc cell!
   ```python
   if last_expr:
       expr = ast.Expression(last_expr.value)
       expr = ast.fix_missing_locations(expr)
       eval_code = compile(expr, "<string>", "eval")
       result = eval(eval_code, globals_dict, local_dict)
       return result
   ```

## The Persistent Global Environment
Cells in a spreadsheet should not be isolated from one another. If Cell `A1` contains `my_var = 100`, Cell `A2` should be able to evaluate `my_var * 2`.

To achieve this, the extension creates a **Shared Singleton Module**:
```python
import types

class PyModule:
    def __init__(self):
        # Create a single virtual module that lives as long as the spreadsheet is open
        self.mod = types.ModuleType("CalcSharedEnv")
```
When executing `exec(exec_code, globals_dict, local_dict)`, you always pass `self.mod.__dict__` as the `globals_dict`. Because every cell executes against this exact same dictionary, state seamlessly persists between cell executions.

## Automatic Context Injection
You can automatically inject variables into this global dictionary *before* any user code runs. This makes the feature much more powerful without demanding boilerplate from the user.

For example, before parsing the user's AST, you can inject:
```python
self.mod.__dict__['CURRENT_CELL'] = get_active_unocomponent_cell()
```
The user can then seamlessly write Python code that inherently "knows" where it is located:
```python
# User's code in the cell:
if CURRENT_CELL.CellAddress.Row == 0:
    "I am in the top row!"
else:
    "I am somewhere else"
```

## Files to Grab & Detailed Code Walkthrough

If you want to port this feature directly into your extension, here are the exact files to harvest from the `python_libre_pythonista_ext` codebase and how their specific implementations work:

### 1. `oxt/pythonpath/libre_pythonista_lib/doc/calc/doc/sheet/cell/code/py_module.py`
**What it is:** This is the heart of the execution engine. It's the file where the `PyModule` singleton and the AST compilation logic live.
**How it works in detail:**
- It defines `get_module_init_code()`, which returns a giant multi-line string of auto-imports (e.g., `import math`). When the singleton `PyModule` first spins up, it executes this block of code against its own globals dictionary so that things like `math` are instantly available to users.
- It contains `execute_code(self, code_snippet: str, globals_dict=None)`. This function is wrapped in lots of `try/except` and logging logic to safely catch user syntax errors. It carefully translates assignments into locals vs globals so users don't accidentally poison the shared memory with temporary variables.
- It provides `reset_module()` and `reset_to_dict()` which allows LibreOffice to forcefully wipe the virtual environment memory if you want to perform a clean evaluation.

### 2. `oxt/pythonpath/libre_pythonista_lib/doc/calc/doc/sheet/cell/code/rules/`
**What it is:** A directory containing a mini Rules Engine that runs after `execute_code()` succeeds. 
**How it works in detail:**
- Just because `execute_code()` returns `[1, 2, 3]` doesn't mean LibreOffice Calc knows what to do with a Python list. 
- The `CodeRules` engine loops through a list of classes (`rule_primitive.py`, `rule_list.py`, etc.). It inspects the `result` of the AST evaluation.
- If `result` is an integer, it returns it as a float (since LibreOffice handles numbers that way).
- If `result` is a nested list `[[1,2], [3,4]]`, the rule engine recognizes this as a 2D array. If you port this logic, you can automatically convert any returned Python 2D array into a `XCellRangeData` UNO array injection, allowing users to return entire tables of data from a single Python formula.

### 3. `oxt/prompt_function.py` (Or Your Custom `=PROMPT()` alternative)
**Integration point:** In a typical UNO implementation, you bind a specific spreadsheet formula (like `=PYTHON("x = 1+1")`) to a python script execution block. You would hook the AST parsing from `py_module.py` inside your own `prompt_function.py` equivalent. When LibreOffice calls the formula function, your python UNO service catches the string, executes it against the singleton, translates it using the CodeRules logic, and returns a standard primitive type or Uno Any array that the spreadsheet can cleanly render.

## Adapting for a "Pure Basic Python" Proof of Concept
If you want to build a lightweight version of this generic Python execution feature entirely avoiding the `numpy`, `pandas`, and heavy pip-bootstrapping dependencies, you can! The core execution engine is actually entirely pure Python.

Here is how you isolate and shrink the code for a basic MVP:

### 1. Gut the Initializer (`get_module_init_code`)
In `py_module.py` (or your equivalent class), look at the function that generates the initial `globals_dict` context. Remove all `import pandas` and `import numpy` injections. 
Instead, populate it strictly with pure python modules or your own helper functions:
```python
def get_module_init_code():
    return """
import math
import datetime
import random
import json
"""
```

### 2. Simplify the Rules Engine
You do not need to implement complex `XCellRangeData` handlers for Pandas `DataFrame` exports or `Matplotlib` SVG generation. Your rules engine can just check primitives:
```python
# A vastly simplified Rule Engine intercepting the result of `execute_code()`
def format_result_for_calc(result):
    if isinstance(result, (int, float, bool)):
        return float(result) # Calc prefers floats
    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple, dict)):
        # Just convert complex structures to JSON strings so the user can easily see them in the cell
        import json
        return json.dumps(result)
    
    return "Error: Unsupported Return Type"
```

### 3. Retain the AST Logic Exactly As-Is
The AST parsing method utilizing `isinstance(last_node, ast.Expr)` does not rely on third-party libraries. You can copy the body of `execute_code()` wholesale. 

With just these three components—the AST pop-and-eval trick, a shared `mod.__dict__` singleton, and a primitive return formatter—you can build a fully functional `=PYTHON()` formula for LibreOffice entirely out of standard library Python that runs perfectly straight out of the box.
