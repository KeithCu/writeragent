# Audit of `except Exception` in `plugin/chatbot/rich_text_control.py`

This document audits broad `except Exception` handlers in `plugin/chatbot/rich_text_control.py`. In a UNO extension, catching too broadly masks critical `DisposedException` bugs (e.g. black menus and hangs, like 900s). The table below ranks the top 20 most severe instances, recommending specific narrowing for each.

| Line | Current Catch | Expected Exception(s) | Recommendation |
|------|---------------|-----------------------|----------------|
| 830 | `except Exception:` | `DisposedException`, `RuntimeException` | **Narrow**: Top-level async callback (`_deferred_init`). Swallowing all exceptions hides `DisposedException` if the sidebar is closed early. Handle it explicitly. |
| 401 | `except Exception as e:` | `DisposedException`, `RuntimeException` | **Narrow**: `sync_rich_control_bounds` swallows all layout errors and returns `False`. Narrow to expected UNO layout exceptions and `DisposedException`. |
| 907 | `except Exception:` | `DisposedException`, `RuntimeException` | **Narrow/Propagate**: `create_sidebar_rich_text_control` whole-function catch returning `None`. Masking `DisposedException` causes silent UI failures. |
| 1003 | `except Exception:` | `DisposedException`, `RuntimeException` | **Narrow**: `append_text_chunk` whole-function catch. Masking `DisposedException` during AI streaming can cause silent hangs or missed updates. Handle disposed UI explicitly. |
| 654 | `except Exception as e:` | `DisposedException`, `NoSuchElementException` | **Narrow**: Broad catch for dialog model insertions (`_try_dialog_embedded_rich_control`). Narrow to exact UNO exceptions expected like `NoSuchElementException`. |
| 1018 | `except Exception:` | `DisposedException`, `AttributeError` | **Narrow**: `clear_control` whole-function catch masking `DisposedException` or missing model attributes on clear. |
| 1061 | `except Exception:` | `DisposedException`, `AttributeError` | **Narrow**: `truncate_control_from` swallows `DisposedException` and formatting errors during cursor navigation. |
| 1098 | `except Exception:` | `DisposedException`, `RuntimeException` | **Narrow**: `reveal_rich_control_caret` swallows `DisposedException` during focus and idle event processing. |
| 312 | `except Exception:` | `DisposedException`, `RuntimeException` | **Narrow**: `_rich_control_needs_bounds` swallows `DisposedException` on `getPosSize()` and returns `True`, masking object lifecycle issues. |
| 413 | `except Exception as e:` | `DisposedException`, `RuntimeException` | **Narrow**: `refresh_rich_control_peer_layout` idle wait can encounter disposed objects. Catch `DisposedException`. |
| 418 | `except Exception as e:` | `DisposedException`, `RuntimeException` | **Narrow**: `refresh_rich_control_peer_layout` `setPosSize` on potentially disposed peers should catch `DisposedException`. |
| 468 | `except Exception as e:` | `DisposedException`, `RuntimeException` | **Narrow**: `_apply_rich_control_style_defaults` cursor operations can fail on a disposed model. Narrow to `DisposedException`. |
| 600 | `except Exception as e:` | `DisposedException`, `IllegalArgumentException` | **Narrow**: `_create_rich_control_peer` loop attempting multiple peer strategies. Explicitly catch setup failures and `DisposedException`. |
| 605 | `except Exception as e:` | `DisposedException`, `RuntimeException` | **Narrow**: `_create_rich_control_peer` outer setup wrapper. Narrow to specific UNO service creation failures. |
| 291 | `except Exception as e:` | `UnknownPropertyException`, `DisposedException` | **Narrow**: `_apply_rich_control_geometry` model properties update can throw specific UNO reflection exceptions. |
| 298 | `except Exception as e:` | `DisposedException`, `RuntimeException` | **Narrow**: `_apply_rich_control_geometry` `setPosSize` should only throw layout errors or `DisposedException`. |
| 324 | `except Exception as e:` | `NoSuchElementException`, `DisposedException` | **Narrow**: `_reinsert_dialog_embedded_rich_control` catch specific expected errors when removing elements from the dialog model. |
| 494 | `except Exception as e:` | `UnknownPropertyException`, `DisposedException` | **Narrow**: `_set_model_property` property setters should catch specific UNO reflection exceptions. |
| 532 | `except Exception as e:` | `UnknownPropertyException`, `DisposedException` | **Narrow**: `_apply_control_surface_colors` narrow to UNO property reflection errors or `DisposedException`. |
| 920 | `except Exception as e:` | `DisposedException`, `RuntimeException` | **Narrow**: `_apply_char_color_to_cursor_range` text cursor formatting can throw if ranges are invalid or disposed. |
