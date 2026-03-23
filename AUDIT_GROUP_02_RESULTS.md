# Error Audit Results - Group 02 (Document & Config)

## Summary
- Total files audited: 10
- Total broad catches found: 58
- Critical: 27 | Medium: 18 | Low: 13
- **Document ops**: 32 catches | **Config ops**: 9 catches

## Detailed Findings

### 1. plugin/framework/document.py
**Total catches**: 32 - **Priority: HIGH**

#### Pattern 1 - Document Object Type Checks (Lines 31, 40, 50)
- **Category**: Critical
- **Context**: `is_writer`, `is_calc`, `is_draw` type checking logic.
- **Current Handling**: Returns `False` on any exception.
- **Issues**: Fails silently, masking underlying disposal or context errors.
- **Recommendation**: Use `UnoObjectError` to propagate type check failures or use `check_disposed` to ensure safety first.
- **Code Example**:
  ```python
  # Current
  try:
      return model.supportsService("com.sun.star.text.TextDocument")
  except Exception as e:
      logging.getLogger(__name__).debug("is_writer exception: %s", type(e).__name__)
      return False

  # Recommended
  try:
      check_disposed(model, "Document")
      return safe_call(model.supportsService, "Document service check", "com.sun.star.text.TextDocument")
  except UnoObjectError as e:
      logging.getLogger(__name__).warning(f"is_writer failed: {e}")
      return False
  ```

#### Pattern 2 - Document Properties Reading/Writing (Lines 69, 76, 78, 97, 107, 113, 115)
- **Category**: Critical
- **Context**: `get_document_property` and `set_document_property`.
- **Current Handling**: Nested bare exceptions to try different LibreOffice implementations.
- **Issues**: Highly fragile and suppresses real execution failures.
- **Recommendation**: Refactor into a linear flow with `safe_call` and throw `UnoObjectError` on ultimate failure.
- **Code Example**:
  ```python
  # Current
  try:
      if props.hasByName(name):
          return props.getPropertyValue(name)
      return default
  except Exception:
      pass

  # Recommended
  try:
      check_disposed(props, "Properties")
      if safe_call(props.hasByName, "Check property name", name):
          return safe_call(props.getPropertyValue, "Get property value", name)
      return default
  except UnoObjectError as e:
      log.warning(f"Failed to read property {name}: {e}")
      return default
  ```

#### Pattern 3 - Text & Context Extraction (Lines 219, 222, 234, 261, 277, 295, 327, 354, 377, 461)
- **Category**: Critical
- **Context**: `get_full_document_text`, `get_document_context_for_chat`, etc.
- **Current Handling**: Broad exceptions return empty string or error strings.
- **Issues**: Masks document lock, initialization, or disposal states.
- **Recommendation**: Ensure document is valid with `check_disposed` and use `safe_call` for UNO calls (`getText()`, `getString()`).
- **Code Example**:
  ```python
  # Current
  try:
      text = model.getText()
      cursor = text.createTextCursor()
      cursor.gotoStart(False)
      cursor.gotoEnd(True)
      full = normalize_linebreaks(cursor.getString())
      doc_len = len(full)
  except Exception as e:
      logging.getLogger(__name__).warning("get_document_context_for_chat Writer exception: %s", type(e).__name__)
      return "[Unable to read Writer document context. The document may be locked or initializing.]"

  # Recommended
  try:
      check_disposed(model, "Document")
      text = safe_call(model.getText, "Document text")
      cursor = safe_call(text.createTextCursor, "Create cursor")
      safe_call(cursor.gotoStart, "Cursor start", False)
      safe_call(cursor.gotoEnd, "Cursor end", True)
      full = normalize_linebreaks(safe_call(cursor.getString, "Get string"))
      doc_len = len(full)
  except UnoObjectError as e:
      logging.getLogger(__name__).warning(f"get_document_context_for_chat failed: {e}")
      return "[Unable to read Writer document context. The document may be locked or initializing.]"
  ```

#### Pattern 4 - Heading & Navigation Operations (Lines 516, 520, 573, 576, 595, 648, 677, 754, 773)
- **Category**: Medium/Low
- **Context**: `find_paragraph_for_range`, `build_heading_tree`, `ensure_heading_bookmarks`.
- **Current Handling**: Bare catches to skip broken paragraphs/headings.
- **Issues**: Silences unexpected UNO structure errors without bubbling up.
- **Recommendation**: Log specific `UnoObjectError` occurrences but continue iteration if safe.

### 2. plugin/framework/config.py
**Total catches**: 9 - **Priority: HIGH**

#### Pattern 1 - Config Path Resolution (Lines 99, 111)
- **Category**: Low
- **Context**: `_config_path` and `user_config_dir` resolution.
- **Current Handling**: Returns `None`.
- **Issues**: Bootstrapping fails silently.
- **Recommendation**: Raise `ConfigError` indicating missing path.

#### Pattern 2 - JSON Parsing and IO (Lines 258, 636, 1042, 1155, 1168)
- **Category**: Critical
- **Context**: Loading and saving the config dict to `writeragent.json`.
- **Current Handling**: Broad `except Exception` or `except (IOError, json.JSONDecodeError)` resulting in empty dict fallback.
- **Issues**: Unvalidated JSON schemas load silently as empty; IO permission errors are masked.
- **Recommendation**: Catch specific exceptions and raise `ConfigError` for invalid JSON.
- **Code Example**:
  ```python
  # Current
  try:
      with open(self._config_path, "r") as f:
          data = json.load(f)
          if key in data:
              return data[key]
  except Exception as e:
      log.debug("ConfigService.get config file read error for key %s: %s", key, e)

  # Recommended
  try:
      with open(self._config_path, "r") as f:
          data = json.load(f)
          if not isinstance(data, dict):
              raise ConfigError("Config must be a JSON object", "CONFIG_INVALID_FORMAT")
          if key in data:
              return data[key]
  except json.JSONDecodeError as e:
      raise ConfigError(
          f"Invalid JSON: {str(e)}",
          "CONFIG_JSON_ERROR",
          details={"position": f"line {e.lineno} col {e.colno}"}
      )
  except OSError as e:
      log.warning(f"ConfigService.get IO error: {e}")
  ```

#### Pattern 3 - Runtime Validation (Lines 512, 1118)
- **Category**: Medium
- **Context**: `validate()` and HTTP requests to endpoints.
- **Current Handling**: Catch-all logging.
- **Issues**: Suppresses bad configuration validation.
- **Recommendation**: Raise `ConfigError`.

### 3. plugin/framework/image_utils.py
**Total catches**: 6 - **Priority: MEDIUM**
- **Context**: Exif parsing, base64 encoding, temporary file generation.
- **Current Handling**: Silent skip/fallback to empty byte arrays.
- **Issues**: Fails to communicate IO write errors or corrupted metadata.
- **Recommendation**: Use specific `IOError` or `ValueError` catching. When an image fundamentally fails to process, wrap in an `ImageProcessingError` (if defined) or log explicitly.

### 4. plugin/framework/format.py
**Total catches**: 3 - **Priority: MEDIUM**
- **Context**: Markdown parsing and string normalization fallbacks.
- **Recommendation**: Tighten to `TypeError` or `ValueError`.

### 5. plugin/framework/service_registry.py
**Total catches**: 2 - **Priority: LOW**
- **Context**: Dynamic module auto-discovery.
- **Recommendation**: Keep, but explicitly catch `ImportError` or `AttributeError` instead of base `Exception`.

### 6. plugin/framework/settings_dialog.py
**Total catches**: 2 - **Priority: MEDIUM**
- **Context**: Translating strings in `get_settings_field_specs`.
- **Recommendation**: Use `ConfigError` when UI bindings fail.

### 7. plugin/framework/uno_context.py
**Total catches**: 1 - **Priority: CRITICAL**
- **Context**: `get_active_document()` UNO Desktop resolution.
- **Current Handling**: Silently returns `None`.
- **Issues**: The entire plugin's active document awareness fails silently.
- **Recommendation**: Use `safe_call` and `check_disposed` to raise `UnoObjectError` to alert the host execution loop.

### 8. plugin/framework/listeners.py, event_bus.py, pricing.py
**Total catches**: 3 (1 each) - **Priority: LOW**
- **Context**: Event emission and pricing lookups.
- **Recommendation**: Log the error distinctly; broad catches in event loops are standard but should at least isolate the exact exception class.

## Execution Plan to Fix All Catches

The goal is to systematically refactor all 58 catches across the 10 files. This will be done in phases to isolate risk and ensure stability.

### Phase 1: Establish Utilities & Base Classes
1. **File:** `plugin/framework/errors.py` (or similar utility module)
   - Ensure the `UnoObjectError` and `ConfigError` classes exist with the appropriate `code` and `details` fields.
   - Implement `check_disposed(model, context_name)` to raise `UnoObjectError` if the object is disposed or null.
   - Implement `safe_call(fn, context_name, *args, **kwargs)` to catch generic exceptions during UNO bridge calls and wrap them in `UnoObjectError`.

### Phase 2: Core Document Operations (`document.py` & `uno_context.py`)
1. **File:** `uno_context.py`
   - Refactor `get_active_document()` to use `safe_call` and `check_disposed` to ensure the desktop component is valid.
2. **File:** `document.py`
   - Refactor Type Checkers (`is_writer`, `is_calc`, `is_draw`) to use `check_disposed` and `safe_call`. Catch `UnoObjectError`.
   - Refactor Property Accessors (`get_document_property`, `set_document_property`) to eliminate nested generic catches and use explicit `hasByName` checks with `safe_call`.
   - Refactor Content Extractors (`get_full_document_text`, `get_document_context_for_chat`, etc.) to wrap UNO text manipulation and cursor iteration in `safe_call`.

### Phase 3: Configuration Management (`config.py` & `settings_dialog.py`)
1. **File:** `config.py`
   - Refactor config loading (`_get_validated_config_dict`, `get_config_dict`) to catch `json.JSONDecodeError` and `OSError` explicitly, raising `ConfigError` with file details.
   - Refactor config saving (`set_config`, `remove_config`) to handle explicit IO exceptions and raise `ConfigError`.
   - Update path resolution `_config_path` to raise `ConfigError` instead of returning `None`.
2. **File:** `settings_dialog.py`
   - Update translation string extraction to catch explicit missing key/mapping errors and raise `ConfigError`.

### Phase 4: Utilities and Fallbacks (`image_utils.py`, `format.py`, `service_registry.py`, `listeners.py`, `event_bus.py`, `pricing.py`)
1. **File:** `image_utils.py`
   - Refactor Exif and Base64 handling to catch `ValueError`, `TypeError`, and `OSError` explicitly.
2. **File:** `format.py`
   - Refactor string normalization and Markdown parsing to catch `TypeError` and `ValueError`.
3. **File:** `service_registry.py`
   - Tighten module auto-discovery to catch `ImportError` and `AttributeError` explicitly.
4. **Files:** `listeners.py`, `event_bus.py`, `pricing.py`
   - Update generic catches to isolate the specific exceptions expected (e.g., `KeyError` for pricing, `TypeError` for listener callbacks) and add distinct error logging.

### Phase 5: Verification
1. Run the test suite (`uv run --extra dev pytest plugin/tests/`).
2. Run UNO native tests (`uv run python -m plugin.testing_runner`) to verify that the `safe_call` wrapper correctly bridges the LibreOffice C++ boundary without side effects.
