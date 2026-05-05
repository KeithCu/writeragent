import logging

from typing import cast, Any
from plugin.framework.listeners import BaseWindowListener

log = logging.getLogger(__name__)

# Chat sidebar resize/layout tracing is very noisy. Set True to log these steps
# to the debug log even when log_level is DEBUG.
PANEL_RESIZE_VERBOSE_DEBUG = False


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


class _PanelResizeListener(BaseWindowListener):
    """Adjusts panel layout on resize. Reads control sizes/gaps from the XDL;
    only the response area height changes to fill available space."""

    def __init__(self, controls, parent_window=None, deck_w_getter=None):
        self._c = controls  # dict name -> control or None
        self._initial = None  # captured from XDL-loaded pixel positions
        self._in_relayout = False
        # Sidebar content area; root window width must match this (not LO's transient hints).
        self._parent_window = parent_window
        # Callable -> last deck hint from getHeightForWidth (with parent: clamp vs fill).
        self._deck_w_getter = deck_w_getter

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
        """Snapshot XDL-loaded pixel positions/sizes of every control."""
        r = win.getPosSize()
        if r.Width <= 0 or r.Height <= 0:
            return
        _resize_debug("_capture_initial: starting snapshot for win W=%d H=%d" % (r.Width, r.Height))
        info = {"win_w": r.Width, "win_h": r.Height, "ctrls": {}}
        resp = self._c.get("response")
        if resp:
            rr = resp.getPosSize()
            info["resp_bottom"] = rr.Y + rr.Height
        bottom_top = None
        bottom_bottom = None
        for name, ctrl in self._c.items():
            if ctrl:
                cr = ctrl.getPosSize()
                # Guard against layout glitches where GTK hands us ultra‑narrow
                # widths on first snapshot: clamp to a small but sane minimum
                # so future relayouts have reasonable baselines.
                min_w = _MIN_WIDTHS.get(name)
                cw = cr.Width
                if min_w is not None and cw < min_w:
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
                # Fallback: no controls below response; keep a small gap.
                info["bottom_top"] = cast("int", info["resp_bottom"])
                info["bottom_bottom"] = cast("int", info["resp_bottom"])
                info["gap_below_response"] = 2

        _resize_debug(
            "_capture_initial: win=(%d,%d) resp_bottom=%s bottom_top=%s gap=%s"
            % (info["win_w"], info["win_h"], str(info.get("resp_bottom")), str(info.get("bottom_top")), str(info.get("gap_below_response")))
        )
        # Lightweight per-control width summary for debugging GTK issues.
        try:
            summary_names = ("response", "query", "send", "clear", "model_selector")
            width_summary = {n: info["ctrls"][n][2] for n in summary_names if n in info["ctrls"]}
            _resize_debug("_capture_initial ctrl_widths=%s" % width_summary)
        except Exception:
            # Logging must never break layout; ignore any issues here.
            pass
        self._initial = info

    def _relayout(self, win):
        r = win.getPosSize()
        w, h = r.Width, r.Height
        if w <= 0 or h <= 0:
            return

        # Match getHeightForWidth: fill parent when aligned; clamp when parent >> deck.
        _DIVERGENCE_PX = 80
        if self._parent_window:
            try:
                pr = self._parent_window.getPosSize()
                pw = pr.Width
                deck = None
                if self._deck_w_getter:
                    try:
                        deck = self._deck_w_getter()
                    except Exception:
                        deck = None
                if deck is not None and deck > 0 and pw > deck + _DIVERGENCE_PX:
                    target_w = min(pw, deck)
                else:
                    target_w = pw
                if target_w > 0 and abs(w - target_w) > 1:
                    _resize_debug("_relayout: sync root W %d -> target W %d (parent=%s deck=%s)" % (w, target_w, pw, deck))
                    win.setPosSize(0, 0, target_w, h, 15)
                    r = win.getPosSize()
                    w, h = r.Width, r.Height
            except Exception as e:
                _resize_debug("_relayout: parent sync skipped: %s" % e)

        _resize_debug("_relayout: win W=%d H=%d (have_initial=%s)" % (w, h, bool(self._initial)))
        fluid_debug = {}

        if self._initial is None:
            self._capture_initial(win)

        if self._initial is None:
            log.warning("_relayout: no initial state, skip")
            return

        initial = cast("dict[str, Any]", self._initial)
        cast("int", initial["win_w"])
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
            # Keep a small fixed margin from the true bottom so controls are visually "at the bottom".
            bottom_margin = 10
            candidate = h - bottom_margin - cluster_height
            # Never push the bottom controls above the original gap below the response.
            min_from_gap = resp_bottom + gap_below_response
            bottom_top_new = max(min_from_gap, candidate)

        # Use anchoring/filling instead of scaling ratios to prevent feedback loops.
        # Controls in fluid_controls will stretch to fill available width with a
        # small fixed right margin; buttons and labels stay fixed size and
        # anchored left.
        fluid_controls = ("response", "query", "model_selector", "image_model_selector", "status", "aspect_ratio_selector")

        top_of_bottom = h  # will track highest new_y (smallest Y) below response
        for name, ctrl in self._c.items():
            if not ctrl or name == "response":
                continue
            orig = initial["ctrls"].get(name)
            if not orig:
                continue
            ox, oy, ow, oh = orig

            fixed_margin = 6
            avail = w - ox - fixed_margin
            if name in fluid_controls:
                # Fill space to a fixed right margin so GTK layout quirks or
                # a bad initial snapshot cannot permanently shrink widths.
                new_x = ox
                new_w = max(10, avail)
            elif name == "backend_indicator":
                # Fixed size, but anchored to the calculated right edge of the response field.
                # The response field uses: max(10, w - response_ox - fixed_margin).
                # We align the right side of the indicator to that same point so it never overflows.
                new_w = ow
                resp_orig = initial["ctrls"].get("response")
                if resp_orig:
                    resp_ox = resp_orig[0]
                    resp_avail = w - resp_ox - fixed_margin
                    resp_right_edge = resp_ox + max(10, resp_avail)
                    new_x = resp_right_edge - new_w
                else:
                    new_x = w - new_w - fixed_margin
                if new_x < ox:  # Don't let it overlap controls to its left
                    new_x = ox
            else:
                # Fixed size, anchored left
                new_x = ox
                new_w = ow

            # Never let controls collapse below a reasonable minimum width;
            # this counteracts GTK cases where they become ~10px wide.
            # For fluid controls, never exceed horizontal space (avail).
            min_w = _MIN_WIDTHS.get(name)
            if min_w is not None and new_w < min_w:
                if name in fluid_controls:
                    new_w = max(new_w, min(min_w, max(10, avail)))
                else:
                    new_w = min_w
            if name in fluid_controls:
                fluid_debug[name] = new_w

            if oy >= resp_bottom:
                # Part of the bottom control group: keep relative spacing but move group toward bottom.
                if bottom_top_new is not None and bottom_top_initial is not None:
                    delta = bottom_top_new - bottom_top_initial
                    new_y = oy + delta
                else:
                    # Fallback: preserve distance from bottom edge (original behavior).
                    new_y = h - (ih - oy)
                cur = ctrl.getPosSize()
                if cur.X != new_x or cur.Y != new_y or cur.Width != new_w or cur.Height != oh:
                    ctrl.setPosSize(new_x, new_y, new_w, oh, 15)
                top_of_bottom = min(top_of_bottom, new_y)
            else:
                # Above response: stay anchored to top
                cur = ctrl.getPosSize()
                if cur.X != new_x or cur.Y != oy or cur.Width != new_w or cur.Height != oh:
                    ctrl.setPosSize(new_x, oy, new_w, oh, 15)

        if fluid_debug:
            _resize_debug("_relayout fluid widths: %s" % fluid_debug)

        # Second pass: stretch response area to fill remaining vertical gap
        resp_orig = initial["ctrls"].get("response")
        resp_ctrl = self._c.get("response")
        if resp_orig and resp_ctrl:
            rx, ry, rw, rh = resp_orig
            gap = gap_below_response
            if gap < 0:
                gap = 2
            new_rh = max(30, top_of_bottom - gap - ry)

            # Fill width to right margin (same avail cap as other fluid controls)
            fixed_margin = 6
            resp_avail = w - rx - fixed_margin
            new_rw = max(10, resp_avail)
            min_rw = _MIN_WIDTHS.get("response")
            if min_rw is not None and new_rw < min_rw:
                new_rw = max(new_rw, min(min_rw, max(10, resp_avail)))

            cur = resp_ctrl.getPosSize()
            if cur.X != rx or cur.Y != ry or cur.Width != new_rw or cur.Height != new_rh:
                resp_ctrl.setPosSize(rx, ry, new_rw, new_rh, 15)
