import logging

from dataclasses import dataclass

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
_BOTTOM_MARGIN = 10
_MIN_RESPONSE_HEIGHT = 30
_RIGHT_MARGIN = 4

# Controls below the chat transcript — anchored as one block toward the panel bottom.
_BOTTOM_CLUSTER = frozenset({
    "status",
    "query_label",
    "query",
    "send",
    "stop",
    "clear",
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


def compute_chat_panel_layout(
    width: int,
    height: int,
    snapshot: dict[str, tuple[int, int, int, int]],
    *,
    bottom_margin: int = _BOTTOM_MARGIN,
    response_gap: int = _XDL_GAP_BELOW_RESPONSE,
    min_response_height: int = _MIN_RESPONSE_HEIGHT,
    right_margin: int = _RIGHT_MARGIN,
) -> dict[str, ControlRect]:
    """Pure layout: bottom band anchored near the bottom, transcript fills the rest."""
    if width <= 0 or height <= 0 or not snapshot or "response" not in snapshot:
        return {}

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
    return layouts


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


class _PanelResizeListener(BaseWindowListener):  # pyright: ignore[reportUnusedClass]  # constructed from panel_wiring; covered by tests
    """Repositions sidebar controls when the panel root is resized.

    Layout policy: XDL snapshot defines control sizes and bottom-band spacing;
    runtime anchors the bottom band and stretches the transcript to fill the column.
    """

    def __init__(self, controls):
        self._c = controls
        self._snapshot: dict[str, tuple[int, int, int, int]] | None = None
        self._in_relayout = False
        self._root_window = None
        self._parent_window = None
        self._width_negotiated = False
        self._viewport_w = 0
        self._last_response_rect = None

    @property
    def last_response_rect(self):
        return self._last_response_rect

    def disposing(self, Source):
        if self._root_window and hasattr(self._root_window, "removeWindowListener"):
            try:
                self._root_window.removeWindowListener(self)
            except Exception:
                pass
        self._root_window = None

    def relayout_now(self, win):
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

    def on_window_resized(self, rEvent):
        r = rEvent.Source.getPosSize()
        log.info("[LAYOUT] source=windowResized root=%dx%d", r.Width, r.Height)
        # Do not setPosSize the dialog here. windowResized can beat
        # getHeightForWidth on a widen drag (1x: 465 then hfw 465); snapping
        # back to last deck_hint would fight the splitter.
        self.relayout_now(rEvent.Source)

    def note_width_negotiated(self, viewport_w: int = 0):
        self._width_negotiated = True
        if viewport_w > 0:
            self._viewport_w = int(viewport_w)

    def _capture_snapshot(self, win):
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
        self._snapshot = snapshot
        bottom_top, cluster_h, _response_y = _cluster_metrics(snapshot)
        _resize_debug(
            "_capture_snapshot: bottom_top=%d cluster_h=%d controls=%d",
            bottom_top,
            cluster_h,
            len(snapshot),
        )

    def _apply_rect(self, ctrl, rect: ControlRect) -> None:
        cur = ctrl.getPosSize()
        if (
            cur.X != rect.x
            or cur.Y != rect.y
            or cur.Width != rect.width
            or cur.Height != rect.height
        ):
            ctrl.setPosSize(rect.x, rect.y, rect.width, rect.height, 15)

    def _relayout(self, win):
        r = win.getPosSize()
        w, h = int(r.Width), int(r.Height)
        if w <= 0 or h <= 0:
            return
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

        layouts = compute_chat_panel_layout(w, h, snapshot)
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
                    )
                    rich = rich_out[0]
                    self._c["response_rich"] = rich
                except Exception as e:
                    log.debug("response_rich sync after relayout: %s", e)
