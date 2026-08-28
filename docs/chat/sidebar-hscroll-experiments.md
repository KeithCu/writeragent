# Chat sidebar H-scroll and type-widen

Deck `ScrolledWindow` H-policy is **AUTOMATIC**. The column width is `getHeightForWidth(nWidth)` (`rContentBox.GetWidth()`). Native weld panels return height only. Our panel is an AWT XDL dialog inside a GTK `ChildFrame`.

**Rule (H8, this branch):** children lay out to `min(window, last deck_hint)`. Before the first `getHeightForWidth`, the first layout width (usually 320) is that column. Never `setPosSize` the ChildFrame. Never `setPosSize` the dialog from `windowResized` (that event can beat `getHeightForWidth` on a widen drag).

Grep:

```
rg "getHeightForWidth|LAYOUT|layout_sanity|cap_width" ~/.config/libreoffice/4/user/config/writeragent_debug.log | tail -50
```

Box is 1x 1280×800. Keith is HiDPI. Do not trust screenshot captions for H-bars. `GDK_SCALE=2` does **not** map XDL AppFont (query stays 49px, not 206).

## What is not the bug

Stick-to-bottom (SelectAll / Hidden) is merged (`4187416d` / PR 490). This file is only: deck H-bar, fill on drag, type-widen after a good drag.

## Native facts

- `Deck.cxx`: H and V policy AUTOMATIC. `tdf#142458` extra width for the **deck** V-scrollbar.
- `DeckLayouter.cxx`: `getHeightForWidth(rContentBox.GetWidth())`. If `getMinimalWidth() > getMaximumWidth()`, max becomes min+100 (splitter freeze).
- GTK `ChildFrame`: hexpand+fill. `Layout()` sizes the AWT child to the allocation.
- `GtkSalFrame::SetPosSize` on SYSTEMCHILD: `gtk_widget_set_size_request` — a **minimum**, not a max. Widgets can still grow the request.
- XDL `ChatPanelDialog.xdl`: `query` and `response` are `multiline` + `vscroll`. `query` height 30 AppFont (Keith HiDPI ≈ 206px; 1x ≈ 49px). `dlg:width="180"` is AppFont, not pixels.

## Experiments

| # | Change | Result |
|---|--------|--------|
| H1 | Cap fill at 800px "frame hint" | Fail. HiDPI column 900–1200 treated as the document frame. Panel stuck at `getMinimalWidth` 320. Gutter + H-bar. |
| H2 | Raise `getMinimalWidth` to HiDPI child extent (~600–1087) | Fail. DeckLayouter sets max to min+100. Splitter barely moves. |
| H3 | Fill `min(nWidth, parent)`; 180 is AppFont; drop 800 cap | Partial. Drag fill looks right when parent tracks. Shrink: ChildFrame request sticks, H-bar until column ≥ stuck width. |
| H4 | `setPosSize` ChildFrame **width only** to `deck_hint` every layout | Partial. Keith 2026-08-27: `parent_after` tracked 641→557 (was stuck at 992). Typing still grew: GTK min, not max. |
| H5 | Clamp HiDPI kids at create (even at 320), before any ChildFrame sync | Partial. Stops `[FIRST LAYOUT] max_child_right=1087` seeding the H-bar. Typing still grew. PR 492 / `685e76c8`. |
| H6 | Drop ChildFrame `setPosSize`. Keep dialog fill + child clamp. `a1dab202` | Fail for type-widen. Log A: 995→1019 still. Shrink H-bar came back (`parent_after` stuck at 1067). |
| H7 | On `windowResized` grow, `setPosSize` dialog back to last `deck_hint` | **Not shipped.** 1x: `windowResized` 465 **before** `getHeightForWidth` 465. Snapping back would fight a widen drag. |
| H8 | Layout children to `min(window, last deck_hint)`. Seed `_viewport_w` from first layout. No dialog `setPosSize` from `windowResized`. | **This branch.** 1x: `cap_width` stops the +4 ratchet; drag still fills after hfw (logs D–E). HiDPI type-widen not reproduced here (query stays 49px). |

## Log A — Keith HiDPI 2026-08-28 11:06 (`a1dab202`)

Drag: `parent_after` stuck at **1067** while `deck_hint`/`root` 1016→995. H-bar vanishes only when the column is wide enough to cover 1067.

```
getHeightForWidth deck_hint=995 parent=1067x2488 current_root=998x2488 eff_W=995
source=windowResized root=995x2488
response_rect ... w=963 ... root=995x2488 max_child_right=991 overflow=no
getHeightForWidth root_after=995x2488 parent_after=1067x2488
```

Type (no `getHeightForWidth`):

```
source=query_text query=963x206@28 has_text=True
source=windowResized root=1019x2488
response_rect ... w=987 ... root=1019x2488 max_child_right=1015 overflow=no
```

Then clear:

```
source=query_text query=987x206@28 has_text=False
source=windowResized root=1043x2488
```

1019 = query X 28 + width 963 + **24** + right margin 4. Query is multiline+vscroll. +24 is a HiDPI V-scrollbar outside the size-request. Filling 1019 put controls past the ~995 viewport.

## Log B — Scrolly 1x 2026-08-28 15:15 (`a1dab202`)

Create: `root=320 max_child_right=316 overflow=no`, then `windowResized` 324, `parent_after` 324 then 328.

Widen: `windowResized` **465 first**, then `getHeightForWidth deck_hint=465`. Parent tracked 465.

Type `hello` / clear: `query=453x49@8`, **no** `windowResized`. Ask box already shows a V-scrollbar when empty at 1x. Type-widen is HiDPI-only on current code.

## Log C — Scrolly `GDK_SCALE=2` 2026-08-28 16:39 (`a1dab202` cache)

`GDK_SCALE=2` was set. AppFont did **not** scale: `query=431x49@8`. Create ratchet without H8:

```
windowResized root=357x485
getHeightForWidth deck_hint=357 parent_after=357
windowResized root=361x577
windowResized root=365x577
windowResized root=369x577
```

No `getHeightForWidth` on those +4 steps. Same "fill GTK's grow" as log A, +4 not +24.

## Log D — Scrolly 1x H8 2026-08-28 16:45 (cap, no first-width seed)

```
FIRST LAYOUT root=320 max_child_right=316
windowResized root=383          # before hfw; filled 383 (viewport still 0)
getHeightForWidth deck_hint=383 parent_after=383
windowResized root=387
cap_width window=387 viewport=383
response_rect root=383 max_child_right=379
```

Widen drag still filled after hfw:

```
windowResized root=460
cap_width window=460 viewport=383
getHeightForWidth deck_hint=460 parent_after=460
response_rect root=460 max_child_right=456
```

## Log E — Scrolly 1x H8 + first-width seed 2026-08-28 16:52

Sidebar restored wide. First layout 320 is the column until hfw:

```
FIRST LAYOUT root=320 max_child_right=316
windowResized root=460
cap_width window=460 viewport=320
response_rect root=320 max_child_right=316
getHeightForWidth deck_hint=460 parent_after=460
response_rect root=460 max_child_right=456
windowResized root=464
cap_width window=464 viewport=460
response_rect root=460 max_child_right=456
```

Kids never followed the +4 GTK bump. Restore-to-wide still filled once the deck spoke.

## What we learned

1. **Column is `getHeightForWidth`, not `windowResized`.** The AWT dialog also resizes when GTK inflates a child (query `vscroll`). Treating that width as the column is how 995 became 1019 became 1043, and how 357 became 361 became 369.
2. **`setPosSize` on GTK is a minimum.** ChildFrame sync (H4) made `parent_after` track on shrink, then typing grew past the min. Snapping the dialog back from `windowResized` (H7) would fight a widen drag, because that event can beat `getHeightForWidth`.
3. **Type-widen is HiDPI.** 1x query is ~49px and already shows a V-scrollbar when empty, so typing does not fire `windowResized`. Keith's query is ~206px; first keystroke adds ~24px chrome outside the size-request.
4. **`GDK_SCALE=2` is not a HiDPI stand-in** for this bug. AppFont stays 1x.
5. **H8 does not shrink a stuck ChildFrame.** Log A `parent=1067` while `deck_hint` 995 is still open. Restacking H4 without a max re-opens type-grow.

## Next

1. **HiDPI click-test of H8** (the only machine that shows log A). After a good drag, type in Ask/instruct. Expect `cap_width window=1019 viewport=995` and kids staying in the column. Same grep as above. 1x cannot prove this.
2. **Stuck ChildFrame on shrink** (`parent_after` 1067). Needs a max, not another size-request min. Do not `setPosSize` the ChildFrame until that exists (native hexpand, or a real max).
3. **Default H-bar leftover** of a few px when parent is 464 and viewport is 460. Kids are in the column; the bar is the GTK request, not child overflow. Same problem as (2) at small delta.
4. **Do not** raise `getMinimalWidth`, treat XDL 180 as pixels, restore the 800px cap, or add a magic 24px scrollbar gutter unless a HiDPI log after H8 still shows controls offscreen.

Code: `plugin/chatbot/panel_resize.py` (`_relayout` cap + first-width seed). Tests in `tests/chatbot/test_panel_resize.py`.
