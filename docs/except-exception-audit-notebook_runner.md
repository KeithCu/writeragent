# Audit of `except Exception` in `notebook_runner.py`

This document tracks blanket `except Exception` handlers in `plugin/notebook/notebook_runner.py` to identify where expected exceptions should be narrowed and where masking `DisposedException` hides bugs.

| Line | Try Snippet | Expected Exception(s) | Action | Recommendation (Severity-ranked) |
|---|---|---|---|---|
| 1000 | `cursor.setString(new_line)` | DisposedException, RuntimeException | log | **(Sev 1)** Catch RuntimeException for cursor issues and DisposedException for dead objects. Log/continue. |
| 440 | `text.insertTextContent(insert_at, bookmark, False)` | IllegalArgumentException, DisposedException | log | **(Sev 2)** Re-anchoring bookmarks can fail if 'insert_at' is invalid. Catch IllegalArgumentException and DisposedException. |
| 607 | `sel.setString("")` | RuntimeException, DisposedException | log | **(Sev 3)** Masks DisposedException during bulk output clear. Narrow to RuntimeException. |
| 708 | `cursor.setPropertyValue("ParaStyleName", style)` | UnknownPropertyException, PropertyVetoException, IllegalArgumentException, WrappedTargetException | narrow | **(Sev 4)** Narrow to standard UNO property exceptions. Masking DisposedException here could leave bad state. |
| 1026 | `vc.gotoRange(shape.getAnchor(), False)` | RuntimeException, DisposedException | narrow | **(Sev 5)** View cursor manipulation can throw DisposedException if controller dies. Narrow to RuntimeException. |
| 913 | `ptype = str(portion.getPropertyValue("TextPortionT` | UnknownPropertyException, WrappedTargetException | narrow | **(Sev 6)** Narrow to UnknownPropertyException. DisposedException should propagate. |
| 85 | `value = host_unpack_data(wire)` | ValueError, TypeError, KeyError | narrow | **(Sev 7)** This is payload decoding, likely ValueError/TypeError. Do not catch generic Exception. |
| 137 | `selected = _plain_text(cursor.getString() or "")` | RuntimeException, DisposedException | narrow | **(Sev 8)** Narrow to RuntimeException. If cursor is disposed, DisposedException should surface. |
| 174 | `bookmarks.hasByName(bookmark_name) ... cursor.coll` | NoSuchElementException, RuntimeException | narrow | **(Sev 9)** Narrow to NoSuchElementException (though hasByName checks first) and RuntimeException. |
| 184 | `return bool(doc.getBookmarks().hasByName(bookmark_` | RuntimeException, DisposedException | narrow | **(Sev 10)** Checking bookmarks can fail if doc is dead. Narrow to RuntimeException. |
| 464 | `sel.setString("")` | RuntimeException, DisposedException | narrow | **(Sev 11)** Narrow to RuntimeException. Masking DisposedException here hides dead cursors. |
| 647 | `cursor.goRight(1, False)` | RuntimeException | narrow | **(Sev 12)** Narrow to RuntimeException. |
| 745 | `text.insertControlCharacter(cursor, _PARAGRAPH_BRE` | IllegalArgumentException, RuntimeException | narrow | **(Sev 13)** Narrow to IllegalArgumentException/RuntimeException. Do not mask DisposedException. |
| 762 | `_apply_para_style(cursor, notebook_in)` | RuntimeException, UnknownPropertyException | narrow | **(Sev 14)** Narrow to style/property exceptions. |
| 846 | `cursor.gotoEndOfParagraph(False)` | RuntimeException | narrow | **(Sev 15)** Narrow to RuntimeException. |
| 905 | `enum = para.createEnumeration()` | RuntimeException | narrow | **(Sev 16)** Narrow to RuntimeException. |
| 958 | `text = doc.getText()` | DisposedException | propagate | **(Sev 17)** If document is disposed, it should propagate immediately. Do not swallow it. |
| 1008 | `vc = doc.getCurrentController().getViewCursor()` | DisposedException, RuntimeException | narrow | **(Sev 18)** Controller or Document could be disposed. Narrow to RuntimeException. |
| 979 | `enum = text.createEnumeration()` | RuntimeException | narrow | **(Sev 19)** Narrow to RuntimeException. |
| 1017 | `vc = doc.getCurrentController().getViewCursor()` | DisposedException | narrow | **(Sev 20)** Narrow to RuntimeException. Let DisposedException propagate. |
