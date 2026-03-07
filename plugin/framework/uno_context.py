"""Global UNO component context provider.

Services are singletons that outlive the UNO component that created them.
The ctx passed during bootstrap (from MainJob.__init__) can become stale.

``uno.getComponentContext()`` always returns the current, valid global
context — this is the same call the fallback autostart thread uses.

All services that need UNO access should call ``get_ctx()`` rather than
storing a ctx reference from ``initialize()``.
"""

import logging

log = logging.getLogger("writeragent.context")

_fallback_ctx = None


def set_fallback_ctx(ctx):
    """Store a fallback ctx for use when uno module is not available."""
    global _fallback_ctx
    _fallback_ctx = ctx


def get_ctx():
    """Return the current valid UNO component context.

    Prefers ``uno.getComponentContext()`` (always fresh).
    Falls back to the stored bootstrap ctx if uno is not importable.
    """
    try:
        import uno
        ctx = uno.getComponentContext()
        if ctx is not None:
            return ctx
    except ImportError:
        pass
    return _fallback_ctx
