import re

with open("plugin/calc/python/function.py", "r") as f:
    c = f.read()

new_func = '''def _spill_registry_doc_key(doc: Any) -> str:
    """Always return the lifecycle id (RuntimeUID) for SPILL_REGISTRY keys.

    Bugfix: every unsaved book reports ``getURL() == ""``. Keying
    ``SPILL_REGISTRY`` and ``LOADED_DOCUMENTS`` on ``""`` mixed their spill
    cells, made the second book skip load, and let save write every untitled
    row into whichever book was saving.
    Bugfix 2: previously we kept the file URL for saved books. Save-As changes
    the URL, but doesn't re-key existing registry entries, so they become orphaned.
    Using lifecycle id consistently keeps them matchable across Save-As.
    """
    if doc is None:
        return ""
    try:
        from plugin.calc.python.workbook_lifecycle import _lifecycle_key

        return str(_lifecycle_key(doc) or "")
    except Exception:
        log.debug("spill registry identity failed", exc_info=True)
        return ""
'''

old_func_pattern = r'def _spill_registry_doc_key\(doc: Any\) -> str:[\s\S]*?return ""'
c = re.sub(old_func_pattern, new_func, c, count=1)


new_clear = '''def clear_in_memory_spill_state(*, lifecycle_key: str = "") -> None:
    """Drop instance-scoped spill maps. UD property is left for a later open of the same file."""
    if lifecycle_key:
        cancel_pending_spill_timers(lifecycle_key)
        for skey in [k for k in SHEET_MODIFY_LISTENERS if k[0] == lifecycle_key]:
            SHEET_MODIFY_LISTENERS.pop(skey, None)
        LOADED_DOCUMENTS.discard(lifecycle_key)
        with _SPILL_REGISTRY_LOCK:
            for key in [k for k in SPILL_REGISTRY if k[0] == lifecycle_key]:
                SPILL_REGISTRY.pop(key, None)
    clear_python_addin_cache()'''

old_clear_pattern = r'def clear_in_memory_spill_state\(\*, doc_url: str = "", lifecycle_key: str = ""\) -> None:[\s\S]*?clear_python_addin_cache\(\)'
c = re.sub(old_clear_pattern, new_clear, c, count=1)

with open("plugin/calc/python/function.py", "w") as f:
    f.write(c)
