def _rollback_leave(self, control: Any, mouse_track: Any, focus_track: Any) -> None:
    first_exc = None

    def _safe_remove(ctrl: Any, method: str, listener: Any) -> None:
        nonlocal first_exc
        try:
            self._remove(ctrl, method, listener)
        except Exception as exc:
            if first_exc is None:
                first_exc = exc

    _safe_remove(control, "removeFocusListener", focus_track)
    _safe_remove(control, "removeMouseListener", mouse_track)
    self._drop_tracker(focus_track)
    self._drop_tracker(mouse_track)
    self._leave = [row for row in self._leave if not (row[0] is control and row[1] is mouse_track and row[2] is focus_track)]

    if first_exc is not None:
        raise first_exc
