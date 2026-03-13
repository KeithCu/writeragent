import os
import sys
import random
import subprocess
import threading
import queue
import time
import uuid
import json

_lo_queue = queue.Queue()
_lo_thread = None
_lo_ctx = None
_lo_desktop = None
_lo_docs = {}
_lo_proc = None  # headless soffice process, for cleanup


def _bootstrap_headless():
    """Start LibreOffice in headless mode and return the component context."""
    global _lo_proc
    # Resolve soffice executable (same logic as officehelper)
    base = os.environ.get("UNO_PATH", "")
    soffice = os.path.join(base, "soffice")
    if sys.platform.startswith("win"):
        soffice += ".exe"
    if not os.path.isabs(soffice) and not soffice.startswith(os.sep):
        # Rely on PATH if UNO_PATH not set
        import shutil
        soffice = shutil.which("soffice") or shutil.which("soffice.exe") or soffice
    random.seed()
    pipe_name = "uno" + str(random.random())[2:]
    accept = f"--accept=pipe,name={pipe_name};urp;"
    proc = subprocess.Popen(
        [soffice, "--headless", "--nologo", "--nodefault", accept],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _lo_proc = proc
    try:
        import uno
        from com.sun.star.connection import NoConnectException
        local_ctx = uno.getComponentContext()
        resolver = local_ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local_ctx
        )
        url = f"uno:pipe,name={pipe_name};urp;StarOffice.ComponentContext"
        for _ in range(20):
            try:
                return resolver.resolve(url)
            except NoConnectException:
                time.sleep(0.5)
        raise RuntimeError("Cannot connect to soffice server (headless).")
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
        raise


class LOBackend:
    @classmethod
    def start(cls):
        global _lo_thread, _lo_ctx, _lo_desktop
        if _lo_thread is not None:
            return
            
        # Add LibreOffice python paths dynamically if we are in a venv
        import sys
        import glob
        # Common paths where UNO and officehelper might live on Linux
        lo_paths = [
            "/usr/lib/python3.14/site-packages",
            "/usr/lib/libreoffice/program",
        ]
        # Find paths from standard distro installs
        for base in ["/usr/lib/python3*/dist-packages", "/usr/lib/python3*/site-packages"]:
            lo_paths.extend(glob.glob(base))
        for p in lo_paths:
            if p not in sys.path and __import__("os").path.exists(p):
                sys.path.append(p)

        try:
            import officehelper  # noqa: F401 - ensure UNO env is available
        except ImportError:
            raise ImportError("Could not find officehelper. Are you sure LibreOffice is installed? You may need to run your venv with --system-site-packages or install python3-uno.")

        _lo_ctx = _bootstrap_headless()
        smgr = _lo_ctx.getServiceManager()
        _lo_desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", _lo_ctx)
        
        # Initialize plugins to ensure paths/services are correct
        from plugin.main import bootstrap
        bootstrap(_lo_ctx)

        _lo_thread = threading.Thread(target=cls._worker_loop, daemon=True)
        _lo_thread.start()
        
    @classmethod
    def stop(cls):
        global _lo_thread
        if _lo_thread is None:
            return
        cls.call(cls._cleanup)
        _lo_queue.put(None)
        _lo_thread.join()
        _lo_thread = None
        
    @classmethod
    def call(cls, func, *args, **kwargs):
        if _lo_thread is not None and threading.get_ident() == _lo_thread.ident:
            return func(*args, **kwargs)
        evt = threading.Event()
        result_box = []
        def _task():
            try:
                result_box.append((True, func(*args, **kwargs)))
            except Exception as e:
                result_box.append((False, e))
            evt.set()
        _lo_queue.put(_task)
        evt.wait()
        success, res = result_box[0]
        if not success:
            raise res
        return res
        
    @classmethod
    def _worker_loop(cls):
        while True:
            task = _lo_queue.get()
            if task is None:
                break
            task()
            
    @classmethod
    def _cleanup(cls):
        global _lo_proc
        for doc in list(_lo_docs.values()):
            try:
                doc.close(True)
            except Exception:
                pass
        _lo_docs.clear()
        try:
            _lo_desktop.terminate()
        except Exception:
            pass
        if _lo_proc is not None:
            try:
                _lo_proc.terminate()
                _lo_proc.wait(timeout=5)
            except Exception:
                pass
            _lo_proc = None

    @classmethod
    def acquire_document(cls):
        tid = threading.get_ident()
        if tid not in _lo_docs:
            def _create():
                from com.sun.star.beans import PropertyValue
                p = PropertyValue()
                p.Name = "Hidden"
                p.Value = True
                return _lo_desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, (p,))
            doc = cls.call(_create)
            _lo_docs[tid] = doc
        return _lo_docs[tid]

def _tool_ctx(doc):
    from plugin.framework.tool_context import ToolContext
    from plugin.main import get_services
    return ToolContext(doc, _lo_ctx, "writer", get_services(), "eval")

def set_document(content: str):
    def _do():
        doc = LOBackend.acquire_document()
        text = doc.getText()
        text.setString(content)
    LOBackend.call(_do)

def get_content() -> str:
    def _do():
        doc = LOBackend.acquire_document()
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        return cursor.getString()
    return LOBackend.call(_do)

def get_document_content(scope="full", max_chars=None, start=None, end=None) -> str:
    def _do():
        doc = LOBackend.acquire_document()
        from plugin.main import get_tools
        params = {"scope": scope}
        if max_chars is not None: params["max_chars"] = max_chars
        if start is not None: params["start"] = start
        if end is not None: params["end"] = end
        
        ctx = _tool_ctx(doc)
        res = get_tools().execute("get_document_content", ctx, **params)
        return json.dumps(res, ensure_ascii=False)
    return LOBackend.call(_do)

def apply_document_content(content: str, target="end", search=None, start=None, end=None, all_matches=False, case_sensitive=True) -> str:
    def _do():
        doc = LOBackend.acquire_document()
        from plugin.main import get_tools
        params = {"content": content, "target": target}
        if search is not None: params["search"] = search
        if start is not None: params["start"] = start
        if end is not None: params["end"] = end
        if all_matches is not None: params["all_matches"] = all_matches
        if case_sensitive is not None: params["case_sensitive"] = case_sensitive
        
        ctx = _tool_ctx(doc)
        res = get_tools().execute("apply_document_content", ctx, **params)
        return json.dumps(res, ensure_ascii=False)
    return LOBackend.call(_do)

def find_text(search: str, start=0, limit=None, case_sensitive=True) -> str:
    def _do():
        doc = LOBackend.acquire_document()
        from plugin.modules.writer.format_support import find_text_ranges
        try:
            ranges = find_text_ranges(doc, _lo_ctx, search, case_sensitive=case_sensitive)
            if limit:
                ranges = ranges[:limit]
            return json.dumps({"status": "ok", "ranges": ranges}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})
    return LOBackend.call(_do)

def dspy_get_document_content(scope: str = "full", max_chars: int = None, start: int = None, end: int = None) -> str:
    """Read the LibreOffice document. Returns JSON with content."""
    return get_document_content(scope, max_chars, start, end)

def dspy_apply_document_content(content: str, target: str = "end", search: str = None, start: int = None, end: int = None, all_matches: bool = False, case_sensitive: bool = True) -> str:
    """Modify the LibreOffice document. Content can be plain text or HTML. Returns JSON status."""
    return apply_document_content(content, target, search, start, end, all_matches, case_sensitive)

def dspy_find_text(search: str, start: int = 0, limit: int = None, case_sensitive: bool = True) -> str:
    """Search the LibreOffice document for text. Returns JSON with ranges."""
    return find_text(search, start, limit, case_sensitive)

VERBOSE = False

def with_logging(func, name):
    def wrapper(*args, **kwargs):
        if VERBOSE:
            print(f"  [Tool] {name} {args} {kwargs}")
        res = func(*args, **kwargs)
        if VERBOSE:
            print(f"  [Tool->] {res}")
        return res
    wrapper.__name__ = name
    wrapper.__doc__ = func.__doc__
    return wrapper

def get_tools_subset(names: list[str] | None = None):
    mapping = {
        "get_document_content": dspy_get_document_content,
        "apply_document_content": dspy_apply_document_content,
        "find_text": dspy_find_text,
    }
    if names is None:
        names = ["get_document_content", "apply_document_content", "find_text"]
    return [with_logging(mapping[n], n) for n in names if n in mapping]
