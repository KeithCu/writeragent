import logging

from typing import cast, Any
from plugin.chatbot.listeners import BaseWindowListener

log = logging.getLogger(__name__)

# Chat sidebar resize/layout tracing is very noisy. Set True to log these steps
# to the debug log even when log_level is DEBUG.
PANEL_RESIZE_VERBOSE_DEBUG = True  # Verbose helper still on for extra detail if logger allows it.


def _resize_debug(msg: str, *args: object) -> None:
    if PANEL_RESIZE_VERBOSE_DEBUG:
        log.debug(msg % args if args else msg)


# Minimum sane widths (in dialog Map AppFont / pixel units) for key controls.
# This protects against GTK/layout glitches where controls end up ~10px wide
# and would otherwise stay that way across resizes.
_MIN_WIDTHS = {
    "response": 80,
    "status": 80,
    "query": 80,
    "query_label": 60,
    "response_label": 60,
    "backend_indicator": 40,
    "send": 40,
    "stop": 40,
    "clear": 40,
    "direct_image_check": 70,
    "web_research_check": 70,
    "model_label": 60,
    # Combos need extra width so the dropdown button stays visible on GTK/VCL themes.
    "model_selector": 120,
    "image_model_selector": 120,
    "base_size_label": 20,
    "base_size_input": 40,
    "aspect_ratio_selector": 80,
}

# Fields that are stretched horizontally on every relayout to fill the current panel width.
# Everything else keeps its XDL snapshot width (subject only to the final right-edge safety clamp).
_STRETCH_CONTROLS = (
    "response",
    "query",
    "status",
    "model_selector",
    "image_model_selector",
    "aspect_ratio_selector",
)


class _PanelResizeListener(BaseWindowListener):
    """Adjusts panel layout on resize.

    Responsibilities (kept deliberately minimal after the 2026-05 simplification):
    - Vertical anchoring of the bottom control cluster toward the window bottom.
    - Stretching the main content fields (response, query, status, model selectors) to fill available width.
    - A final right-edge clamp on all controls as a safety net against H scrollbars.

    Horizontal sizing policy lives exclusively in ChatToolPanel.getHeightForWidth.
    The listener no longer receives or uses parent/deck information.
    """

    def __init__(self, controls):
        self._c = controls  # dict name -> control or None
        self._initial = None  # captured from XDL-loaded pixel positions
        self._in_relayout = False
        self._root_window = None  # Set by owner when attaching, for self-removal on dispose
        self._first_relayout = True  # used to be slightly more conservative on the very first layout after startup

    def disposing(self, Source):
        """Defensive: try to remove ourselves if we were given the root window."""
        if self._root_window and hasattr(self._root_window, "removeWindowListener"):
            try:
                self._root_window.removeWindowListener(self)
            except Exception:
                pass
        self._root_window = None

    def relayout_now(self, win):
        """Run layout on the root panel window (e.g. after programmatic resize).

        Used from ``getHeightForWidth`` when ``windowResized`` does not fire reliably.
        """
        if not win:
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
        r = rEvent.Source.getPosSize()
        _resize_debug("windowResized: W=%d H=%d" % (r.Width, r.Height))
        if self._in_relayout:
            _resize_debug("windowResized: skipped (in_relayout)")
            return
        self.relayout_now(rEvent.Source)

    def _capture_initial(self, win):
        """Snapshot XDL-loaded positions (primarily Y/height for vertical anchoring).

        Horizontal widths are no longer derived from this snapshot for stretching or
        "min client width" forcing — those were major contributors to the H scrollbar.
        We still snapshot for vertical cluster math and as a safe baseline for Y/oh.
        A light min-width floor is kept only for combos (to keep the dropdown glyph visible
        on first narrow GTK paints).
        """
        r = win.getPosSize()
        if r.Width <= 0 or r.Height <= 0:
            return
        _resize_debug("_capture_initial: starting snapshot for win W=%d H=%d" % (r.Width, r.Height))

        info = {"win_h": r.Height, "ctrls": {}}
        resp = self._c.get("response")
        if resp:
            rr = resp.getPosSize()
            info["resp_bottom"] = rr.Y + rr.Height

        bottom_top = None
        bottom_bottom = None
        for name, ctrl in self._c.items():
            if ctrl:
                cr = ctrl.getPosSize()
                # Light floor only for controls whose dropdown arrow must stay visible.
                min_w = _MIN_WIDTHS.get(name)
                cw = cr.Width
                if min_w is not None and cw < min_w and name in ("model_selector", "image_model_selector", "aspect_ratio_selector"):
                    cw = min_w
                info["ctrls"][name] = (cr.X, cr.Y, cw, cr.Height)
                if "resp_bottom" in info and cr.Y >= info["resp_bottom"]:
                    if bottom_top is None or cr.Y < bottom_top:
                        bottom_top = cr.Y
                    bb = cr.Y + cr.Height
                    if bottom_bottom is None or bb > bottom_bottom:
                        bottom_bottom = bb

        if "resp_bottom" in info:
            if bottom_top is not None:
                info["bottom_top"] = bottom_top
                info["bottom_bottom"] = bottom_bottom
                info["gap_below_response"] = max(0, bottom_top - info["resp_bottom"])
            else:
                info["bottom_top"] = cast("int", info["resp_bottom"])
                info["bottom_bottom"] = cast("int", info["resp_bottom"])
                info["gap_below_response"] = 2

        _resize_debug("_capture_initial: win_h=%d resp_bottom=%s bottom_top=%s gap=%s" % (
            info["win_h"], str(info.get("resp_bottom")), str(info.get("bottom_top")), str(info.get("gap_below_response"))))
        self._initial = info

    def _relayout(self, win):
        r = win.getPosSize()
        w, h = r.Width, r.Height
        if w <= 0 or h <= 0:
            return

        if self._initial is None:
            self._capture_initial(win)

        if self._initial is None:
            log.warning("_relayout: no initial state, skip")
            return

        initial = cast("dict[str, Any]", self._initial)

        _resize_debug("_relayout: win W=%d H=%d (have_initial=True)" % (w, h))

        ih = cast("int", initial["win_h"])
        resp_bottom = int(initial.get("resp_bottom", 0))
        gap_below_response = int(initial.get("gap_below_response", 2))
        bottom_top_initial = initial.get("bottom_top")
        bottom_bottom_initial = initial.get("bottom_bottom")
        if isinstance(bottom_top_initial, (int, float)):
            bottom_top_initial = int(bottom_top_initial)
        if isinstance(bottom_bottom_initial, (int, float)):
            bottom_bottom_initial = int(bottom_bottom_initial)

        # Compute where the bottom control group should start for this window height.
        bottom_top_new = None
        if bottom_top_initial is not None and bottom_bottom_initial is not None:
            cluster_height = bottom_bottom_initial - bottom_top_initial
            bottom_margin = 10
            candidate = h - bottom_margin - cluster_height
            min_from_gap = resp_bottom + gap_below_response
            bottom_top_new = max(min_from_gap, candidate)

        # NOTE (2026-05): This layout / stretch / clamping logic (and the 4px margins
        # below) is restored from commit af649476 because it had better real-world
        # horizontal scrollbar behavior in the plain text sidebar. When the user
        # widened the sidebar, the scrollbar would often disappear.
        #
        # Later experiments (tightening margins to 2px and removing the final margin
        # in the safety clamp) made the H scrollbar more persistent / always visible.
        # Future changes here should be made very cautiously.
        #
        # Simple stretch policy (horizontal width policy lives in getHeightForWidth).
        # Only these fields are stretched to fill; everything else stays at XDL snapshot
        # width and is only right-clamped if it would overflow.
        stretch = _STRETCH_CONTROLS

        top_of_bottom = h

        # Smallest possible extension of the first-relayout conservatism:
        # Give bottom-row controls (model_selector, clear, etc.) a bit more
        # right margin on the very first layout when the sidebar restores wide.
        # This is the minimal targeted change to pull max_child_right leftward
        # on startup without affecting runtime widening.
        right_margin = 6 if self._first_relayout else 4

        for name, ctrl in self._c.items():
            if not ctrl or name == "response":
                continue
            orig = initial["ctrls"].get(name)
            if not orig:
                continue
            ox, oy, ow, oh = orig

            if name in stretch:
                new_x = ox
                new_w = max(_MIN_WIDTHS.get(name, 40), w - ox - right_margin)
            else:
                # Buttons, labels, checkboxes, backend_indicator, etc. — keep XDL width, left-anchored.
                new_x = ox
                new_w = ow

            # Final safety: no control may extend past the current window right edge.
            # This is the only remaining "prevent H scrollbar" clamp.
            if new_x + new_w > w - right_margin:
                new_w = max(20, w - new_x - right_margin)

            if oy >= resp_bottom:
                if bottom_top_new is not None and bottom_top_initial is not None:
                    delta = bottom_top_new - bottom_top_initial
                    new_y = oy + delta
                else:
                    new_y = h - (ih - oy)
                cur = ctrl.getPosSize()
                if cur.X != new_x or cur.Y != new_y or cur.Width != new_w or cur.Height != oh:
                    ctrl.setPosSize(new_x, new_y, new_w, oh, 15)
                top_of_bottom = min(top_of_bottom, new_y)
            else:
                cur = ctrl.getPosSize()
                if cur.X != new_x or cur.Y != oy or cur.Width != new_w or cur.Height != oh:
                    ctrl.setPosSize(new_x, oy, new_w, oh, 15)

        # Second pass: stretch response vertically (and a simple width fill for safety).
        resp_orig = initial["ctrls"].get("response")
        resp_ctrl = self._c.get("response")
        if resp_orig and resp_ctrl:
            rx, ry, rw, rh = resp_orig
            gap = gap_below_response if gap_below_response >= 0 else 2
            new_rh = max(30, top_of_bottom - gap - ry)

            # On the very first layout (especially when the sidebar is restored to a
            # previous wide width on app start), be a bit more conservative with the
            # response width so the deck doesn't immediately decide it needs an H
            # scrollbar. Runtime widening behavior is unchanged.
            right_margin = 8 if self._first_relayout else 4
            new_rw = max(_MIN_WIDTHS.get("response", 40), w - rx - right_margin)
            if rx + new_rw > w - right_margin:
                new_rw = max(20, w - rx - right_margin)

            # High-signal one-time diagnostic for the critical first layout on startup.
            # Promoted to INFO so it reliably appears in writeragent_debug.log.
            if self._first_relayout:
                try:
                    log.info("[FIRST RELAYOUT SIZES] root_w=%d response_w=%d (x=%d) clear_right=%d model_sel_right=%d",
                              w,
                              new_rw, rx,
                              (self._c.get("clear").getPosSize().X + self._c.get("clear").getPosSize().Width) if self._c.get("clear") else -1,
                              (self._c.get("model_selector").getPosSize().X + self._c.get("model_selector").getPosSize().Width) if self._c.get("model_selector") else -1)
                except Exception:
                    pass

            self._first_relayout = False

            cur = resp_ctrl.getPosSize()
            if cur.X != rx or cur.Y != ry or cur.Width != new_rw or cur.Height != new_rh:
                resp_ctrl.setPosSize(rx, ry, new_rw, new_rh, 15)
