# Audit of `except Exception` in `plugin/draw/shapes.py`

This document audits blanket `except Exception` handlers in `plugin/draw/shapes.py`. Catching too broadly can mask disposed-object bugs (`DisposedException`) leading to black menus and hangs in the UNO extension.

| Line | Current Catch | Expected Exception(s) | Recommendation (Severity-Ranked) |
|---|---|---|---|
| 727 | `except Exception as e:` | `IndexOutOfBoundsException, WrappedTargetException, DisposedException` | Narrow to IndexOutOfBoundsException. Propagate DisposedException instead of masking as tool error. |
| 781 | `except Exception as e:` | `IndexOutOfBoundsException, WrappedTargetException, DisposedException` | Narrow to IndexOutOfBoundsException. Propagate DisposedException instead of masking as tool error. |
| 799 | `except Exception as e:` | `UnknownPropertyException, IllegalArgumentException, DisposedException` | Narrow to UNO beans exceptions. Propagate DisposedException instead of masking as tool error. |
| 838 | `except Exception as e:` | `IndexOutOfBoundsException, DisposedException, IllegalArgumentException` | Narrow to expected UNO exceptions. Propagate DisposedException instead of masking as tool error. |
| 643 | `except Exception:` | `IndexOutOfBoundsException, WrappedTargetException, DisposedException` | Narrow to IndexOutOfBoundsException. Propagate DisposedException instead of masking as tool error. |
| 873 | `except Exception:` | `IndexOutOfBoundsException, WrappedTargetException, DisposedException` | Narrow to IndexOutOfBoundsException. Propagate DisposedException instead of masking as error string. |
| 376 | `except Exception:` | `DisposedException` | Remove or narrow catch. Propagate DisposedException instead of masking as tool error. |
| 923 | `except Exception as exc:` | `IndexOutOfBoundsException, DisposedException, ValueError` | Narrow to specific UNO and ValueError exceptions. Propagate DisposedException. |
| 973 | `except Exception as exc:` | `IndexOutOfBoundsException, DisposedException, ValueError` | Narrow to specific UNO and ValueError exceptions. Propagate DisposedException. |
| 482 | `except Exception:` | `UnknownPropertyException, IllegalArgumentException, DisposedException` | Narrow to property exceptions. Propagate DisposedException. Silent swallow hides formatting failures. |
| 491 | `except Exception:` | `UnknownPropertyException, IllegalArgumentException, DisposedException` | Narrow to property exceptions. Propagate DisposedException. Silent swallow hides formatting failures. |
| 509 | `except Exception:` | `UnknownPropertyException, IllegalArgumentException, DisposedException` | Narrow to property exceptions. Propagate DisposedException. Silent swallow hides formatting failures. |
| 518 | `except Exception:` | `UnknownPropertyException, IllegalArgumentException, DisposedException` | Narrow to property exceptions. Propagate DisposedException. Silent swallow hides formatting failures. |
| 525 | `except Exception:` | `UnknownPropertyException, IllegalArgumentException, DisposedException` | Narrow to property exceptions. Propagate DisposedException. Silent swallow hides formatting failures. |
| 537 | `except Exception:` | `UnknownPropertyException, IllegalArgumentException, DisposedException` | Narrow to property exceptions. Propagate DisposedException. Silent swallow hides formatting failures. |
| 554 | `except Exception:` | `UnknownPropertyException, IllegalArgumentException, DisposedException` | Narrow to property exceptions. Propagate DisposedException. Silent swallow hides formatting failures. |
| 346 | `except Exception as ex:` | `UnknownPropertyException, IllegalArgumentException, DisposedException` | Narrow to UNO beans exceptions. Propagate DisposedException instead of returning a generic error string. |
| 69 | `except Exception as ex:` | `UnknownPropertyException, IllegalArgumentException, DisposedException` | Narrow to UNO beans exceptions. Propagate DisposedException. Logging as a warning masks severe state issues. |
| 194 | `except Exception as ex:` | `UnknownPropertyException, IllegalArgumentException, DisposedException` | Narrow to UNO beans exceptions. Propagate DisposedException. Logging as a warning masks severe state issues. |
| 206 | `except Exception as ex:` | `PropertyVetoException, DisposedException` | Narrow to PropertyVetoException. Propagate DisposedException. Logging as a warning masks severe state issues. |
