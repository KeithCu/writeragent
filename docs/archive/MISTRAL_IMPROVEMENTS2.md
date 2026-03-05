# LocalWriter Improvement Plan (Detailed)

This document outlines detailed improvements for the LocalWriter codebase, focusing on reducing complexity, improving maintainability, and enhancing user experience. Each suggestion includes implementation details, estimated complexity, and tradeoffs.

---

## 1. Config Management Unification

### Problem
- Legacy config keys (`chat_system_prompt`) coexist with new ones (`additional_instructions`), causing confusion.
- Config I/O logic is duplicated across `main.py` and `panel_factory.py`.
- No single source of truth for config schema.

### Proposed Changes

#### a) Rename `chat_system_prompt` → `additional_instructions`
- **Files**: `core/config.py`, `main.py`, `panel_factory.py`, `SettingsDialog.xdl`
- **Complexity**: Low (search-replace + migration logic)
- **Migration**: Add one-time migration in `get_config()`:
  ```python
  if config.get('chat_system_prompt') and 'additional_instructions' not in config:
      config['additional_instructions'] = config.pop('chat_system_prompt')
  ```

#### b) Centralize config I/O
- **New file**: `core/config_io.py`
- **Contents**:
  - `read_config(ctx)` → dict
  - `write_config(ctx, data)`
  - `migrate_legacy_config(data)`
- **Complexity**: Medium (refactor 5 call sites)
- **Benefit**: Single place for path resolution, error handling, and schema validation.

#### c) Schema validation
- **Add**: `CONFIG_SCHEMA` dict in `core/config.py` with types and defaults.
- **Use**: Validate on write via `jsonschema` (optional dependency).
- **Complexity**: Low (add schema, validate in `set_config`).

---

## 2. Dialog/UI Refactoring

### Problem
- XDL helpers (`TabListener`, `get_optional`) are duplicated.
- SettingsDialog.xdl has unused controls from iterative development.
- No abstraction for common dialog patterns (tabs, optional controls).

### Proposed Changes

#### a) Extract `core/xdl_utils.py`
- **Contents**:
  ```python
  class TabListener(unohelper.Base, XActionListener):
      def __init__(self, dialog, page): ...
  
  def get_optional(dialog, control_name): ...
  def get_checkbox_state(ctrl): ...
  def set_checkbox_state(ctrl, value): ...
  ```
- **Complexity**: Low (move existing code)
- **Impact**: Reduces `main.py` and `panel_factory.py` by ~50 lines each.

#### b) Clean up SettingsDialog.xdl
- **Actions**:
  - Remove `old_api_key_field` (legacy)
  - Remove `unused_fixedline_3`
  - Consolidate duplicate spacing controls
- **Complexity**: Low (XML edits)
- **Risk**: None (unused controls)

#### c) Abstract dialog wiring
- **New**: `DialogWiring` class in `core/xdl_utils.py`:
  ```python
  class DialogWiring:
      def __init__(self, dialog, field_specs):
          self.dlg = dialog
          self.specs = field_specs  # [{'name': 'endpoint', 'type': 'text'}, ...]
      
      def populate(self, config):
          for spec in self.specs:
              ctrl = self.dlg.getControl(spec['name'])
              if spec['type'] == 'text':
                  ctrl.getModel().Text = config.get(spec['name'], '')
              # ... other types
      
      def read(self):
          return {spec['name']: self._read_control(spec) for spec in self.specs}
  ```
- **Complexity**: Medium (refactor `main.py` wiring)
- **Benefit**: Reuse for Settings and EditInput dialogs; easier to add new fields.

---

## 3. Tool Framework Consolidation

### Problem
- Three separate tool registries (`WRITER_TOOLS`, `CALC_TOOLS`, `DRAW_TOOLS`).
- Manual doc-type checks (`is_writer`, `is_calc`) scattered across code.
- No discovery mechanism for new document types.

### Proposed Changes

#### a) Unified tool registry
- **New**: `plugin/framework/tool_registry.py`:
  ```python
  class ToolRegistry:
      def __init__(self):
          self._tools = {}  # 'writer' → [ToolDef], 'calc' → [ToolDef]
      
      def register(self, doc_type, tool_def): ...
      def get_tools(self, doc_type): ...
      def get_executor(self, doc_type): ...
  
  TOOL_REGISTRY = ToolRegistry()
  
  # In writer_ops.py:
  TOOL_REGISTRY.register('writer', ToolDef('list_styles', list_styles, ...))
  ```
- **Complexity**: High (refactor 3 tool files + document_tools.py)
- **Benefit**: Single place to add new tools; auto-discovery.

#### b) Document type enum
- **New**: `core/document_types.py`:
  ```python
  from enum import Enum
  
  class DocumentType(Enum):
      WRITER = 'writer'
      CALC = 'calc'
      DRAW = 'draw'
      UNKNOWN = 'unknown'
  
  def get_document_type(model):
      if model.supportsService('com.sun.star.text.TextDocument'):
          return DocumentType.WRITER
      # ... other types
  ```
- **Complexity**: Low (extract existing logic)
- **Impact**: Replace `is_writer(model)` with `get_document_type(model) == DocumentType.WRITER`.

#### c) Auto-wire tools in sidebar
- **Change**: In `panel_factory.py`, replace:
  ```python
  if is_writer(self.doc):
      tools = WRITER_TOOLS
      executor = execute_tool
  elif is_calc(self.doc):
      tools = CALC_TOOLS
      executor = execute_calc_tool
  ```
  With:
  ```python
  doc_type = get_document_type(self.doc)
  tools = TOOL_REGISTRY.get_tools(doc_type)
  executor = TOOL_REGISTRY.get_executor(doc_type)
  ```
- **Complexity**: Low (use new registry)
- **Benefit**: Adding Impress support later only requires registering tools.

---

## 4. Streaming Unification

### Problem
- Streaming logic duplicated in `panel_factory.py`, `main.py`, `prompt_function.py`.
- Inconsistent error handling and queue item formats.

### Proposed Changes

#### a) Move streaming to `core/async_stream.py`
- **Add**:
  ```python
  def run_stream_drain_loop(queue, on_chunk, on_thinking, on_done, on_error):
      while True:
          try:
              item = queue.get(timeout=0.1)
              if item[0] == 'chunk':
                  on_chunk(item[1])
              elif item[0] == 'thinking':
                  on_thinking(item[1])
              # ... other types
          except queue.Empty:
              toolkit.processEventsToIdle()
  ```
- **Complexity**: Medium (refactor 3 call sites)
- **Benefit**: Single place for timeout logic, error handling, and `processEventsToIdle` safety.

#### b) Standardize queue items
- **Define**: `QueueItem` namedtuple in `core/async_stream.py`:
  ```python
  QueueItem = namedtuple('QueueItem', ['type', 'data'])
  # type: 'chunk' | 'thinking' | 'stream_done' | 'error' | 'stopped'
  ```
- **Complexity**: Low (search-replace string tuples)
- **Benefit**: Type safety; easier to add new item types.

---

## 5. Error Handling Centralization

### Problem
- Ad-hoc `MessageBox` calls with inconsistent messages.
- No error context (e.g., "Failed to generate image").
- Errors written to selection in some paths (bad UX).

### Proposed Changes

#### a) Centralized error display
- **New**: `core/ui.py`:
  ```python
  def show_error(ctx, message, title="LocalWriter Error"):
      toolkit = ctx.getServiceManager().createInstanceWithContext(
          "com.sun.star.awt.Toolkit", ctx)
      msgbox = toolkit.createMessageBox(...)
      msgbox.execute()
  ```
- **Complexity**: Low (extract existing code)
- **Impact**: Replace all `MessageBox` calls.

#### b) Error context
- **Add**: `error_context` parameter to API calls:
  ```python
  try:
      result = api_call(...)
  except Exception as e:
      show_error(ctx, f"{error_context}: {format_error_for_display(e)}")
  ```
- **Complexity**: Low (add parameter)
- **Benefit**: Users see "Failed to generate image: Connection refused" instead of generic errors.

#### c) Never write errors to document
- **Audit**: Search for `setString` calls in error paths.
- **Fix**: Replace with `show_error`.
- **Complexity**: Low (5-10 sites)

---

## 6. Logging Improvements

### Problem
- `init_logging` called early, can fail before ctx is fully ready.
- No log rotation (files grow indefinitely).
- Agent log enabled globally; no per-session control.

### Proposed Changes

#### a) Lazy log initialization
- **Change**: Defer path resolution until first `debug_log`:
  ```python
  _log_path = None
  
  def debug_log(msg, context=None):
      global _log_path
      if _log_path is None:
          _log_path = _resolve_log_path()
      # ... write to _log_path
  ```
- **Complexity**: Low (refactor `init_logging`)
- **Benefit**: No early failures; works in headless tests.

#### b) Log rotation
- **Add**: Rotate when file > 5MB:
  ```python
  def _rotate_logs():
      if os.path.getsize(_log_path) > 5 * 1024 * 1024:
          for i in range(4, 0, -1):
              src = f"{_log_path}.{i}"
              dst = f"{_log_path}.{i+1}"
              if os.path.exists(src):
                  os.rename(src, dst)
          os.rename(_log_path, f"{_log_path}.1")
  ```
- **Complexity**: Low (add to `debug_log`)
- **Benefit**: Prevents disk filling.

#### c) Per-session agent log
- **Change**: Add `enable_agent_log_this_session` config key.
- **Reset**: On restart, set to False (opt-in per session).
- **Complexity**: Low (add key + checkbox in Settings)

---

## 7. Image Generation Simplification

### Problem
- Two separate provider classes with duplicated logic (timeouts, retries).
- Config keys scattered (`image_provider`, `aihorde_api_key`, `image_model`).
- No unified error handling for image failures.

### Proposed Changes

#### a) Unified `ImageProvider`
- **New**: `core/image_service.py`:
  ```python
  class ImageProvider:
      def __init__(self, backend, api_config):
          self.backend = backend  # 'aihorde' or 'endpoint'
          self.api_config = api_config
  
      def generate(self, prompt, **kwargs):
          if self.backend == 'aihorde':
              return self._generate_aihorde(prompt, **kwargs)
          else:
              return self._generate_endpoint(prompt, **kwargs)
  ```
- **Complexity**: Medium (merge two classes)
- **Benefit**: Single place for retries, timeouts, and error handling.

#### b) Config group
- **Add**: `IMAGE_CONFIG_KEYS` in `core/config.py`:
  ```python
  IMAGE_CONFIG_KEYS = [
      'image_provider', 'image_model', 'aihorde_api_key',
      'image_width', 'image_height', 'image_cfg_scale',
      # ... others
  ]
  ```
- **Use**: In Settings dialog wiring and validation.
- **Complexity**: Low (extract list)

#### c) Better error messages
- **Add**: Translate AI Horde errors (e.g., "NSFW detected") to user-friendly messages.
- **Complexity**: Low (add mapping in `image_service.py`)

---

## 8. Testing Improvements

### Problem
- No integration tests for multi-doc scoping.
- Format preservation not tested with exotic formatting.
- Tool-calling edge cases (e.g., malformed JSON) not covered.

### Proposed Changes

#### a) Multi-doc scoping test
- **New**: `tests/test_multi_doc.py`:
  ```python
  def test_sidebar_scoping():
      # Open two Writer docs
      doc1 = create_doc("Doc1")
      doc2 = create_doc("Doc2")
      
      # Create sidebar for doc1
      panel1 = ChatPanelFactory.create_for_doc(doc1)
      panel1.send_message("Make this bold")
      
      # Verify doc2 unchanged
      assert get_text(doc2) == ""
  ```
- **Complexity**: High (requires UNO test harness)
- **Benefit**: Prevents regressions in document targeting.

#### b) Format preservation tests
- **Extend**: `tests/format_tests.py`:
  - Test with `CharBackColor`, `CharWeight`, `CharPosture` combinations.
  - Test same-length, longer, shorter replacements.
  - Test across paragraph boundaries.
- **Complexity**: Medium (add test cases)

#### c) Tool-calling edge cases
- **Add**: Tests for:
  - Malformed JSON in tool calls
  - Missing required parameters
  - Timeout during tool execution
- **Complexity**: Low (mock-based)

---

## 9. Documentation Pruning

### Problem
- `AGENTS.md` mixes architecture with implementation details.
- Outdated sections (e.g., legacy config keys).
- Hard to find critical gotchas.

### Proposed Changes

#### a) Split `AGENTS.md`
- **New files**:
  - `ARCHITECTURE.md`: High-level overview (1 page).
  - `GOTCHAS.md`: Critical gotchas only (e.g., dialog deadlocks, doc scoping).
  - `DEVELOPMENT.md`: Implementation details for contributors.
- **Complexity**: Medium (restructure content)
- **Benefit**: Easier onboarding; less noise.

#### b) Remove redundant comments
- **Audit**: Search for comments like "# Get the document model" that restate the code.
- **Action**: Delete or replace with why/when (e.g., "# Required for Calc; Writer uses different path").
- **Complexity**: Low (editor work)

---

## 10. Performance: Document Metadata Cache

### Problem
- `get_document_length`, `build_heading_tree` called repeatedly.
- No caching for styles, comments, tables.

### Proposed Changes

#### a) Extend `DocumentCache`
- **Add**:
  ```python
  class DocumentCache:
      def __init__(self, model):
          self.model = model
          self._length = None
          self._para_ranges = None
          self._heading_tree = None
          self._styles = None  # New
          self._comments = None  # New
  
      def get_styles(self):
          if self._styles is None:
              self._styles = _enumerate_styles(self.model)
          return self._styles
  ```
- **Complexity**: Medium (add cache invalidation on mutations)
- **Benefit**: Faster tool execution in large docs.

#### b) Auto-invalidation
- **Hook**: Invalidate cache in `execute_tool` for mutating tools:
  ```python
  def execute_tool(doc, tool_name, params):
      cache = DocumentCache.get(doc)
      if tool_name in MUTATING_TOOLS:
          cache.invalidate()
      # ... execute tool
  ```
- **Complexity**: Low (add hook)

---

## Complexity Summary

| Improvement | Complexity | Estimate | Risk |
|-------------|------------|----------|------|
| Config unification | Low | 2h | Low |
| Dialog refactoring | Medium | 4h | Low |
| Tool consolidation | High | 8h | Medium |
| Streaming unification | Medium | 3h | Low |
| Error handling | Low | 2h | Low |
| Logging improvements | Low | 2h | Low |
| Image simplification | Medium | 4h | Low |
| Testing | High | 12h | Medium |
| Documentation | Medium | 4h | Low |
| Performance cache | Medium | 4h | Low |

---

## Recommendations

### High Priority (Low Risk, High Impact)
1. **Config unification** (eliminates legacy confusion)
2. **Error handling centralization** (better UX)
3. **Dialog refactoring** (reduces duplication)

### Medium Priority (Moderate Effort)
4. **Streaming unification** (maintainability)
5. **Image simplification** (cleaner code)
6. **Performance cache** (user-visible speedup)

### Low Priority (High Effort or Niche)
7. **Tool consolidation** (future-proofing)
8. **Testing improvements** (long-term stability)
9. **Documentation pruning** (nice-to-have)

---

## Decision Guide

For each improvement, ask:
1. **Does this reduce user-facing bugs?** (Prioritize)
2. **Does this simplify adding new features?** (Prioritize if feature pipeline is full)
3. **Is the complexity justified by the benefit?** (Skip if marginal)

Example:
- **Config unification**: Yes to #1 (avoids confusion), low complexity → **Do it**.
- **Tool consolidation**: Yes to #2, but high complexity → **Defer unless adding Impress soon**.
- **Testing**: Yes to #1, but high complexity → **Do incrementally** (start with format preservation).
