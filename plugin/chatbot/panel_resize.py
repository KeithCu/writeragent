import logging

from dataclasses import dataclass
from typing import Any

from plugin.framework.uno_listeners import BaseWindowListener

log = logging.getLogger(__name__)

# Chat sidebar resize/layout tracing is very noisy. Set True to log these steps
# to the debug log even when log_level is DEBUG.
PANEL_RESIZE_VERBOSE_DEBUG = False


def _resize_debug(msg: str, *args: object) -> None:
    if PANEL_RESIZE_VERBOSE_DEBUG:
        log.debug(msg % args if args else msg)


_STRETCH_CONTROLS = frozenset({
    "response",
    "query",
    "status",
    "chat_mode_selector",
    "model_selector",
    "image_model_selector",
    "aspect_ratio_selector",
})

# Completion menu overlays the transcript; do not park it in the bottom band.
_OVERLAY_CONTROLS = frozenset({"slash_popup"})

# ChatPanelDialog.xdl: response top=16 height=110, status top=128 -> gap=2.
_XDL_GAP_BELOW_RESPONSE = 2
# 20 matches python_sidebar.py. 10 clips the Image-mode bottom row
# (base_size_input, aspect_ratio_selector, model_selector) under the deck
# border at 1x.
_BOTTOM_MARGIN = 20
_MIN_RESPONSE_HEIGHT = 30
_RIGHT_MARGIN = 4

# Send/Record, Stop, Clear share one row. XDL widths are AppFont, so at 2x the
# three buttons need ~500px and Clear was clamped to 20px under Stop.
_BUTTON_ROW = ("send", "stop", "clear")

# Controls below the chat transcript — anchored as one block toward the panel bottom.
_BOTTOM_CLUSTER = frozenset({
    "status",
    "query_label",
    "query",
    "send",
    "stop",
    "clear",
    "chk_voice",
    "chat_mode_selector",
    "model_label",
    "model_selector",
    "image_model_selector",
    "base_size_label",
    "base_size_input",
    "aspect_ratio_selector",
})


@dataclass(frozen=True)
class ControlRect:
    x: int
    y: int
    width: int
    height: int


def _cluster_metrics(snapshot: dict[str, tuple[int, int, int, int]]) -> tuple[int, int, int]:
    """Return (bottom_top_y, cluster_height, response_top_y) from the XDL snapshot."""
    bottoms = [snapshot[n] for n in _BOTTOM_CLUSTER if n in snapshot]
    if not bottoms or "response" not in snapshot:
        response_y = snapshot.get("response", (0, 16, 0, 0))[1]
        return response_y + 112, 0, response_y
    bottom_top = min(rect[1] for rect in bottoms)
    bottom_bottom = max(rect[1] + rect[3] for rect in bottoms)
    return bottom_top, bottom_bottom - bottom_top, snapshot["response"][1]


def column_right_margin(snapshot: dict[str, tuple[int, int, int, int]], right_margin: int = _RIGHT_MARGIN) -> int:
    """Right inset of the column, as wide as the left inset.

    Mirror the left inset (already DPI-mapped) plus a pixel of slack per
    AppFont unit of rounding, so the request fits the viewport. A fixed 4px
    right margin against a 4 AppFont left inset (7px at 1x, 13px at 2x)
    makes the panel wider than the deck (the window asks for the children's
    extent plus the left inset) and the deck shows a horizontal scrollbar.
    """
    left = snapshot.get("response", (right_margin, 0, 0, 0))[0]
    return max(right_margin, left + max(1, left // 4))


def compute_chat_panel_layout(
    width: int,
    height: int,
    snapshot: dict[str, tuple[int, int, int, int]],
    *,
    bottom_margin: int = _BOTTOM_MARGIN,
    response_gap: int = _XDL_GAP_BELOW_RESPONSE,
    min_response_height: int = _MIN_RESPONSE_HEIGHT,
    right_margin: int | None = None,
    preferred: dict[str, tuple[int, int]] | None = None,
) -> dict[str, ControlRect]:
    """Pure layout: bottom band anchored near the bottom, transcript fills the rest.

    *preferred* holds measured (width, height) of controls whose text sets their
    size (``query_label``); it is optional so the pure layout still works
    without a peer.
    """
    if width <= 0 or height <= 0 or not snapshot or "response" not in snapshot:
        return {}
    if right_margin is None:
        right_margin = column_right_margin(snapshot)

    bottom_top_initial, cluster_height, response_y = _cluster_metrics(snapshot)
    response_x, _oy, _ow, _oh = snapshot["response"]
    bottom_top_new = height - bottom_margin - cluster_height
    cluster_delta = bottom_top_new - bottom_top_initial
    response_h = max(min_response_height, bottom_top_new - response_gap - response_y)
    response_w = max(20, width - response_x - right_margin)
    response_x, response_w = _clamp_to_column(response_x, response_w, width, right_margin)

    # Fill the column. Shrinking width-only left HiDPI Clear/indicator X past
    # the viewport (deck H-bar). Move X too so nothing extends past the column.
    layouts: dict[str, ControlRect] = {}
    for name, (ox, oy, ow, oh) in snapshot.items():
        if name == "response":
            continue

        if name in _OVERLAY_CONTROLS:
            continue

        if name in _STRETCH_CONTROLS:
            new_w = max(20, width - ox - right_margin)
        else:
            new_w = ow

        ox, new_w = _clamp_to_column(ox, new_w, width, right_margin)
        new_y = oy + cluster_delta if name in _BOTTOM_CLUSTER else oy
        layouts[name] = ControlRect(ox, new_y, new_w, oh)

    layouts["response"] = ControlRect(response_x, response_y, response_w, response_h)
    _share_button_row(layouts, snapshot, width, right_margin)
    _fit_base_size_row(layouts, snapshot, width, right_margin, (preferred or {}).get("base_size_input"))
    label_pref = (preferred or {}).get("query_label")
    if label_pref and label_pref[0] > 0:
        _place_voice_checkbox_after_label(layouts, snapshot, width, right_margin, label_pref[0])
    else:
        # No measured label width: the Ask label stays at its XDL X and the TTS
        # checkbox is centered.
        _center_voice_checkbox(layouts, snapshot, width, right_margin)
    return layouts


def _share_button_row(
    layouts: dict[str, ControlRect],
    snapshot: dict[str, tuple[int, int, int, int]],
    width: int,
    right_margin: int,
) -> None:
    """Split the row from Send's left edge to the column edge between the buttons.

    Equal shares with the XDL gap fill the column at any DPI, so every
    button stays visible and its right edge matches the stretch controls.
    XDL widths (AppFont) need ~500px at 2x; in a ~300px column Stop shrinks
    and Clear clamps to a sliver under it.
    """
    names = [n for n in _BUTTON_ROW if n in layouts and n in snapshot]
    if len(names) < 2:
        return
    x0 = snapshot[names[0]][0]
    gaps = [
        snapshot[b][0] - (snapshot[a][0] + snapshot[a][2])
        for a, b in zip(names, names[1:])
    ]
    gap = max(1, min(gaps))
    avail = width - right_margin - x0
    each = (avail - gap * (len(names) - 1)) // len(names)
    if each < 20:
        return
    x = x0
    for i, name in enumerate(names):
        rect = layouts[name]
        w = each if i < len(names) - 1 else (width - right_margin) - x
        layouts[name] = ControlRect(x, rect.y, w, rect.height)
        x += w + gap


def _fit_base_size_row(
    layouts: dict[str, ControlRect],
    snapshot: dict[str, tuple[int, int, int, int]],
    width: int,
    right_margin: int,
    base_pref: tuple[int, int] | None,
) -> None:
    """Image mode: size box at its measured width, aspect box takes the rest.

    The peer's preferred width covers the text and the dropdown button
    at any DPI; the XDL width stays the upper bound and the XDL gap is kept.
    A fixed 40 AppFont (131px at 2x) for "1024" leaves the aspect box ~60px,
    which reads "Squa".
    """
    if not base_pref or base_pref[0] <= 0:
        return
    base = layouts.get("base_size_input")
    aspect = layouts.get("aspect_ratio_selector")
    if base is None or aspect is None or "base_size_input" not in snapshot or "aspect_ratio_selector" not in snapshot:
        return
    bx, _by, bw, _bh = snapshot["base_size_input"]
    gap = max(1, snapshot["aspect_ratio_selector"][0] - (bx + bw))
    new_bw = min(bw, base_pref[0])
    layouts["base_size_input"] = ControlRect(base.x, base.y, new_bw, base.height)
    ax = base.x + new_bw + gap
    aw = (width - right_margin) - ax
    if aw >= 20:
        layouts["aspect_ratio_selector"] = ControlRect(ax, aspect.y, aw, aspect.height)


def _place_voice_checkbox_after_label(
    layouts: dict[str, ControlRect],
    snapshot: dict[str, tuple[int, int, int, int]],
    width: int,
    right_margin: int,
    label_text_w: int,
) -> None:
    """Put the TTS checkbox right after the Ask label's measured text.

    The label keeps its text width (measured from the peer, so DPI and
    translations are covered) and the checkbox follows it. Only when the
    column is too narrow for both does the label give up width. Centering
    the checkbox cuts the label ("Ask / ins" at 2x) or parks the box far
    from its label in a wide column.
    """
    voice = layouts.get("chk_voice")
    label = layouts.get("query_label")
    if voice is None or label is None or "chk_voice" not in snapshot:
        _center_voice_checkbox(layouts, snapshot, width, right_margin)
        return
    max_right = width - right_margin
    voice_w = min(snapshot["chk_voice"][2], max(1, max_right))
    gap = max(2, voice.height // 4)
    x = label.x + label_text_w + gap
    x = max(0, min(x, max_right - voice_w))
    room = max(1, x - gap - label.x)
    layouts["query_label"] = ControlRect(label.x, label.y, room, label.height)
    layouts["chk_voice"] = ControlRect(x, voice.y, voice_w, voice.height)


def fit_snapshot_min_heights(
    snapshot: dict[str, tuple[int, int, int, int]],
    min_heights: dict[str, int],
) -> dict[str, tuple[int, int, int, int]]:
    """Grow controls to their measured minimum height; push the band below down.

    Take the peer's minimum height and move the rows under it down by
    the same amount; the transcript gives up the space. The status box is
    10 AppFont (16px at 1x). An edit field needs the font height plus its
    frame (21px at 1x, 33px at 2x). AppFont scales the box with the font;
    the frame does not, so at 1x "Ready" touches the top border.
    """
    out = dict(snapshot)
    for name, min_h in min_heights.items():
        rect = out.get(name)
        if rect is None or min_h <= rect[3]:
            continue
        x, y, w, h = rect
        dh = min_h - h
        bottom = y + h
        for other, (ox, oy, ow, oh) in list(out.items()):
            if other != name and other in _BOTTOM_CLUSTER and oy >= bottom:
                out[other] = (ox, oy + dh, ow, oh)
        out[name] = (x, y, w, min_h)
    return out


def _center_voice_checkbox(
    layouts: dict[str, ControlRect],
    snapshot: dict[str, tuple[int, int, int, int]],
    width: int,
    right_margin: int,
) -> None:
    """Center the TTS checkbox horizontally. Leave its Y and the label's X alone.

    The label keeps the left edge from the snapshot and is cut off just before
    the checkbox. A fixed text width clips translations, and a column-width
    label paints over the checkbox. The gap follows the checkbox height, which
    is already in device pixels after AppFont mapping.
    """
    voice = layouts.get("chk_voice")
    voice_snap = snapshot.get("chk_voice")
    if voice is None or voice_snap is None:
        return

    _vx, _vy, vw, _vh = voice_snap
    max_right = width - right_margin
    if max_right <= 0:
        return

    voice_w = min(vw, max_right)
    x = max(0, (width - voice_w) // 2)
    if x + voice_w > max_right:
        x = max(0, max_right - voice_w)
    gap = max(1, voice.height // 2)
    label = layouts.get("query_label")
    if label is not None:
        room = x - gap - label.x
        if room < 1:
            room = 1
            x = min(max_right - voice_w, label.x + room + gap)
            x = max(0, x)
        layouts["query_label"] = ControlRect(label.x, label.y, room, label.height)
    layouts["chk_voice"] = ControlRect(x, voice.y, voice_w, voice.height)


def _clamp_to_column(x: int, w: int, width: int, right_margin: int) -> tuple[int, int]:
    """Keep a control inside the column. Shrink width, then slide X if needed."""
    max_right = width - right_margin
    if max_right <= 0:
        return 0, max(20, width)
    if x + w > max_right:
        w = max(20, max_right - x)
    if x + w > max_right:
        w = min(w, max(20, max_right))
        x = max(0, max_right - w)
    if x < 0:
        x = 0
    return x, w


def _measure(ctrl: Any, method: str) -> tuple[int, int] | None:
    """(width, height) from XLayoutConstrains on a control, or None."""
    fn = getattr(ctrl, method, None) if ctrl is not None else None
    if not callable(fn):
        return None
    try:
        size: Any = fn()
        w, h = size.Width, size.Height
    except Exception:
        return None
    if not isinstance(w, int) or not isinstance(h, int) or w <= 0 or h <= 0:
        return None
    return w, h


class _PanelResizeListener(BaseWindowListener):  # pyright: ignore[reportUnusedClass]  # constructed from panel_wiring; covered by tests
    """Repositions sidebar controls when the panel root is resized.

    Layout policy: XDL snapshot defines control sizes and bottom-band spacing;
    runtime anchors the bottom band and stretches the transcript to fill the column.
    """

    _c: dict[str, Any]
    _on_dispose: Any
    _in_relayout: bool
    _root_window: Any
    _parent_window: Any
    _width_negotiated: bool
    _viewport_w: int
    _restore_focus: Any

    def __init__(self, controls: dict[str, Any], on_dispose: Any = None, restore_focus: Any = None) -> None:
        self._c = controls
        self._on_dispose = on_dispose
        self._restore_focus = restore_focus
        self._snapshot: dict[str, tuple[int, int, int, int]] | None = None
        self._preferred: dict[str, tuple[int, int]] = {}
        self._in_relayout = False
        self._root_window = None
        self._parent_window = None
        self._width_negotiated = False
        self._viewport_w = 0
        self._last_response_rect: tuple[int, int, int, int] | None = None

    @property
    def last_response_rect(self) -> tuple[int, int, int, int] | None:
        return self._last_response_rect

    def disposing(self, Source: Any) -> None:
        # VCL calls this on deck close. ChatPanelElement.disposing is not
        # invoked then, so the in-flight send stayed running. Do not
        # removeWindowListener here: this listener is already disposing.
        callback = self._on_dispose
        self._on_dispose = None
        self._root_window = None
        if callable(callback):
            try:
                callback()
            except Exception:
                log.exception("sidebar window dispose callback failed")

    def relayout_now(self, win: Any) -> None:
        if not win:
            return
        # Do not wait for deck negotiation. Keith create-time: root=320 with
        # max_child_right=1087 seeded the H-bar until the first widen.
        if self._in_relayout:
            _resize_debug("relayout_now: skipped (in_relayout)")
            return
        try:
            self._in_relayout = True
            self._relayout(win)
        except Exception:
            log.exception("relayout_now failed")
        finally:
            self._in_relayout = False

    def on_window_resized(self, rEvent: Any) -> None:
        r = rEvent.Source.getPosSize()
        log.info("[LAYOUT] source=windowResized root=%dx%d", r.Width, r.Height)
        # Do not setPosSize the dialog here. windowResized can beat
        # getHeightForWidth on a widen drag (1x: 465 then hfw 465); snapping
        # back to last deck_hint would fight the splitter.
        self.relayout_now(rEvent.Source)

    def note_width_negotiated(self, viewport_w: int = 0) -> None:
        self._width_negotiated = True
        if viewport_w > 0:
            self._viewport_w = int(viewport_w)

    def _capture_snapshot(self, win: Any) -> None:
        r = win.getPosSize()
        if r.Width <= 0 or r.Height <= 0:
            return
        _resize_debug("_capture_snapshot: win W=%d H=%d" % (r.Width, r.Height))

        snapshot: dict[str, tuple[int, int, int, int]] = {}
        for name, ctrl in self._c.items():
            if not ctrl:
                continue
            cr = ctrl.getPosSize()
            snapshot[name] = (int(cr.X), int(cr.Y), int(cr.Width), int(cr.Height))

        if "response" not in snapshot:
            return
        min_heights: dict[str, int] = {}
        status = self._c.get("status")
        min_size = _measure(status, "getMinimumSize")
        if min_size is not None:
            min_heights["status"] = min_size[1]
        for name in ("query_label", "base_size_input"):
            size = _measure(self._c.get(name), "getPreferredSize")
            if size is not None:
                self._preferred[name] = size
        self._snapshot = fit_snapshot_min_heights(snapshot, min_heights)
        bottom_top, cluster_h, _response_y = _cluster_metrics(snapshot)
        _resize_debug(
            "_capture_snapshot: bottom_top=%d cluster_h=%d controls=%d",
            bottom_top,
            cluster_h,
            len(snapshot),
        )

    def _apply_rect(self, ctrl: Any, rect: ControlRect) -> None:
        cur = ctrl.getPosSize()
        if (
            cur.X != rect.x
            or cur.Y != rect.y
            or cur.Width != rect.width
            or cur.Height != rect.height
        ):
            ctrl.setPosSize(rect.x, rect.y, rect.width, rect.height, 15)

    def _relayout(self, win: Any) -> None:
        r = win.getPosSize()
        w, h = int(r.Width), int(r.Height)
        if w <= 0 or h <= 0:
            return
        # Cap h against the parent window height (getPosSize().Height) and
        # 3000px. In Calc's sidebar, moving bottom controls down resizes the
        # container, which fires windowResized with a larger height and
        # repeats (test_e12).
        if self._parent_window is not None:
            try:
                pr = self._parent_window.getPosSize()
                if pr.Height > 0 and h > pr.Height:
                    h = int(pr.Height)
            except Exception:
                pass
        if h > 3000:
            h = 3000
        # Column is last getHeightForWidth. Before the first hfw, the first
        # layout width (320) is the column so a GTK jump (320→383) is not filled.
        # A windowResized grow without a new deck_hint is GTK, not a drag.
        # Do not setPosSize the dialog here; that can beat hfw on a widen.
        if self._viewport_w <= 0:
            self._viewport_w = w
        elif w > self._viewport_w:
            log.info("[LAYOUT] cap_width window=%s viewport=%s", w, self._viewport_w)
            w = self._viewport_w

        if self._snapshot is None:
            self._capture_snapshot(win)
        snapshot = self._snapshot
        if not snapshot:
            log.warning("_relayout: no snapshot, skip")
            return

        layouts = compute_chat_panel_layout(w, h, snapshot, preferred=self._preferred)
        if not layouts:
            return

        for name, rect in layouts.items():
            ctrl = self._c.get(name)
            if ctrl is not None:
                self._apply_rect(ctrl, rect)

        response = layouts.get("response")
        if response is not None:
            self._last_response_rect = (response.x, response.y, response.width, response.height)
            max_right = 0
            for rect in layouts.values():
                max_right = max(max_right, rect.x + rect.width)
            log.info(
                "[LAYOUT] response_rect x=%d y=%d w=%d h=%d root=%dx%d max_child_right=%d overflow=%s",
                response.x,
                response.y,
                response.width,
                response.height,
                w,
                h,
                max_right,
                "YES" if max_right > w - 2 else "no",
            )
            rich = self._c.get("response_rich")
            if rich is not None:
                try:
                    from plugin.chatbot.rich_text_control import log_rich_scroll, sync_rich_control_bounds

                    log_rich_scroll("relayout", control=rich, root_w=w, root_h=h)
                    rich_out = [rich]
                    sync_rich_control_bounds(
                        rich,
                        win,
                        self._c.get("response"),
                        placeholder_rect=self._last_response_rect,
                        control_out=rich_out,
                        restore_focus=self._restore_focus,
                    )
                    rich = rich_out[0]
                    self._c["response_rich"] = rich
                except Exception as e:
                    log.debug("response_rich sync after relayout: %s", e)
