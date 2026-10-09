# get_page_objects: leave nested XText, then lock

Physical page of a range is view-cursor `getPage()` only — no UNO page-of-range API (probed).

The sequence is encapsulated in `with_view_cursor_left_body_locked` (`plugin/doc/text_helpers.py`), shared by `get_page_objects` scans, paragraph page resolution (`get_page_for_paragraph`), and document page counting (`get_page_count`):

**Leave body first.** `lockControllers` while the view cursor sits in a table cell makes `gotoRange`/`getPage` fail silently (Cneg: `tables=[]`). Hop unlocked to `doc.getText().getStart()`, then lock. `jumpToPage` is a no-op on the same page (the cell).

**Lock during the scan/action.** Headed visarea: never-lock C hits Y=37017 on page-2 hops; leave+lock A does not. Flicker needs lock during multi-object scan.

**Unlock before restore.** `gotoRange` into a nested cell fails while locked. Save/restore via `clone_text_range` (`vc.getText()`), not body `XText`. (Body `createTextCursorByRange` raises `RuntimeException: End of content node doesn't have the proper start node` on ranges inside nested cells/frames).

**Unlock-retry** only for table/frame/graphic anchors: a locked `getAnchor()` hop can leave `getPage()==0` (stale layout). Ordinary paragraph ranges treat 0 as "not on this page" (no unlock churn). Live probe: retries fired on tables only; frames/graphics/paragraph shapes returned a real page while locked.

**No view hop** when `AnchorType` is `AT_PAGE` (`AnchorPageNo` on shapes, frames, graphics). Do not use `jumpToEndOfPage` + body `createTextCursorByRange` for shapes (`RuntimeException` when page end sits in a cell/frame).
