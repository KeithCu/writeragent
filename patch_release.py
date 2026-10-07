def release_listeners(self) -> None:
    """Remove this session's listeners from controls that are still alive."""
    first_exc = None

    def _safe_remove(control: Any, method: str, listener: Any) -> None:
        nonlocal first_exc
        try:
            self._remove(control, method, listener)
        except Exception as exc:
            if first_exc is None:
                first_exc = exc

    if self._query_listener is not None:
        _safe_remove(self._query_control, "removeFocusListener", self._query_listener)
    for control, mouse, focus in list(self._leave):
        _safe_remove(control, "removeMouseListener", mouse)
        _safe_remove(control, "removeFocusListener", focus)
    if self._click_handler is not None:
        _safe_remove(self._click_controller, "removeMouseClickHandler", self._click_handler)
    self._query_listener = None
    self._query_control = None
    self._leave.clear()
    self._click_handler = None
    self._click_controller = None
    self._trackers.clear()

    if first_exc is not None:
        raise first_exc
