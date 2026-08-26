# Audit of `except Exception` in `calc/charts.py`

This document audits the usage of broad `except Exception` handlers in `plugin/calc/charts.py`.
The goal is to identify which handlers are too broad, potentially masking critical errors like UNO `DisposedException` or `RuntimeException`, and recommend how to narrow, propagate, or properly log them.

## Summary

In a UNO extension, catching `Exception` too broadly masks disposed-object bugs (e.g., black menus and hangs, as already found in other areas). Many catches in this file should be narrowed to specific UNO exceptions (like `uno.RuntimeException` or `uno.Exception`) or Python exceptions (like `AttributeError`, `ValueError`, `TypeError`), while ensuring `DisposedException` is allowed to propagate or handled explicitly.

## Findings (Top 20 Severity Ranked)

| Line | Current Catch | Expected Exception(s) | Recommendation (narrow / log / propagate) |
|---|---|---|---|
| 984 | `except Exception as e:` (chart_doc refresh) | `uno.RuntimeException`, `DisposedException` | **Propagate** `DisposedException`. Narrow to `uno.Exception` or log and re-raise. |
| 952 | `except Exception as e:` (fallback object find) | `uno.Exception`, `AttributeError` | **Propagate** `DisposedException`. Narrow to `uno.Exception`. |
| 900 | `except Exception as e3:` (insert DRAW_OLE CLSID) | `uno.Exception`, `IllegalArgumentException` | **Propagate** `DisposedException`. Narrow to `uno.Exception`. |
| 893 | `except Exception:` (insert AT_PARAGRAPH) | `uno.Exception`, `IllegalArgumentException` | **Propagate** `DisposedException`. Narrow to `uno.Exception`. |
| 885 | `except Exception as e:` (insertTextContent) | `uno.Exception`, `IllegalArgumentException` | **Propagate** `DisposedException`. Narrow to `uno.Exception`. |
| 869 | `except Exception as e:` (create/config embedded obj) | `uno.Exception`, `AttributeError` | **Propagate** `DisposedException`. Narrow to `uno.Exception`. |
| 749 | `except Exception as e:` (Chart edit / styling) | `uno.Exception`, `AttributeError` | **Propagate** `DisposedException`. Narrow to `uno.Exception`. |
| 724 | `except Exception as e:` (Chart creation dispatch) | `uno.Exception`, `IllegalArgumentException` | **Propagate** `DisposedException`. Narrow to `uno.Exception`. |
| 1058 | `except Exception:` (remove shape) | `uno.Exception`, `IndexOutOfBoundsException` | **Propagate** `DisposedException`. Narrow to `uno.Exception`. |
| 965 | `except Exception:` (set chart diagram) | `uno.Exception`, `IllegalArgumentException` | **Propagate** `DisposedException`. Narrow to `uno.Exception` and log. |
| 1010 | `except Exception as e:` (shape setSize/setPosition) | `uno.Exception`, `PropertyVetoException` | **Propagate** `DisposedException`. Narrow to `uno.Exception`. |
| 428 | `except Exception as e:` (apply chart data arrays) | `uno.Exception`, `ValueError` | **Narrow** to `uno.Exception` and `ValueError`. |
| 372 | `except Exception:` (set data series colors) | `uno.Exception`, `UnknownPropertyException` | **Propagate** `DisposedException`. Narrow to `uno.Exception`. |
| 338 | `except Exception:` (set background color) | `uno.Exception`, `UnknownPropertyException` | **Propagate** `DisposedException`. Narrow to `uno.Exception`. |
| 837 | `except Exception:` (resolve cursor position) | `uno.Exception`, `AttributeError` | **Narrow** to `uno.Exception` and `AttributeError`. |
| 768 | `except Exception:` (get_sheet by name) | `uno.Exception`, `NoSuchElementException` | **Narrow** to `uno.Exception`. |
| 788 | `except Exception:` (getCellRangeByName) | `uno.Exception`, `NoSuchElementException` | **Narrow** to `uno.Exception`. |
| 452 | `except Exception:` (get all calc chart names) | `uno.Exception`, `NoSuchElementException` | **Narrow** to `uno.Exception`. |
| 472 | `except Exception:` (find calc chart and sheet) | `uno.Exception`, `NoSuchElementException` | **Narrow** to `uno.Exception`. |
| 116 | `except Exception:` (_chart_document_from_host Model/Comp) | `AttributeError` | **Narrow** to `AttributeError`. |
