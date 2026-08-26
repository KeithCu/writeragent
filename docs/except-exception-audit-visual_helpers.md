# Audit of except Exception in `visual_helpers.py`

| Line | Current Catch | Expected Exception(s) | Recommendation |
|---|---|---|---|
| 284 | `except Exception:` | `AttributeError`, `RuntimeException` | Narrow to expected exceptions, log, and propagate `DisposedException`. Swallows disposed errors during controller access. |
| 291 | `except Exception:` | `AttributeError`, `RuntimeException` | Narrow to expected exceptions, log, and propagate `DisposedException`. Swallows disposed errors during selection access. |
| 296 | `except Exception as ex:` | `AttributeError`, `RuntimeException` | Log and propagate `DisposedException`. Hiding `DisposedException` when accessing the controller masks object lifecycle bugs. |
| 320 | `except Exception as ex:` | `IndexOutOfBoundsException`, `RuntimeException` | Log, narrow exceptions, and propagate `DisposedException`. Narrow index bounds checks. |
| 438 | `except Exception as ex:` | `IndexOutOfBoundsException`, `RuntimeException` | Log, narrow exceptions, and propagate `DisposedException`. Accessing selection on a closed document should fail loudly, not return `[]`. |
| 452 | `except Exception:` | `AttributeError`, `RuntimeException` | Narrow to expected exceptions, log, and propagate `DisposedException`. Controller access shouldn't swallow disposed state. |
| 524 | `except Exception as ex:` | `IndexOutOfBoundsException`, `RuntimeException` | Log, narrow exceptions, and propagate `DisposedException`. Swallows critical object disposed errors during draw page access. |
| 550 | `except Exception as ex:` | `IndexOutOfBoundsException`, `RuntimeException` | Log, narrow exceptions, and propagate `DisposedException`. Draw page shape iteration shouldn't silently swallow lifecycle bugs. |
| 561 | `except Exception as ex:` | `NoSuchElementException`, `RuntimeException` | Log, narrow exceptions, and propagate `DisposedException`. Hides disposed writer models. |
| 595 | `except Exception as ex:` | `IndexOutOfBoundsException`, `RuntimeException` | Log, narrow exceptions, and propagate `DisposedException`. Removing from a disposed page shouldn't just log and return `False`. |
| 619 | `except Exception as ex:` | `UnknownPropertyException`, `RuntimeException` | Log, narrow exceptions, and propagate `DisposedException`. Narrow property access fallback. |
| 48 | `except Exception:` | `RuntimeException` | Narrow to expected exceptions, log, and propagate `DisposedException`. Service support check shouldn't hide disposed documents. |
| 90 | `except Exception:` | `RuntimeException` | Narrow to expected exceptions, log, and propagate `DisposedException`. |
| 101 | `except Exception as ex:` | `UnknownPropertyException`, `IllegalArgumentException` | Log, narrow exceptions, and propagate `DisposedException`. |
| 248 | `except Exception as ex:` | `IllegalArgumentException`, `RuntimeException` | Log, narrow exceptions, and propagate `DisposedException`. Broad catch masks bugs in dynamic method calls. |
| 256 | `except Exception:` | `UnknownPropertyException`, `RuntimeException` | Narrow to expected exceptions, log, and propagate `DisposedException`. |
| 271 | `except Exception:` | `RuntimeException` | Narrow to expected exceptions, log, and propagate `DisposedException`. Service checks shouldn't swallow disposed states. |
| 333 | `except Exception:` | `RuntimeException` | Narrow to expected exceptions, log, and propagate `DisposedException`. Anchor extraction shouldn't swallow lifecycle bugs. |
| 350 | `except Exception:` | `IllegalArgumentException`, `RuntimeException` | Narrow to expected exceptions, log, and propagate `DisposedException`. Comparison region errors. |
| 364 | `except Exception:` | `RuntimeException` | Narrow to expected exceptions, log, and propagate `DisposedException`. Range text access shouldn't hide dead objects. |
