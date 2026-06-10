import logging

from dataclasses import dataclass

from plugin.chatbot.listeners import BaseWindowListener

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
    "model_selector",
    "image_model_selector",
    "aspect_ratio_selector",
})

_CONTENT_EDGE_CLAMP = frozenset({"status", "query", "model_selector", "image_model_selector", "aspect_ratio_selector"})

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
    response_x, _, response_ow, _ = snapshot["response"]
    bottom_top_new = height - bottom_margin - cluster_height
    cluster_delta = bottom_top_new - bottom_top_initial
    response_h = max(min_response_height, bottom_top_new - response_gap - response_y)
    response_w = max(2 * response_ow // 3, width - response_x - right_margin)

    # Pre-calculate the content right bound used for clamping specific controls (typically clear button's right edge)
    clear_snap = snapshot.get("clear")
    if clear_snap:
        content_right = clear_snap[0] + clear_snap[2]
    else:
        content_right = response_x + response_w
    content_right = min(content_right, width - right_margin)

    layouts: dict[str, ControlRect] = {}
    for name, (ox, oy, ow, oh) in snapshot.items():
        if name == "response":
            continue

        min_w = 2 * ow // 3 if name in _STRETCH_CONTROLS else ow
        if name in _STRETCH_CONTROLS:
            new_w = max(min_w, width - ox - right_margin)
        else:
            new_w = ow

        # Clamp specific controls to the content_right edge
        if name in _CONTENT_EDGE_CLAMP:
            cap = content_right - ox
            if cap > 0:
                new_w = min(new_w, max(min_w, cap))

        # Ensure no control overflows the right margin
        if ox + new_w > width - right_margin:
            new_w = max(20, width - ox - right_margin)

        new_y = oy + cluster_delta if name in _BOTTOM_CLUSTER else oy
        layouts[name] = ControlRect(ox, new_y, new_w, oh)

    layouts["response"] = ControlRect(response_x, response_y, response_w, response_h)
    return layouts


class _PanelResizeListener(BaseWindowListener):
    """Repositions sidebar controls when the panel root is resized.

    Layout policy: XDL snapshot defines control sizes and bottom-band spacing;
    runtime only anchors the bottom band and stretches the transcript vertically.
    """

    def __init__(self, controls):
        self._c = controls
        self._snapshot: dict[str, tuple[int, int, int, int]] | None = None
        self._in_relayout = False
        self._root_window = None
        self._width_negotiated = False
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
        if not self._width_negotiated and self._snapshot is None:
            _resize_debug("relayout_now: deferred until deck width negotiated")
            return
        if self._in_relayout:
            _resize_debug("relayout_now: skipped (in_relayout)")
            return
        try:
            self._in_relayout = True
            self._relayout(win)
        except Exception as e:
            log.error("relayout_now error: %s" % e)
        finally:
            self._in_relayout = False

    def on_window_resized(self, rEvent):
        _resize_debug("windowResized: W=%d H=%d" % (rEvent.Source.getPosSize().Width, rEvent.Source.getPosSize().Height))
        self.relayout_now(rEvent.Source)

    def note_width_negotiated(self):
        self._width_negotiated = True

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
            log.info(
                "[LAYOUT] response_rect x=%d y=%d w=%d h=%d root=%dx%d",
                response.x,
                response.y,
                response.width,
                response.height,
                w,
                h,
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
