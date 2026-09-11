# testing_utils.py
# Centralized testing utilities and mocks for WriterAgent tests.

import contextlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

# `com.sun.*` names created/updated by setup_uno_mocks (for-loop).
_COM_SUN_STAR_MOCK_MODULE_KEYS = [
    "com",
    "com.sun",
    "com.sun.star",
    "com.sun.star.text",
    "com.sun.star.util",
    "com.sun.star.document",
    "com.sun.star.frame",
    "com.sun.star.beans",
    "com.sun.star.awt",
    "com.sun.star.task",
    "com.sun.star.lang",
    "com.sun.star.style",
    "com.sun.star.style.BreakType",
    "com.sun.star.ui",
    "com.sun.star.ui.UIElementType",
    "com.sun.star.container",
    "com.sun.star.uno",
    "com.sun.star.datatransfer",
    "com.sun.star.datatransfer.clipboard",
]

# Every sys.modules key setup_uno_mocks assigns, plus core.* used by some uno tests.
# plugin.testing_runner.run_all_tests snapshots/restores this list between native suites.
NATIVE_TEST_SYS_MODULE_SNAPSHOT_KEYS = (
    "uno",
    "unohelper",
    "unohelper.Base",
    *_COM_SUN_STAR_MOCK_MODULE_KEYS,
    "core",
    "core.logging",
    "core.async_stream",
    "core.config",
    "core.api",
    "core.document",
    "core.document_tools",
    "core.constants",
)


def setup_uno_mocks():
    """
    Centralized function to mock LibreOffice UNO dependencies for testing outside of LibreOffice.
    This must be called at the top of test files before importing the module under test.
    """
    # Real `uno` is a types.ModuleType (embedded LibreOffice PyUNO or types-unopy in the venv).
    # Never replace it with MagicMock — that breaks in-LO native tests (e.g. uno.createUnoStruct).
    # Still create missing `com.sun.star.*` shell modules for pytest without a full bridge: types-unopy
    # provides `uno` but often has not loaded `com.sun.star.lang` etc. yet.
    try:
        import uno  # noqa: F401
    except ImportError:
        uno_import_ok = False
    else:
        uno_import_ok = True

    um = sys.modules.get("uno")
    use_magicmock_uno = not (uno_import_ok and isinstance(um, types.ModuleType))

    class MockBase(object):
        pass

    if use_magicmock_uno:
        sys.modules["uno"] = MagicMock()
        sys.modules["unohelper"] = MagicMock()

        # We must use types.ModuleType and attach empty classes to avoid 'metaclass conflict' with ty
        sys.modules["unohelper"].Base = MockBase
        sys.modules["unohelper.Base"] = MockBase

    created_com_shells: set[str] = set()
    for mod in _COM_SUN_STAR_MOCK_MODULE_KEYS:
        cur = sys.modules.get(mod)
        if cur is None or isinstance(cur, MagicMock):
            sys.modules[mod] = types.ModuleType(mod)
            created_com_shells.add(mod)

    # Do not setattr test doubles onto real bridge-loaded com.sun.star.* modules (embedded LO).
    if not use_magicmock_uno and not created_com_shells:
        return

    # Specific sub-module attachments (only when we fully mocked uno or installed fresh shells).
    class MockDate(object):
        Year = 2024
        Month = 1
        Day = 1

    setattr(sys.modules["com.sun.star.util"], "Date", MockDate)

    class MockListener(object):
        pass

    setattr(sys.modules["com.sun.star.awt"], "XActionListener", MockListener)

    class MockClipboardListener(object):
        pass

    setattr(
        sys.modules["com.sun.star.datatransfer.clipboard"],
        "XClipboardListener",
        MockClipboardListener,
    )

    class MockXCallback(object):
        pass

    setattr(sys.modules["com.sun.star.awt"], "XCallback", MockXCallback)

    awt_mod = sys.modules.get("com.sun.star.awt")
    if awt_mod is not None and not hasattr(awt_mod, "Size"):

        class MockSize:
            def __init__(self, width=0, height=0):
                self.Width = width
                self.Height = height

        class MockPoint:
            def __init__(self, x=0, y=0):
                self.X = x
                self.Y = y

        setattr(awt_mod, "Size", MockSize)
        setattr(awt_mod, "Point", MockPoint)

    class MockXTextListener(object):
        pass

    setattr(sys.modules["com.sun.star.awt"], "XTextListener", MockXTextListener)

    class MockXWindowListener(object):
        pass

    setattr(sys.modules["com.sun.star.awt"], "XWindowListener", MockXWindowListener)

    class MockXKeyListener(object):
        pass

    setattr(sys.modules["com.sun.star.awt"], "XKeyListener", MockXKeyListener)

    class MockXEventListener(object):
        pass

    setattr(sys.modules["com.sun.star.lang"], "XEventListener", MockXEventListener)

    class MockXInitialization(object):
        pass

    setattr(sys.modules["com.sun.star.lang"], "XInitialization", MockXInitialization)

    class MockXServiceInfo(object):
        pass

    setattr(sys.modules["com.sun.star.lang"], "XServiceInfo", MockXServiceInfo)

    class MockXJobExecutor(object):
        pass

    setattr(sys.modules["com.sun.star.task"], "XJobExecutor", MockXJobExecutor)

    class MockXJob(object):
        pass

    setattr(sys.modules["com.sun.star.task"], "XJob", MockXJob)

    class MockXDispatch(object):
        pass

    setattr(sys.modules["com.sun.star.frame"], "XDispatch", MockXDispatch)

    class MockXDispatchProvider(object):
        pass

    setattr(sys.modules["com.sun.star.frame"], "XDispatchProvider", MockXDispatchProvider)
    setattr(sys.modules["com.sun.star.frame"], "DispatchDescriptor", MockBase)

    # Fresh shells replace conftest MagicMock beans; image_tools imports PropertyValue at load time.
    beans_mod = sys.modules.get("com.sun.star.beans")
    if beans_mod is not None and not hasattr(beans_mod, "PropertyValue"):

        class MockPropertyValue:
            def __init__(self, Name=None, Value=None):
                self.Name = Name
                self.Value = Value

        setattr(beans_mod, "PropertyValue", MockPropertyValue)

    class MockNoSuchElementException(Exception):
        pass

    class MockDisposedException(Exception):
        pass

    class MockIllegalArgumentException(Exception):
        pass

    class MockRuntimeException(Exception):
        pass

    class MockUnoException(Exception):
        pass

    setattr(sys.modules["com.sun.star.container"], "NoSuchElementException", MockNoSuchElementException)
    setattr(sys.modules["com.sun.star.lang"], "DisposedException", MockDisposedException)
    setattr(sys.modules["com.sun.star.lang"], "IllegalArgumentException", MockIllegalArgumentException)
    setattr(sys.modules["com.sun.star.uno"], "RuntimeException", MockRuntimeException)
    setattr(sys.modules["com.sun.star.uno"], "Exception", MockUnoException)

    class MockXSidebarPanel:
        pass

    class MockXToolPanel:
        pass

    class MockXUIElement:
        pass

    class MockXUIElementFactory:
        pass

    setattr(sys.modules["com.sun.star.ui"], "XSidebarPanel", MockXSidebarPanel)
    setattr(sys.modules["com.sun.star.ui"], "XToolPanel", MockXToolPanel)
    setattr(sys.modules["com.sun.star.ui"], "XUIElement", MockXUIElement)
    setattr(sys.modules["com.sun.star.ui"], "XUIElementFactory", MockXUIElementFactory)

class ElementStub:
    def __init__(self, text, outline_level=0, services=None):
        self.text = text
        self.outline_level = outline_level
        self.services = services or ["com.sun.star.text.Paragraph"]

    def getString(self):
        return self.text

    def getPropertyValue(self, name):
        if name == "OutlineLevel":
            return self.outline_level
        from plugin.framework.errors import WriterAgentException
        raise WriterAgentException("Property not found")

    def supportsService(self, service):
        return service in self.services

    def getStart(self):
        return self # Stub for range

    def getEnd(self):
        return self

    def getText(self):
        return self

class WriterDocStub:
    def __init__(self, elements=None, doc_type="writer", items=None):
        self.elements = elements or []
        self.doc_type = doc_type
        self._items = items or {}
        self.url = f"test://{doc_type}"
        self._created = {}
        self._load_styles_calls = []

    def getText(self):
        class TextStub:
            def __init__(self, el):
                self.el = el

            def createEnumeration(self):
                class EnumStub:
                    def __init__(self, el):
                        self.el = el
                        self.idx = 0

                    def hasMoreElements(self):
                        return self.idx < len(self.el)

                    def nextElement(self):
                        res = self.el[self.idx]
                        self.idx += 1
                        return res
                return EnumStub(self.el)
        return TextStub(self.elements)

    def supportsService(self, svc):
        if self.doc_type == "writer" and svc == "com.sun.star.text.TextDocument": return True
        if self.doc_type == "calc" and svc == "com.sun.star.sheet.SpreadsheetDocument": return True
        if self.doc_type == "draw" and svc == "com.sun.star.drawing.DrawingDocument": return True
        if self.doc_type == "impress" and svc == "com.sun.star.presentation.PresentationDocument": return True
        return False

    def getStyleFamilies(self):
        class FamiliesStub:
            def __init__(self, items):
                self.items = items
            def hasByName(self, name):
                return name in self.items
            def getByName(self, name):
                return self.items[name]
            def getElementNames(self):
                return tuple(self.items.keys())
        return FamiliesStub(self._items)

    def getMyItems(self):
        return self.getStyleFamilies()

    def createInstance(self, name):
        inst = self._created.get(name)
        if inst is None:
            inst = MagicMock(name=name)
            self._created[name] = inst
        return inst

    def loadStylesFromURL(self, url, props):
        self._load_styles_calls.append((url, props))

class MockDocument:
    def __init__(self):
        self.url = "test://mock"

    def supportsService(self, service):
        return False

class MockTextCursor:
    def __init__(self):
        pass

    def getStart(self): return self
    def getEnd(self): return self
    def getString(self): return ""
    def setString(self, val): pass
    def gotoStart(self, expand): pass
    def gotoEnd(self, expand): pass
    def goRight(self, count, expand): pass
    def goLeft(self, count, expand): pass
    def setPropertyValue(self, name, val): pass


# UNO CellContentType values (com.sun.star.table.CellContentType).
_CELL_EMPTY = 0
_CELL_VALUE = 1
_CELL_TEXT = 2
_CELL_FORMULA = 3


class _RangeAddress:
    __slots__ = ("StartColumn", "StartRow", "EndColumn", "EndRow", "Sheet")

    def __init__(self, start_col, start_row, end_col, end_row, sheet=0):
        self.StartColumn = start_col
        self.StartRow = start_row
        self.EndColumn = end_col
        self.EndRow = end_row
        self.Sheet = sheet


class _CellAddress:
    __slots__ = ("Column", "Row", "Sheet")

    def __init__(self, col, row, sheet=0):
        self.Column = col
        self.Row = row
        self.Sheet = sheet


class CalcCellStub:
    """Stateful stand-in for a Calc cell / single-cell range."""

    def __init__(self, col=0, row=0, sheet=None):
        self._col = col
        self._row = row
        self._sheet = sheet
        self._string = ""
        self._value = 0.0
        self._formula = ""
        self._kind = _CELL_EMPTY  # empty | value | text | formula

    def getString(self):
        return self._string

    def setString(self, value):
        self._string = "" if value is None else str(value)
        self._formula = ""
        self._value = 0.0
        self._kind = _CELL_TEXT if self._string else _CELL_EMPTY

    def getValue(self):
        return self._value

    def setValue(self, value):
        try:
            self._value = float(value)
        except (TypeError, ValueError):
            self._value = 0.0
        self._string = ""
        self._formula = ""
        self._kind = _CELL_VALUE

    def getFormula(self):
        return self._formula

    def setFormula(self, value):
        text = "" if value is None else str(value)
        self._formula = text
        if not text:
            self._string = ""
            self._value = 0.0
            self._kind = _CELL_EMPTY
        else:
            self._kind = _CELL_FORMULA

    def getType(self):
        return self._kind

    def clearContents(self, _flags=0):
        self._string = ""
        self._value = 0.0
        self._formula = ""
        self._kind = _CELL_EMPTY

    def getCellAddress(self):
        return _CellAddress(self._col, self._row)

    def getRangeAddress(self):
        return _RangeAddress(self._col, self._row, self._col, self._row)

    def getPropertyValue(self, _name):
        return None

    def setPropertyValue(self, _name, _val):
        pass

    def getSpreadsheet(self):
        return self._sheet


class CalcRangeStub:
    """Rectangular range backed by a CalcSheetStub grid."""

    def __init__(self, sheet, start_col, start_row, end_col, end_row):
        self._sheet = sheet
        self._start_col = start_col
        self._start_row = start_row
        self._end_col = end_col
        self._end_row = end_row

    def getRangeAddress(self):
        return _RangeAddress(self._start_col, self._start_row, self._end_col, self._end_row)

    def getCellByPosition(self, col, row):
        # Relative to range origin (UNO XCellRange).
        return self._sheet.getCellByPosition(self._start_col + col, self._start_row + row)

    def getDataArray(self):
        rows = []
        for r in range(self._start_row, self._end_row + 1):
            row_vals = []
            for c in range(self._start_col, self._end_col + 1):
                cell = self._sheet.getCellByPosition(c, r)
                if cell.getType() == _CELL_VALUE:
                    row_vals.append(cell.getValue())
                elif cell.getType() == _CELL_FORMULA:
                    row_vals.append(cell.getFormula())
                else:
                    row_vals.append(cell.getString())
            rows.append(tuple(row_vals))
        return tuple(rows)

    def setDataArray(self, data):
        for r_off, row in enumerate(data or ()):
            for c_off, value in enumerate(row):
                cell = self._sheet.getCellByPosition(self._start_col + c_off, self._start_row + r_off)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cell.setValue(value)
                elif value is None or value == "":
                    cell.clearContents()
                else:
                    text = str(value)
                    if text.startswith("="):
                        cell.setFormula(text)
                    else:
                        cell.setString(text)

    def getFormulas(self):
        rows = []
        for r in range(self._start_row, self._end_row + 1):
            row_vals = []
            for c in range(self._start_col, self._end_col + 1):
                row_vals.append(self._sheet.getCellByPosition(c, r).getFormula())
            rows.append(tuple(row_vals))
        return tuple(rows)

    def getFormula(self):
        return self.getCellByPosition(0, 0).getFormula()

    def setFormula(self, value):
        self.getCellByPosition(0, 0).setFormula(value)

    def getString(self):
        return self.getCellByPosition(0, 0).getString()

    def setString(self, value):
        self.getCellByPosition(0, 0).setString(value)

    def getValue(self):
        return self.getCellByPosition(0, 0).getValue()

    def setValue(self, value):
        self.getCellByPosition(0, 0).setValue(value)

    def getType(self):
        return self.getCellByPosition(0, 0).getType()

    def clearContents(self, flags=0):
        for r in range(self._start_row, self._end_row + 1):
            for c in range(self._start_col, self._end_col + 1):
                self._sheet.getCellByPosition(c, r).clearContents(flags)

    def getSpreadsheet(self):
        return self._sheet


class CalcSheetStub:
    """Named sheet with an expandable cell grid."""

    def __init__(self, name="Sheet1", data=None):
        self._name = name
        self._cells = {}
        self._modify_listeners: list = []
        self.DrawPage = MagicMock(name=f"{name}.DrawPage")
        if data is not None:
            self._seed_data(data)

    def _seed_data(self, data):
        for row_idx, row in enumerate(data):
            for col_idx, value in enumerate(row):
                if value is None or value == "":
                    continue
                cell = self.getCellByPosition(col_idx, row_idx)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cell.setValue(value)
                else:
                    text = str(value)
                    if text.startswith("="):
                        cell.setFormula(text)
                    else:
                        cell.setString(text)

    def getName(self):
        return self._name

    def getCellByPosition(self, col, row):
        key = (int(col), int(row))
        cell = self._cells.get(key)
        if cell is None:
            cell = CalcCellStub(col=key[0], row=key[1], sheet=self)
            self._cells[key] = cell
        return cell

    def getCellRangeByPosition(self, start_col, start_row, end_col, end_row):
        return CalcRangeStub(self, int(start_col), int(start_row), int(end_col), int(end_row))

    def getCellRangeByName(self, name):
        from plugin.calc.address_utils import parse_range_string

        (start_col, start_row), (end_col, end_row) = parse_range_string(name)
        if start_col == end_col and start_row == end_row:
            return self.getCellByPosition(start_col, start_row)
        return self.getCellRangeByPosition(start_col, start_row, end_col, end_row)

    def addModifyListener(self, listener):
        self._modify_listeners.append(listener)

    def removeModifyListener(self, listener):
        try:
            self._modify_listeners.remove(listener)
        except ValueError:
            pass

    def queryContentCells(self, _flags=0):
        """Return formula cells as a UNO-like enum (CellFlags.FORMULA = 16 in production).

        Stub ignores *flags* and always enumerates formula cells — enough for pytest
        discovery paths that only query formulas.
        """
        formula_keys = [(c, r) for (c, r), cell in self._cells.items() if cell.getType() == _CELL_FORMULA]
        if not formula_keys:
            return _ContentCellsEnum([])
        cols = [c for c, _r in formula_keys]
        rows = [r for _c, r in formula_keys]
        rng = self.getCellRangeByPosition(min(cols), min(rows), max(cols), max(rows))
        return _ContentCellsEnum([rng])


class _ContentCellsEnum:
    """Minimal stand-in for XSheetCellRanges enumeration from queryContentCells."""

    def __init__(self, ranges):
        self._ranges = list(ranges)

    def getCount(self):
        return len(self._ranges)

    def getByIndex(self, index):
        return self._ranges[int(index)]


class CalcSheetsStub:
    """XSpreadsheets-like collection."""

    def __init__(self, sheets=None):
        self._sheets = {}
        self._order = []
        if sheets:
            for sheet in sheets:
                self._add(sheet)
        else:
            self._add(CalcSheetStub("Sheet1"))

    def _add(self, sheet):
        name = sheet.getName()
        if name not in self._sheets:
            self._order.append(name)
        self._sheets[name] = sheet

    def hasByName(self, name):
        return name in self._sheets

    def getByName(self, name):
        return self._sheets[name]

    def getByIndex(self, index):
        return self._sheets[self._order[int(index)]]

    def getCount(self):
        return len(self._order)

    def getElementNames(self):
        return tuple(self._order)

    def insertNewByName(self, name, index):
        sheet = CalcSheetStub(name)
        idx = max(0, min(int(index), len(self._order)))
        if name in self._sheets:
            self._sheets[name] = sheet
            return
        self._order.insert(idx, name)
        self._sheets[name] = sheet


class CalcControllerStub:
    """Current controller with both attribute and method access styles."""

    def __init__(self, active_sheet, selection=None):
        self.ActiveSheet = active_sheet
        self.Selection = selection if selection is not None else active_sheet.getCellByPosition(0, 0)

    def getActiveSheet(self):
        return self.ActiveSheet

    def getSelection(self):
        return self.Selection


class CalcDocStub:
    """Stateful SpreadsheetDocument stub for pure pytest (no live LibreOffice).

    Defaults: one sheet ``Sheet1``, selection A1, ``url='test://calc'``.
    Seed a 2D grid with ``data=``; override selection / command values via kwargs.
    """

    def __init__(
        self,
        data=None,
        sheets=None,
        url="test://calc",
        command_values=None,
        selection=None,
        active_sheet=None,
        props=None,
        **_kwargs,
    ):
        if sheets is not None:
            sheet_list = list(sheets)
        else:
            sheet_list = [CalcSheetStub("Sheet1", data=data)]
        self._sheets = CalcSheetsStub(sheet_list)
        active = active_sheet
        if active is None:
            active = self._sheets.getByIndex(0)
        elif isinstance(active, str):
            active = self._sheets.getByName(active)
        if selection is None:
            selection = active.getCellByPosition(0, 0)
        elif isinstance(selection, str):
            selection = active.getCellRangeByName(selection)
        self._controller = CalcControllerStub(active, selection=selection)
        self.CurrentController = self._controller
        self.url = url
        self._command_values = command_values
        self._close_calls = []
        self._created = {}
        self._props = dict(props or {})
        self._document_event_listeners = []
        self._calculate_all_calls = 0
        # Number-format supplier hooks for inspector/enrichment pytest (override via kwargs).
        self._number_formats = _kwargs.get("number_formats")
        if self._number_formats is None:
            self._number_formats = MagicMock(name="NumberFormats")
        self._null_date = _kwargs.get("null_date") or SimpleNamespace(Year=1899, Month=12, Day=30)

    def supportsService(self, svc):
        return svc == "com.sun.star.sheet.SpreadsheetDocument"

    def getSheets(self):
        return self._sheets

    def getCurrentController(self):
        return self._controller

    def getURL(self):
        return self.url

    def getNumberFormats(self):
        return self._number_formats

    def getNumberFormatSettings(self):
        settings = MagicMock(name="NumberFormatSettings")
        settings.getPropertyValue.return_value = self._null_date
        return settings

    def calculateAll(self):
        self._calculate_all_calls += 1

    @property
    def calculate_all_count(self):
        return self._calculate_all_calls

    def getCommandValues(self, _command=None):
        return self._command_values

    def getPropertyValue(self, name):
        if name not in self._props:
            raise KeyError(name)
        return self._props[name]

    def setPropertyValue(self, name, value):
        self._props[name] = value

    def addDocumentEventListener(self, listener):
        self._document_event_listeners.append(listener)

    def createInstance(self, name):
        inst = self._created.get(name)
        if inst is None:
            inst = MagicMock(name=name)
            self._created[name] = inst
        return inst

    def close(self, unused=True):
        self._close_calls.append(unused)

    def dispose(self):
        self.close(True)


class MockContext:
    """Mock context object used as a stand-in for the UNO ComponentContext outside of LibreOffice."""
    def __init__(self):
        self.mock_values = {}

    def getValueByName(self, name):
        return self.mock_values.get(name)

    def getServiceManager(self):
        return MagicMock()

# Experimental: wipe-and-reuse one hidden document per (ctx, type, hidden).
# Default ON for Calc only. Writer pooling still leaks CharWeight/HTML styles; pass reuse=True to try it.
_NATIVE_DOC_POOL: dict = {}

# GHA 33703959362: hang after insert_cell_html execute-done, before TEST end.
# When True, stderr breadcrumbs name reset_native_doc / _reset_calc_doc steps.
# with_native_doc sets this only for test_insert_cell_html (keep suite noise low).
_LOG_NATIVE_DOC_TEARDOWN = False


def _native_teardown_progress(msg: str) -> None:
    if not _LOG_NATIVE_DOC_TEARDOWN:
        return
    from plugin.testing_runner import _progress

    _progress(msg)


# testing_runner keeper. insert_cell_html_rich leaves extra Writers open
# (close skipped after paste). Never close those leftovers — GHA
# 34556185752 hung 30s in leftover close(True). Reactivate this keeper
# so a later Writer factory is not against a leftover current component.
_HARNESS_KEEPER_UID = ""
_HARNESS_KEEPER_DOC = None


def _testing_utils_holders():
    """Modules that share this file's keeper globals.

    ``python -m plugin.testing_runner`` imports ``tests.testing_utils`` to
    record the keeper. Suites import ``plugin.tests.testing_utils``
    (``plugin/tests/__init__.py`` points ``__path__`` at ``tests/``). Same
    file, second object. GHA 34595675515: every factory prepare printed
    ``keeper=-`` and treated uid=1 as a leftover — #720 never reactivated.
    Same dual-module family as #719 recycle. Touch both.
    """
    import os

    seen = []
    try:
        here_file = os.path.normcase(os.path.realpath(__file__))
    except Exception:
        here_file = ""
    for name in ("tests.testing_utils", "plugin.tests.testing_utils", __name__):
        mod = sys.modules.get(name)
        if mod is None or mod in seen:
            continue
        other = getattr(mod, "__file__", None)
        if other and here_file:
            try:
                if os.path.normcase(os.path.realpath(other)) != here_file:
                    continue
            except Exception:
                continue
        seen.append(mod)
    return seen or [sys.modules[__name__]]


def _ensure_testing_utils_aliases() -> None:
    """Import both sys.modules names so set/adopt can write both copies."""
    try:
        import tests.testing_utils as _tests_tu  # noqa: F401
    except Exception:
        pass
    try:
        import plugin.tests.testing_utils as _plugin_tu  # noqa: F401
    except Exception:
        pass


def set_harness_keeper_uid(uid: str, doc=None) -> None:
    """Record the hidden keeper Writer (uid + doc) for later setActiveFrame."""
    _ensure_testing_utils_aliases()
    uid_s = str(uid or "")
    if uid_s == "-":
        uid_s = ""
    doc_s = doc if uid_s else None
    for mod in _testing_utils_holders():
        mod._HARNESS_KEEPER_UID = uid_s
        mod._HARNESS_KEEPER_DOC = doc_s


def _adopt_keeper_from_sibling() -> bool:
    """Copy keeper uid/doc from the other testing_utils module if we have none."""
    global _HARNESS_KEEPER_UID, _HARNESS_KEEPER_DOC
    if _HARNESS_KEEPER_UID:
        return False
    here = sys.modules.get(__name__)
    for mod in _testing_utils_holders():
        if mod is here:
            continue
        uid = str(getattr(mod, "_HARNESS_KEEPER_UID", "") or "")
        if not uid or uid == "-":
            continue
        _HARNESS_KEEPER_UID = uid
        _HARNESS_KEEPER_DOC = getattr(mod, "_HARNESS_KEEPER_DOC", None)
        return True
    return False


def _writer_doc_uid(doc) -> str:
    try:
        return str(getattr(doc, "RuntimeUID", None) or "")
    except Exception:
        return ""


def _writer_frame_name(doc) -> str:
    try:
        frame = doc.getCurrentController().getFrame()
        name = frame.getName()
        return str(name or "")
    except Exception:
        return ""


def _native_doc_svc(doc) -> str:
    """Harness breadcrumb: writer / calc / impress / draw. Impress first.

    ``is True``: MagicMock.supportsService() is truthy and would classify
    every mock as Writer. GHA 34609539461: leftover=1 from a prior prepare
    test then skipped close on an untyped mock, so
    ``test_close_doc_logs_urp_dispose`` never saw the dispose breadcrumb.
    """
    try:
        if doc.supportsService("com.sun.star.text.TextDocument") is True:
            return "writer"
        if doc.supportsService("com.sun.star.sheet.SpreadsheetDocument") is True:
            return "calc"
        if doc.supportsService("com.sun.star.presentation.PresentationDocument") is True:
            return "impress"
        if doc.supportsService("com.sun.star.drawing.DrawingDocument") is True:
            return "draw"
    except Exception:
        return ""
    return ""


def _iter_open_writer_docs(desktop):
    """Yield (uid, doc) for open TextDocuments. Read-only enum; no close."""
    try:
        enum = desktop.getComponents().createEnumeration()
    except Exception:
        return
    n = 0
    while n < 64:
        try:
            if enum.hasMoreElements() is not True:
                break
            component = enum.nextElement()
        except Exception:
            break
        n += 1
        try:
            if component.supportsService("com.sun.star.text.TextDocument"):
                yield _writer_doc_uid(component), component
        except Exception:
            continue


def reactivate_harness_keeper(desktop=None) -> bool:
    """``setActiveFrame`` the keeper. Does not close leftover paste Writers.

    GHA 34556185752: leftover ``close(True)`` hung 30s (uid=27). GHA
    34554275072: first text_helpers Writer factory + ``close_doc``
    returned; the *next* factory hung. After a test Writer close,
    desktop current becomes a leftover paste Writer. Reactivate the
    keeper so the next ``swriter`` load is not against that leftover.
    """
    from plugin.testing_runner import _progress

    if _adopt_keeper_from_sibling():
        _progress(
            "html_paste_writer: keeper adopted from sibling uid=%s"
            % (_HARNESS_KEEPER_UID or "-")
        )
    doc = _HARNESS_KEEPER_DOC
    if doc is None:
        return False
    try:
        frame = doc.getCurrentController().getFrame()
        if desktop is None:
            desktop = frame.getCreator()
        desktop.setActiveFrame(frame)
        _progress("html_paste_writer: keeper reactivated uid=%s" % _HARNESS_KEEPER_UID)
        return True
    except Exception as exc:
        _progress(
            "html_paste_writer: keeper reactivate failed uid=%s err=%s"
            % (_HARNESS_KEEPER_UID or "-", type(exc).__name__)
        )
        return False


def prepare_windows_writer_factory(ctx) -> int:
    """Harness-only: log leftover paste Writers and reactivate the keeper.

    What was wrong: GHA 34554275072 / 34553944171 hung 30s on the
    *second* text_helpers Writer factory. Leftovers uid=26/27 were
    already open (``close skipped pasted=True``). The first factory +
    ``close_doc`` returned. GHA 34556185752 then hung 30s *inside*
    leftover ``close(True)`` — product and harness must not close those
    Writers after paste (33771766524).

    Why this: enum leftovers (read-only; safe before load) and
    ``setActiveFrame`` the keeper. Do not close leftovers. Returns how
    many non-keeper Writers are still open. Windows-only caller.

    GHA 34593327841: the next hang was ``private:factory/scalc`` in
    ``document_research_uno`` ``_create_nearby_test_env``, not swriter.
    Call this before every Windows ``private:factory/`` load.
    """
    if ctx is None:
        return 0
    from plugin.framework.uno_context import get_desktop
    from plugin.testing_runner import _progress

    if _adopt_keeper_from_sibling():
        _progress(
            "html_paste_writer: keeper adopted from sibling uid=%s"
            % (_HARNESS_KEEPER_UID or "-")
        )
    desktop = get_desktop(ctx)
    keeper = _HARNESS_KEEPER_UID
    leftover_uids = []
    leftover_frames = []
    for uid, doc in _iter_open_writer_docs(desktop):
        if not uid or uid == keeper:
            continue
        leftover_uids.append(uid)
        leftover_frames.append(_writer_frame_name(doc) or "-")
    leftover_open = len(leftover_uids)
    _set_windows_leftover_open(leftover_open)
    _progress(
        "html_paste_writer: leftovers open=%s uids=%s frames=%s keeper=%s"
        % (leftover_open, leftover_uids, leftover_frames, keeper or "-")
    )
    reactivate_harness_keeper(desktop)
    return leftover_open


# Last leftover count from prepare. close_doc / native_doc reuse read this
# instead of enumerating again (getComponents after paste close can hang).
_WINDOWS_LEFTOVER_OPEN = 0


def _set_windows_leftover_open(n: int) -> None:
    """Write leftover count on both testing_utils module copies."""
    n_i = int(n or 0)
    for mod in _testing_utils_holders():
        mod._WINDOWS_LEFTOVER_OPEN = n_i


def _windows_leftover_open() -> int:
    n = int(_WINDOWS_LEFTOVER_OPEN or 0)
    if n > 0:
        return n
    here = sys.modules.get(__name__)
    for mod in _testing_utils_holders():
        if mod is here:
            continue
        other = int(getattr(mod, "_WINDOWS_LEFTOVER_OPEN", 0) or 0)
        if other > 0:
            return other
    return 0


def _windows_should_reuse_writer(ctx) -> bool:
    """True when a later Windows Writer factory would hang after leftovers.

    GHA 34601787293 / 34602219973: unique ``_wa_factory_N`` loaded three
    leftover Calc factories and the first leftover swriter (uid=34).
    ``close_doc`` of that Writer returned; the next unique swriter hung
    30s. Reuse the first leftover Writer instead of close + factory.
    """
    if ctx is None or sys.platform != "win32":
        return False
    # Notebook suites use ``_wa_notebook_host``, not leftover HTML-paste
    # Writers (GHA 34643210006: leftover reuse + global listener counts).
    if _windows_notebook_host():
        return False
    return prepare_windows_writer_factory(ctx) > 0


def _windows_should_reuse_calc(ctx) -> bool:
    """True when a leftover scalc factory would hang.

    GHA 34643210006: ``test_calc_reuse_false_still_empty`` loaded leftover
    ``target=_wa_scalc`` at leftover_open=5 and hung 30s (office alive).
    Unique leftover ``_wa_factory_N`` already failed then hung
    (34633295036). Cached leftover count only — do not enumerate
    (getComponents after paste close can hang). Do not close leftover
    paste Writers (34556185752).
    """
    if ctx is None or sys.platform != "win32":
        return False
    return _windows_leftover_open() > 0


# Same CREATE|GLOBAL as insert_cell_html_rich (8|55=63). Named target with
# flags 0 can search instead of creating. Do not reuse "_blank" / "_default"
# while leftover paste Writers are open — see _windows_factory_load_args.
_WINDOWS_FACTORY_SEARCH_FLAGS = 8 | 55
# Leftover Hidden swriter only. rich_html reuses one CREATE|GLOBAL name
# (_wa_calc_html). Unique _wa_factory_N stacked empty frames after close
# and the second leftover swriter hung (GHA 34602219973, _wa_factory_5).
_WINDOWS_FACTORY_TARGET = "_wa_factory"
# Leftover Calc uses one CREATE|GLOBAL name. The pooled @with_native_doc
# Calc is opened at leftover_open=0 as Hidden ``_blank`` — ``_wa_scalc``
# does not replace it. Unique ``_wa_factory_N`` stacked after a failed
# leftover scalc load (GHA 34633295036). Draw/Impress still increment.
_WINDOWS_CALC_FACTORY_TARGET = "_wa_scalc"
_WINDOWS_FACTORY_SEQ = 0


def _windows_factory_load_args(factory_url: str, leftover_open: int) -> tuple[str, int]:
    """Target + FrameSearchFlag for a Windows factory load.

    GHA 34597506651: keeper sync worked (``keeper=1``, reactivated).
    First text_helpers ``_blank`` swriter + leftovers returned (uid=34,
    close_doc OK). The *next* ``_blank`` swriter hung 30s after the same
    leftover log + keeper reactivate.

    GHA 34599838644: same leftovers + ``keeper=1``, but Calc ``_blank``
    failed in ~1s (PyUNO traceback conversion on
    ``loadComponentFromURL(scalc)``) and the next Calc ``_blank`` hung
    30s — ``document_research_uno`` never finished, so the swriter-only
    named target was never reached. ``setActiveFrame`` is not enough
    for Hidden ``_blank`` while leftover ``_wa_calc_html`` frames exist
    (rich_html.py: not ``_blank`` / ``_default``).

    GHA 34602219973 (``ec40ed29``): unique ``_wa_factory_N`` loaded
    leftover Calc (``document_research_uno`` passed=3, targets
    ``_wa_factory_1/2/3``) and the first leftover Hidden swriter
    (``doc.test_text_helpers_uno.test_get_string_without_tracked_deletions_paragraph_bold_run_no_newline``,
    ``target=_wa_factory_4``, uid=34, ``close_doc`` OK). The *next*
    leftover Hidden swriter
    (``…_multi_para_joins_with_newline``, ``target=_wa_factory_5``)
    hung 30s in ``loadComponentFromURL`` — no RuntimeException. Unique
    CREATE stacks empty named frames after harness Writer close; it
    does not fix consecutive leftover Hidden swriter (same hang family
    as 34597506651). Reuse one CREATE|GLOBAL name for leftover
    ``swriter`` so CREATE replaces/reuses instead of stacking.

    GHA 34633295036 (master ``bdb421bb``, post #724): same leftover
    paste Writers (uids ``27``/``26``, ``frames=['-','-']``,
    ``keeper=1``) then unique leftover ``scalc`` ``target=_wa_factory_1``
    failed in ~766ms (PyUNO traceback wrap on
    ``loadComponentFromURL``). The next unique ``_wa_factory_2`` hung
    30s — same stacking family as leftover swriter unique names.
    Pooled Calc is still the leftover_open=0 ``_blank`` workbook.
    Leftover ``scalc`` now reuses one CREATE|GLOBAL name ``_wa_scalc``.
    Draw/Impress stay unique. Do not close leftover paste Writers
    (34556185752).
    """
    global _WINDOWS_FACTORY_SEQ
    if leftover_open <= 0 or not factory_url.startswith("private:factory/"):
        return "_blank", 0
    # One stable name, like rich_html._wa_calc_html. CREATE|GLOBAL finds
    # the empty frame left by the previous leftover-mode Writer close.
    if factory_url == "private:factory/swriter":
        if _windows_notebook_host():
            return _WINDOWS_NOTEBOOK_HOST_TARGET, _WINDOWS_FACTORY_SEARCH_FLAGS
        return _WINDOWS_FACTORY_TARGET, _WINDOWS_FACTORY_SEARCH_FLAGS
    if factory_url == "private:factory/scalc":
        return _WINDOWS_CALC_FACTORY_TARGET, _WINDOWS_FACTORY_SEARCH_FLAGS
    _WINDOWS_FACTORY_SEQ += 1
    return "_wa_factory_%s" % _WINDOWS_FACTORY_SEQ, _WINDOWS_FACTORY_SEARCH_FLAGS


# Stable CREATE|GLOBAL name for Hidden .ipynb loads. Do not use "_blank"
# after leftover Writers — consecutive Hidden _blank hung detect
# (GHA 34619751330) the same way as leftover swriter (34597506651).
_WINDOWS_NOTEBOOK_TARGET = "_wa_notebook"
# Notebook-runner host while leftover HTML-paste Writers stay open.
# Not leftover ``_wa_factory`` (reuses paste leftovers) and not
# import-filter ``_wa_notebook`` (GHA 34643210006 leftover listeners).
_WINDOWS_NOTEBOOK_HOST_TARGET = "_wa_notebook_host"
_WINDOWS_NOTEBOOK_HOST = False


def set_windows_notebook_host(on: bool) -> None:
    """Writer leftover reuse off; leftover factory uses ``_wa_notebook_host``."""
    flag = bool(on)
    for mod in _testing_utils_holders():
        mod._WINDOWS_NOTEBOOK_HOST = flag


def _windows_notebook_host() -> bool:
    if bool(_WINDOWS_NOTEBOOK_HOST):
        return True
    here = sys.modules.get(__name__)
    for mod in _testing_utils_holders():
        if mod is here:
            continue
        if bool(getattr(mod, "_WINDOWS_NOTEBOOK_HOST", False)):
            return True
    return False


def windows_notebook_load_args() -> tuple[str, int]:
    """Target + FrameSearchFlag for a Hidden Jupyter ``loadComponentFromURL``.

    GHA 34619751330 (``5377a87e``, ``test_draw_uno`` already deferred):
    leftover Math Draw had **not** run. ``test_import_filter_uno_load_component``
    Hidden ``_blank`` + FilterName returned, then raw ``close(True)``.
    ``test_import_filter_uno_detect_without_filtername`` Hidden ``_blank``
    hung 30s (soffice still ``2444,5124``). Same consecutive leftover
    Hidden ``_blank`` family as 34597506651. Use one CREATE|GLOBAL name
    and ``TestingFactory.close_doc`` (skips Writer close while leftovers
    remain). POSIX keeps ``_blank``.
    """
    if sys.platform != "win32":
        return "_blank", 0
    return _WINDOWS_NOTEBOOK_TARGET, _WINDOWS_FACTORY_SEARCH_FLAGS


def _reraise_native_open_failure(
    exc: BaseException, factory_url: str, pre_open: str = "no_probe"
) -> None:
    """Re-raise factory-open failures with the previous-test breadcrumb attached.

    URP ``DisposedException`` on ``loadComponentFromURL`` usually means the
    *previous* ``@with_native_doc`` close killed soffice/the bridge. Keep
    ``Binary URP bridge`` in the message so ``_is_uno_bridge_disposed`` still
    matches and the runner aborts remaining suites. ``pre_open`` is the cheap
    getServiceManager probe taken *before* load (alive vs already disposed).
    """
    from plugin.testing_runner import (
        _is_uno_bridge_disposed,
        _progress,
        format_lifecycle_breadcrumb,
    )

    crumb = format_lifecycle_breadcrumb()
    if _is_uno_bridge_disposed(exc):
        msg = (
            "create_native_doc loadComponentFromURL(%s) DisposedException / URP dead "
            "pre_open=%s (%s: %s) %s"
            % (factory_url, pre_open, type(exc).__name__, exc, crumb)
        )
        _progress("LIFECYCLE native_doc open FAIL %s" % msg)
        raise RuntimeError("Binary URP bridge disposed during call; %s" % msg) from exc
    raise


def _log_close_doc_failure(exc: BaseException) -> None:
    """Log (do not re-raise) a dispose during ``close_doc`` so the trail is named."""
    from plugin.testing_runner import (
        _is_uno_bridge_disposed,
        _progress,
        _soffice_pids,
        format_lifecycle_breadcrumb,
    )

    if not (_is_uno_bridge_disposed(exc) or type(exc).__name__ == "DisposedException"):
        return
    _progress(
        "LIFECYCLE close_doc dispose pids=%s %s"
        % (_soffice_pids(), format_lifecycle_breadcrumb())
    )


def _log_office_health_after_close(ctx, doc_type: str) -> None:
    """Harness-only: probe the desktop after close; log if the office is already dead.

    Draw/Impress never reuse a pooled doc, so each test closes. If that close
    (or a delayed crash) kills URP, the next factory open is the named victim.
    This probe prints at close time. It does not restart office or skip tests.
    """
    from plugin.testing_runner import (
        _is_uno_bridge_disposed,
        _progress,
        _soffice_pids,
        format_lifecycle_breadcrumb,
    )

    try:
        from plugin.framework.uno_context import get_desktop

        desktop = get_desktop(ctx)
        desktop.getComponents()
    except Exception as exc:
        if _is_uno_bridge_disposed(exc) or type(exc).__name__ == "DisposedException":
            _progress(
                "LIFECYCLE office dead after close doc_type=%s pids=%s %s"
                % (doc_type, _soffice_pids(), format_lifecycle_breadcrumb())
            )


def _default_native_doc_reuse(doc_type: str) -> bool:
    return doc_type == "calc"


# Pre-close URP settle after gc.collect() (see close_doc). Draw soak amplifier
# was duplicate_slide + held SvxShape proxies; Writer forms/charts/shapes share
# the SfxItemPool path. Measured 0/80 on the killer; post-close wait did not help.
_CLOSE_DOC_URP_SETTLE_S = 0.05

# GHA 34419828920 (master bf6c2ea2): peer test_peer_impress_rejected_on_resolved_model
# raw-closed Impress via doc.close(True), then the *next* test hung 30s in
# private:factory/swriter load (test_peer_message_uno.py:109). Office stayed
# alive (kill-libreoffice.ps1 still found soffice PIDs). close_doc's 50 ms is
# pre-close (release proxies before teardown). This is post-close: drop the
# closed Impress/Draw proxy, GC, then let URP finish before the next factory
# load. Do not put this inside close_doc — that would tax every Writer/Calc
# close. Windows needs longer; POSIX is a short drain. Not a product fix.
#
# GHA 34518091151 (#710 path): the hang moved *into* close_doc at Impress
# ``doc.close(True)`` (faulthandler 30s; office still alive). close_doc GCs
# leftover ``resolve_document_by_url`` wrappers then close()s after only
# 50 ms — too tight on Windows Draw-family. Pre-close settle uses the same
# Windows-longer budget; still not inside close_doc. POSIX only: Windows
# Draw-family close is a bare ``close(True)`` (see ``_draw_family_raw_close``).
_DRAW_FAMILY_POST_CLOSE_SETTLE_S = 0.75 if sys.platform == "win32" else 0.15
_DRAW_FAMILY_PRE_CLOSE_SETTLE_S = _DRAW_FAMILY_POST_CLOSE_SETTLE_S


def _draw_family_raw_close() -> bool:
    """True when Impress must be closed with a bare ``close(True)``.

    What the breadcrumbs showed:
    - GHA 34419828920 / #710: raw ``doc.close(True)`` (no pre-close GC)
      *returned* on Windows; the *next* Writer factory load then hung
      until ``settle_after_draw_family_close`` was added.
    - GHA 34518091151: ``close_doc`` (GC + 50 ms + ``close``) hung 30s
      *inside* Impress close.
    - GHA 34532953982 / 34535868114: GC + pre-close settle then
      ``close(True)`` / ``dispose()`` hung the same way.
    - GHA 34537826720: skipping close/dispose left Impress alive; the
      next ``private:factory/swriter`` load hung 30s.

    Skip is not a fix. Pre-close GC/sleep before close is the hung path.
    Raw close + post-close settle is the only sequence that both returned
    and (with #710's settle) was meant to unwedge the next Writer load.
    """
    return sys.platform == "win32"


def _draw_family_doc_label(doc) -> str:
    """Harness-only: impress / draw / unknown for close breadcrumbs."""
    try:
        # ``is True``: MagicMock.supportsService() is truthy and must not
        # look like Draw-family during unit tests (Windows pytest).
        if doc.supportsService("com.sun.star.presentation.PresentationDocument") is True:
            return "impress"
    except Exception:
        pass
    try:
        if doc.supportsService("com.sun.star.drawing.DrawingDocument") is True:
            return "draw"
    except Exception:
        pass
    return "unknown"


# plugin/draw/math_insert.py MATH_CLSID. close_doc of a Draw that still
# holds this OLE killed soffice (GHA 34607010446, exit 0).
_MATH_OLE_CLSID = "078B7ABA-54FC-457F-8551-6147e776a997"
_WINDOWS_MATH_OLE_UIDS: set[str] = set()


def mark_windows_math_ole_doc(doc) -> None:
    """Harness-only: this Draw/Impress still holds a Math OLE.

    GHA 34607010446: ``test_insert_math_draw`` body returned, then
    ``close_doc`` ``dispose`` of that Draw killed soffice (exit 0).
    Nine earlier Draw ``close_doc`` calls in the same suite survived.
    Remember the uid on both testing_utils copies so teardown can skip
    the close. The runner then defers ``test_draw_uno`` until just
    before the peer suite so this leftover is not ``close(True)``'d
    (34607010446). Notebook detect hang is leftover Hidden ``_blank``
    (34619751330), not this Draw. Not a product fix.
    """
    if sys.platform != "win32" or not doc:
        return
    try:
        uid = str(getattr(doc, "RuntimeUID", None) or "")
    except Exception:
        uid = ""
    if not uid:
        return
    for mod in _testing_utils_holders():
        uids = getattr(mod, "_WINDOWS_MATH_OLE_UIDS", None)
        if uids is None:
            mod._WINDOWS_MATH_OLE_UIDS = set()
            uids = mod._WINDOWS_MATH_OLE_UIDS
        uids.add(uid)


def _windows_math_ole_uids() -> set[str]:
    uids = set(_WINDOWS_MATH_OLE_UIDS)
    here = sys.modules.get(__name__)
    for mod in _testing_utils_holders():
        if mod is here:
            continue
        other = getattr(mod, "_WINDOWS_MATH_OLE_UIDS", None)
        if other:
            uids.update(other)
    return uids


def _clear_windows_math_ole_uids() -> None:
    """Unit-test reset. ``mark_windows_math_ole_doc`` writes both copies."""
    _WINDOWS_MATH_OLE_UIDS.clear()
    here = sys.modules.get(__name__)
    for mod in _testing_utils_holders():
        if mod is here:
            continue
        other = getattr(mod, "_WINDOWS_MATH_OLE_UIDS", None)
        if other is not None:
            other.clear()


def _draw_doc_has_math_ole(doc) -> bool:
    """True when a Draw/Impress page still has a Math OLE2Shape."""
    if not doc:
        return False
    try:
        pages = doc.getDrawPages()
        n_pages = pages.getCount()
    except Exception:
        return False
    try:
        page_count = int(n_pages)
    except (TypeError, ValueError):
        return False
    for i in range(page_count):
        try:
            page = pages.getByIndex(i)
            n_shapes = int(page.getCount())
        except Exception:
            continue
        for j in range(n_shapes):
            try:
                shape = page.getByIndex(j)
                clsid = str(getattr(shape, "CLSID", "") or "")
            except Exception:
                continue
            if clsid == _MATH_OLE_CLSID:
                return True
    return False


def _windows_should_skip_math_ole_close(uid: str = "") -> bool:
    """True when Windows must not ``close_doc`` this marked Math OLE uid.

    What was wrong: GHA 34607010446 (master ``3720c175``) closed a Draw
    after ``insert_math`` and soffice exited 0. GHA 34612145495 then
    died on the *first* Draw forms ``close(True)`` after this helper
    walked pages/shapes and ``_native_doc_svc`` probed the model.
    Master closed ordinary Draw docs without that extra UNO.

    How: skip only when ``mark_windows_math_ole_doc`` recorded the uid.
    Do not walk the document. Unmarked Draw close stays GC + 50 ms +
    ``close(True)``. Not a product fix.
    """
    if sys.platform != "win32" or not uid:
        return False
    return uid in _windows_math_ole_uids()


def close_draw_family_doc(doc):
    """Harness-only Impress/Draw close. Does **not** go through ``close_doc``.

    What was wrong: GHA 34518091151 hung 30s in ``TestingFactory.close_doc``
    at ``doc.close(True)`` while tearing down Impress in
    ``test_peer_impress_rejected_on_resolved_model`` (Writer still open;
    both factory loads had succeeded). #710's post-close settle never ran.
    Skipping close/dispose (34537826720) unblocked that teardown, then the
    *next* ``private:factory/swriter`` load hung 30s — leftover Impress
    poisons later Writer factory loads (same family as #710).

    How: ``close_doc`` GCs leftover peer-resolve wrappers then close()s
    after 50 ms. On Windows, GC + sleep *immediately before* ``close`` /
    ``dispose`` blocks the UI thread (34532953982 / 34535868114). A bare
    ``close(True)`` with no pre-close GC is the only Impress close that
    has returned on Windows (#710 / 34419828920).

    Why this: Windows calls ``close(True)`` with no setModified / GC /
    sleep in front of it, then callers drop the proxy and
    ``settle_after_draw_family_close``. POSIX still marks unmodified, GC,
    pre-close settle, then ``close(True)``. Peer tests keep Writer open
    across the Windows raw close (the #710 returning order) and
    re-activate it before closing Writer. Logs svc/uid and each step.
    Not a product fix.
    """
    if not doc:
        return
    from plugin.testing_runner import _progress

    svc = _draw_family_doc_label(doc)
    uid = "?"
    try:
        uid = str(getattr(doc, "RuntimeUID", None) or "?")
    except Exception:
        uid = "?"
    _progress("close_draw_family: start svc=%s uid=%s" % (svc, uid))
    if _draw_family_raw_close():
        # Do not GC or sleep here — that is the hung close/dispose path.
        _progress("close_draw_family: raw close(True) start svc=%s uid=%s" % (svc, uid))
        try:
            if hasattr(doc, "close"):
                doc.close(True)
            elif hasattr(doc, "dispose"):
                doc.dispose()
        except Exception as exc:
            _log_close_doc_failure(exc)
            _progress(
                "close_draw_family: raw close failed svc=%s uid=%s err=%s"
                % (svc, uid, type(exc).__name__)
            )
        else:
            _progress("close_draw_family: raw close(True) done svc=%s uid=%s" % (svc, uid))
        return
    try:
        if hasattr(doc, "setModified"):
            doc.setModified(False)
            _progress("close_draw_family: setModified(False) ok svc=%s uid=%s" % (svc, uid))
    except Exception as exc:
        _progress(
            "close_draw_family: setModified skipped svc=%s uid=%s err=%s"
            % (svc, uid, type(exc).__name__)
        )
    import gc
    import time

    gc.collect()
    _progress(
        "close_draw_family: gc done; sleep %.2fs svc=%s uid=%s"
        % (_DRAW_FAMILY_PRE_CLOSE_SETTLE_S, svc, uid)
    )
    time.sleep(_DRAW_FAMILY_PRE_CLOSE_SETTLE_S)
    _progress("close_draw_family: close(True) start svc=%s uid=%s" % (svc, uid))
    try:
        if hasattr(doc, "close"):
            doc.close(True)
        elif hasattr(doc, "dispose"):
            doc.dispose()
    except Exception as exc:
        _log_close_doc_failure(exc)
        _progress(
            "close_draw_family: close failed svc=%s uid=%s err=%s"
            % (svc, uid, type(exc).__name__)
        )
    else:
        _progress("close_draw_family: close(True) done svc=%s uid=%s" % (svc, uid))


def settle_after_draw_family_close() -> None:
    """Harness-only: GC + sleep after closing Impress/Draw before the next factory load.

    Call after ``close_draw_family_doc`` *and* after dropping the local
    reference. The next ``loadComponentFromURL`` is the hang site if this
    settle is skipped *or* if Impress was never actually closed
    (GHA 34537826720 skip-teardown; see
    ``tests/chatbot/test_peer_message_uno.py``).
    """
    import gc
    import time

    gc.collect()
    time.sleep(_DRAW_FAMILY_POST_CLOSE_SETTLE_S)


# offapi/com/sun/star/sheet/CellFlags.idl — VALUE|DATETIME|STRING|ANNOTATION|FORMULA|HARDATTR|STYLES|OBJECTS|EDITATTR|FORMATTED
_CALC_CLEAR_ALL = 1 | 2 | 4 | 8 | 16 | 32 | 64 | 128 | 256 | 512


def _native_doc_alive(doc) -> bool:
    try:
        doc.getCurrentController()
        return True
    except Exception:
        return False


def _clear_named_container(container) -> None:
    if container is None or not hasattr(container, "getElementNames"):
        return
    for name in list(container.getElementNames()):
        try:
            container.removeByName(name)
        except Exception:
            pass


def _clear_undo(doc) -> None:
    try:
        mgr = doc.getUndoManager()
        if mgr is not None:
            mgr.clear()
    except Exception:
        pass


def _remove_all_calc_charts(doc) -> None:
    sheets = doc.getSheets()
    for i in range(sheets.getCount()):
        try:
            charts = sheets.getByIndex(i).getCharts()
            for name in list(charts.getElementNames()):
                try:
                    charts.removeByName(name)
                except Exception:
                    pass
        except Exception:
            pass
    try:
        objs = doc.getEmbeddedObjects()
        for name in list(objs.getElementNames()):
            try:
                objs.removeByName(name)
            except Exception:
                pass
    except Exception:
        pass


def _probe_soffice_before_udprops(doc, _ctx=None) -> None:
    """Name whether the Calc model is already wedged before the udprop write.

    GHA 33763078357: sheet-level reset returned, then getDocumentProperties blocked.
    GHA 33771766524 / 33772063173: #572's desktop.getComponents probe became
    the Windows hang site after close+paste (deterministic). Product fix is in
    rich_html.py (reuse Writer, do not enumerate after close). Test harness
    keeps only a cheap RuntimeUID attribute read here.
    """
    _native_teardown_progress("udprops probe: RuntimeUID start")
    try:
        uid = getattr(doc, "RuntimeUID", None)
        _native_teardown_progress("udprops probe: RuntimeUID done uid=%s" % uid)
    except Exception as exc:
        _native_teardown_progress("udprops probe: RuntimeUID failed %r" % (exc,))


def _clear_writeragent_udprops(doc, ctx=None) -> None:
    # GHA 33707990007: after insert_cell_html, Windows hung in
    # set_document_scripts → is_document_readonly_for_scripts → doc.isReadonly()
    # during calc native-doc wipe (remove_charts + clearContents had already
    # returned). A harness wipe is never a user-readonly save; write the
    # scripts UDProp the same way as the session ids so we never call
    # isReadonly(). Empty string is missing to get_document_scripts.
    # GHA 33763078357: that workaround relocated the hang to
    # getDocumentProperties() — do not skip this write; probe first, then
    # trace each UNO step inside set_document_property.
    from plugin.doc import udprops as udprops_mod

    prev_trace = udprops_mod._TRACE_UDPROPS
    # Keep suite noise low: desktop/clipboard probe + per-UNO-step stderr
    # only when with_native_doc armed teardown logs (test_insert_cell_html).
    if _LOG_NATIVE_DOC_TEARDOWN:
        if ctx is not None:
            _probe_soffice_before_udprops(doc, ctx)
        udprops_mod._TRACE_UDPROPS = True
    try:
        from plugin.scripting.document_scripts import DOCUMENT_SCRIPTS_UDPROP
        from plugin.scripting.session_manager import PYTHON_WORKBOOK_SESSION_PROP

        _native_teardown_progress("udprops clear: DOCUMENT_SCRIPTS start")
        udprops_mod.set_document_property(doc, DOCUMENT_SCRIPTS_UDPROP, "")
        _native_teardown_progress("udprops clear: DOCUMENT_SCRIPTS done")
        udprops_mod.set_document_property(doc, PYTHON_WORKBOOK_SESSION_PROP, "")
        udprops_mod.set_document_property(doc, "WriterAgentSessionID", "")
        _native_teardown_progress("udprops clear: all done")
    except Exception:
        pass
    finally:
        udprops_mod._TRACE_UDPROPS = prev_trace


def _reset_calc_doc(doc, ctx) -> None:  # ctx unused; same signature as writer reset
    _native_teardown_progress("native_doc: _reset_calc_doc start")
    _native_teardown_progress("native_doc: _reset_calc_doc remove_charts start")
    _remove_all_calc_charts(doc)
    _native_teardown_progress("native_doc: _reset_calc_doc remove_charts done")
    sheets = doc.getSheets()
    while sheets.getCount() > 1:
        name = sheets.getByIndex(sheets.getCount() - 1).Name
        sheets.removeByName(name)
    sheet = sheets.getByIndex(0)
    try:
        if sheet.Name != "Sheet1":
            sheet.setName("Sheet1")
    except Exception:
        pass
    try:
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents start")
        cursor = sheet.createCursor()
        cursor.gotoStartOfUsedArea(False)
        cursor.gotoEndOfUsedArea(True)
        try:
            cursor.merge(False)
        except Exception:
            pass
        cursor.clearContents(_CALC_CLEAR_ALL)
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents done")
    except Exception:
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents fallback start")
        sheet.getCellRangeByName("A1:AMJ1048576").clearContents(_CALC_CLEAR_ALL)
        _native_teardown_progress("native_doc: _reset_calc_doc clearContents fallback done")
    _clear_named_container(getattr(doc, "NamedRanges", None))
    try:
        _clear_named_container(sheet.NamedRanges)
    except Exception:
        pass
    _clear_named_container(getattr(doc, "DatabaseRanges", None))
    try:
        import uno

        settings = doc.getNumberFormatSettings()
        nd = uno.createUnoStruct("com.sun.star.util.Date")
        nd.Year, nd.Month, nd.Day = 1899, 12, 30
        settings.setPropertyValue("NullDate", nd)
    except Exception:
        pass
    try:
        controller = doc.getCurrentController()
        controller.setActiveSheet(sheet)
        controller.select(sheet.getCellByPosition(0, 0))
        _native_teardown_progress("native_doc: _reset_calc_doc select done")
    except Exception:
        pass
    _clear_writeragent_udprops(doc, ctx)
    _clear_undo(doc)
    _native_teardown_progress("native_doc: _reset_calc_doc done")


def _reset_writer_style_families(doc) -> None:
    """Drop user HTML styles and restore built-in CharWeight (Standard can pick up bold)."""
    try:
        families = doc.getStyleFamilies()
    except Exception:
        return
    for family_name in ("ParagraphStyles", "CharacterStyles"):
        try:
            styles = families.getByName(family_name)
        except Exception:
            continue
        for name in list(styles.getElementNames()):
            try:
                style = styles.getByName(name)
            except Exception:
                continue
            try:
                if bool(style.isUserDefined()):
                    styles.removeByName(name)
                    continue
            except Exception:
                pass
            for prop in ("CharWeight", "CharHeight", "CharPosture", "CharUnderline", "CharColor"):
                try:
                    style.setPropertyToDefault(prop)
                except Exception:
                    pass


def _writer_pool_is_clean(doc) -> bool:
    """False if wipe left text, bold, or graphics — caller should factory-load."""
    try:
        if (doc.getText().getString() or "").strip():
            return False
        cursor = doc.getText().createTextCursor()
        cursor.gotoStart(False)
        if float(cursor.getPropertyValue("CharWeight") or 100) >= 135.0:
            return False
        if hasattr(doc, "getGraphicObjects") and doc.getGraphicObjects().getCount() > 0:
            return False
    except Exception:
        return False
    return True


def _reset_writer_doc(doc, ctx) -> None:
    try:
        doc.setPropertyValue("RecordChanges", False)
    except Exception:
        pass
    try:
        smgr = ctx.getServiceManager()
        helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        frame = doc.getCurrentController().getFrame()
        helper.executeDispatch(frame, ".uno:AcceptAllTrackedChanges", "", 0, ())
    except Exception:
        pass
    try:
        text = doc.getText()
        cursor = text.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        cursor.setString("")
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        # Empty para keeps last run CharWeight/Heading; HTML insert at "end" with
        # apply_styles=False then paints body text with leftover bold (150).
        try:
            cursor.setPropertyValue("ParaStyleName", "Standard")
        except Exception:
            pass
        try:
            cursor.setPropertyValue("CharWeight", 100.0)
        except Exception:
            pass
        for prop in (
            "CharStyleName",
            "CharWeight",
            "CharHeight",
            "CharPosture",
            "CharUnderline",
            "CharColor",
            "CharBackColor",
            "CharEscapement",
            "CharFontName",
            "ParaAdjust",
        ):
            try:
                cursor.setPropertyToDefault(prop)
            except Exception:
                pass
        cursor.gotoStart(False)
        try:
            doc.getCurrentController().select(cursor)
        except Exception:
            pass
    except Exception:
        pass
    try:
        enum = doc.getText().createEnumeration()
        while enum.hasMoreElements():
            para = enum.nextElement()
            try:
                para.setPropertyValue("ParaStyleName", "Standard")
            except Exception:
                pass
            try:
                para.setPropertyValue("CharWeight", 100.0)
            except Exception:
                pass
            for prop in ("CharStyleName", "CharWeight", "CharHeight", "CharPosture"):
                try:
                    para.setPropertyToDefault(prop)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        smgr = ctx.getServiceManager()
        helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        frame = doc.getCurrentController().getFrame()
        helper.executeDispatch(frame, ".uno:SelectAll", "", 0, ())
        helper.executeDispatch(frame, ".uno:ResetAttributes", "", 0, ())
        cursor = doc.getText().createTextCursor()
        cursor.gotoStart(False)
        doc.getCurrentController().select(cursor)
    except Exception:
        pass
    for getter in ("getTextTables", "getTextFrames", "getGraphicObjects", "getEmbeddedObjects", "getTextSections"):
        if not hasattr(doc, getter):
            continue
        try:
            container = getattr(doc, getter)()
            for name in list(container.getElementNames()):
                try:
                    content = container.getByName(name)
                    doc.getText().removeTextContent(content)
                except Exception:
                    try:
                        container.getByName(name).dispose()
                    except Exception:
                        pass
        except Exception:
            pass
    _reset_writer_style_families(doc)
    _clear_undo(doc)


def reset_native_doc(doc, doc_type: str, ctx) -> None:
    """Wipe a Writer or Calc document so the next native test can reuse it."""
    if doc_type == "calc":
        _reset_calc_doc(doc, ctx)
    elif doc_type == "writer":
        _reset_writer_doc(doc, ctx)
    else:
        raise ValueError("reset_native_doc only supports writer and calc")


class TestingFactory:
    """Unified factory for creating test documents and contexts."""

    @staticmethod
    def create_doc(env="mock", doc_type="writer", content=None, **kwargs):
        """Create a mock document stub (or raise for native — use create_native_doc).

        - ``calc`` → :class:`CalcDocStub` (prefer ``data=`` 2D grid)
        - otherwise → :class:`WriterDocStub` (``content=`` paragraph list, ``items=`` style families)
        """
        if env == "native":
            raise NotImplementedError("Native doc creation requires a ctx. Use create_native_doc(ctx, ...)")

        if doc_type == "calc":
            calc_kwargs = dict(kwargs)
            if "data" not in calc_kwargs and content is not None and not isinstance(content, list):
                calc_kwargs["data"] = content
            return CalcDocStub(**calc_kwargs)

        elements = content if isinstance(content, list) else []
        return WriterDocStub(elements, doc_type=doc_type, **kwargs)

    @staticmethod
    def create_native_doc(ctx, doc_type="writer", hidden=True):
        """Creates a real hidden document in LibreOffice.

        On URP ``DisposedException``, logs the previous native test (and PIDs)
        then re-raises a ``RuntimeError`` that still matches
        ``_is_uno_bridge_disposed`` so the runner names the *previous* test,
        not only this factory open. See ``docs/framework/uno-test-lifecycle.md``.
        """
        from plugin.framework.uno_context import get_desktop
        import uno

        desktop = get_desktop(ctx)
        props = []
        if hidden:
            props.append(uno.createUnoStruct("com.sun.star.beans.PropertyValue", Name="Hidden", Value=True))
        
        if doc_type.startswith("private:") or doc_type.startswith("file://"):
            factory_url = doc_type
        else:
            factory_url = {
                "writer": "private:factory/swriter",
                "calc": "private:factory/scalc",
                "draw": "private:factory/sdraw",
                "impress": "private:factory/simpress"
            }.get(doc_type, "private:factory/swriter")

        from plugin.testing_runner import probe_uno_bridge

        leftover_open = 0
        target, flags = "_blank", 0
        # GHA 34593327841 / 34599838644 / 34602219973 / 34633295036:
        # leftover paste Writers as desktop current hung the next factory
        # (30s). #720 only prepared swriter. Reactivate the keeper.
        # Leftover swriter reuses one CREATE|GLOBAL name (_wa_factory).
        # Leftover Calc reuses _wa_scalc (unique _wa_factory_N failed
        # then hung, 34633295036). Draw/Impress stay unique.
        if sys.platform == "win32" and factory_url.startswith("private:factory/"):
            leftover_open = prepare_windows_writer_factory(ctx)
            target, flags = _windows_factory_load_args(factory_url, leftover_open)
            from plugin.testing_runner import _progress

            _progress(
                "create_native_doc: windows factory leftover_open=%s url=%s target=%s flags=%s"
                % (leftover_open, factory_url, target, flags)
            )

        # Distinguish "bridge already dead" (previous test) from "died during load".
        pre_open = probe_uno_bridge(ctx)
        if pre_open == "disposed":
            _reraise_native_open_failure(
                RuntimeError("Binary URP bridge already disposed before loadComponentFromURL"),
                factory_url,
                pre_open=pre_open,
            )
            raise
        try:
            if sys.platform == "win32" and leftover_open:
                from plugin.testing_runner import _progress

                _progress(
                    "create_native_doc: load start url=%s target=%s flags=%s"
                    % (factory_url, target, flags)
                )
            doc = desktop.loadComponentFromURL(factory_url, target, flags, tuple(props))
            if sys.platform == "win32" and leftover_open:
                from plugin.testing_runner import _progress

                uid = ""
                try:
                    uid = str(getattr(doc, "RuntimeUID", None) or "")
                except Exception:
                    uid = ""
                _progress(
                    "create_native_doc: load done url=%s target=%s uid=%s"
                    % (factory_url, target, uid or "-")
                )
        except Exception as exc:
            _reraise_native_open_failure(exc, factory_url, pre_open=pre_open)
            raise
        return doc

    @staticmethod
    def close_doc(doc):
        """Safely closes a document instance if available."""
        if not doc:
            return
        # Skip leftover-window Writer close *before* dropping the pool entry
        # so reuse can still find the doc (34602219973).
        uid = ""
        is_writer = False
        leftover_open = 0
        if sys.platform == "win32":
            try:
                uid = str(getattr(doc, "RuntimeUID", None) or "")
            except Exception:
                uid = ""
            # GHA 34612145495: _native_doc_svc + page walk before the first
            # Draw forms close(True) killed soffice (exit 0). Master
            # 34607010446 closed that same forms Draw. Check the Math
            # mark from RuntimeUID only — no supportsService / getDrawPages.
            if _windows_should_skip_math_ole_close(uid):
                from plugin.testing_runner import _progress, _soffice_pids

                leftover_open = _windows_leftover_open()
                reactivate_harness_keeper()
                _progress(
                    "close_doc: skip math ole close (windows) uid=%s "
                    "leftovers=%s keeper=%s pids=%s"
                    % (
                        uid or "-",
                        leftover_open,
                        _HARNESS_KEEPER_UID or "-",
                        _soffice_pids(),
                    )
                )
                return
            try:
                is_writer = bool(
                    doc.supportsService("com.sun.star.text.TextDocument") is True
                )
            except Exception:
                is_writer = False
            leftover_open = _windows_leftover_open()
            if is_writer:
                from plugin.testing_runner import _progress

                _progress(
                    "close_doc: start uid=%s leftovers=%s" % (uid or "-", leftover_open)
                )
                if leftover_open > 0:
                    # GHA 34602219973: close uid=34 returned; next unique
                    # _wa_factory_5 swriter hung 30s. Do not close a
                    # harness Writer while paste leftovers remain (same
                    # ban as leftover paste close, 34556185752).
                    # GHA 34643210006: that skip also kept import-filter
                    # ``_wa_notebook`` leftovers (uids 41/42) and their
                    # form listeners. Close notebook-registry leftovers.
                    # Do not close leftover paste Writers.
                    close_notebook = False
                    try:
                        from plugin.notebook.cell_registry import (
                            has_notebook_registry,
                        )

                        close_notebook = has_notebook_registry(doc) is True
                    except Exception:
                        close_notebook = False
                    if not close_notebook:
                        reactivate_harness_keeper()
                        _progress(
                            "close_doc: skip writer close leftovers open=%s uid=%s"
                            % (leftover_open, uid or "-")
                        )
                        return
                    _progress(
                        "close_doc: close notebook leftover uid=%s leftovers=%s"
                        % (uid or "-", leftover_open)
                    )
        for key, pooled in list(_NATIVE_DOC_POOL.items()):
            if pooled is doc:
                del _NATIVE_DOC_POOL[key]
        try:
            from plugin.scripting.session_manager import clear_active_calc_session

            clear_active_calc_session()
        except Exception:
            pass
        try:
            import gc
            import time

            # Release PyUNO sequences before Calc tears down the document.
            # Large getDataArray results held across close can abort soffice (glibc double-free).
            gc.collect()
            # What was wrong: Draw close raced URP ~SvxShape / SdrRectObj with
            # SfxItemPool::unregisterNameOrIndex (SalAbort after the test returned).
            # How: Python still held page/shape proxies; close tore the model down
            # while cppu_threadpool released the wrappers. Why this: GC then a short
            # settle lets ~SvxShape finish before close. Unscoped: Writer
            # ControlShape / charts use the same pool. Post-close wait did not help.
            time.sleep(_CLOSE_DOC_URP_SETTLE_S)
            if hasattr(doc, "close"):
                doc.close(True)
            elif hasattr(doc, "dispose"):
                doc.dispose()
            if sys.platform == "win32" and is_writer:
                from plugin.testing_runner import _progress

                # Test Writer close returned (34554275072). Leftover close
                # hangs (34556185752). Reactivate keeper so the next factory
                # is not against a leftover paste Writer as current.
                reactivate_harness_keeper()
                _progress("close_doc: done uid=%s" % (uid or "-"))
        except Exception as exc:
            # Harness-only: close used to swallow DisposedException, so the
            # *next* factory open became the named victim. Log the trail here.
            _log_close_doc_failure(exc)


    @staticmethod
    @contextlib.contextmanager
    def native_doc(ctx, doc_type="writer", hidden=True, reuse=None):
        """Yield a native LO document. Calc defaults to experimental wipe-and-reuse; Writer does not."""
        if reuse is None:
            reuse = _default_native_doc_reuse(doc_type)
            if doc_type == "writer" and _windows_should_reuse_writer(ctx):
                from plugin.testing_runner import _progress

                reuse = True
                _progress("native_doc: leftover writer reuse")
        # reuse=False still calls create_native_doc. Leftover _wa_scalc
        # hung 30s (34643210006). Wipe-and-reuse the pooled Calc instead.
        if doc_type == "calc" and not reuse and _windows_should_reuse_calc(ctx):
            from plugin.testing_runner import _progress

            reuse = True
            _progress("native_doc: leftover calc reuse")
        use_pool = bool(reuse) and doc_type in ("writer", "calc")
        doc = None
        pooled = False
        if use_pool:
            key = (id(ctx), doc_type, bool(hidden))
            candidate = _NATIVE_DOC_POOL.get(key)
            if candidate is not None and _native_doc_alive(candidate):
                try:
                    reset_native_doc(candidate, doc_type, ctx)
                    if doc_type == "writer" and not _writer_pool_is_clean(candidate):
                        if sys.platform == "win32" and _windows_leftover_open() > 0:
                            from plugin.testing_runner import _progress

                            # close + factory hangs (34602219973). Wipe again
                            # and keep the leftover-window Writer.
                            _progress("native_doc: leftover writer pool dirty; keep")
                            reset_native_doc(candidate, doc_type, ctx)
                            doc = candidate
                            pooled = True
                        else:
                            TestingFactory.close_doc(candidate)
                            doc = None
                    else:
                        doc = candidate
                        pooled = True
                except Exception:
                    if sys.platform == "win32" and _windows_leftover_open() > 0:
                        from plugin.testing_runner import _progress

                        _progress("native_doc: leftover writer reset failed; keep")
                        doc = candidate
                        pooled = True
                    else:
                        TestingFactory.close_doc(candidate)
                        doc = None
            if doc is None:
                doc = TestingFactory.create_native_doc(ctx, doc_type=doc_type, hidden=hidden)
                _NATIVE_DOC_POOL[key] = doc
                pooled = True
        else:
            doc = TestingFactory.create_native_doc(ctx, doc_type=doc_type, hidden=hidden)
        if doc_type == "calc" and doc is not None:
            try:
                from plugin.scripting.session_manager import calc_workbook_base_session_id

                calc_workbook_base_session_id(doc)
            except Exception:
                pass
        try:
            yield doc
        finally:
            if pooled:
                # Wipe before leaving the pool: tests without @with_native_doc still
                # see this document as the desktop's current component (init scripts, charts).
                _native_teardown_progress(
                    "native_doc: teardown reset start doc_type=%s" % doc_type
                )
                try:
                    reset_native_doc(doc, doc_type, ctx)
                except Exception:
                    _native_teardown_progress(
                        "native_doc: teardown reset failed; close_doc"
                    )
                    TestingFactory.close_doc(doc)
                    return
                _native_teardown_progress("native_doc: teardown reset done")
                try:
                    from plugin.scripting.session_manager import clear_active_calc_session

                    clear_active_calc_session()
                except Exception:
                    pass
            else:
                _native_teardown_progress("native_doc: teardown close_doc start")
                TestingFactory.close_doc(doc)
                # Harness probe (not a product fix): if close toasted URP, name
                # it now instead of waiting for the next loadComponentFromURL.
                _log_office_health_after_close(ctx, doc_type)
                _native_teardown_progress("native_doc: teardown close_doc done")



    @staticmethod
    def create_context(doc=None, ctx=None, env="mock", doc_type="writer", services=None, **ctx_kwargs):
        """Create a ToolContext for mock or native tests.

        Mock: builds a stub doc via :meth:`create_doc` when ``doc`` is omitted.
        Native: requires an existing ``doc`` (compose with ``@with_native_doc`` /
        :meth:`native_doc`); does not open documents itself.

        Pass ``services=`` to use the live plugin registry (``get_services()``) instead
        of a fresh ``ServiceRegistry``. Extra ``ctx_kwargs`` go to ``ToolContext``
        (e.g. ``status_callback``, ``active_page_index``).
        """
        from plugin.framework.tool import ToolContext
        from plugin.framework.service import ServiceRegistry

        if env == "mock":
            if doc is None:
                doc = TestingFactory.create_doc(env="mock", doc_type=doc_type)
            if ctx is None:
                ctx = MockContext()
            if services is None:
                services = ServiceRegistry()
            return ToolContext(doc=doc, ctx=ctx, doc_type=doc_type, services=services, caller="test", **ctx_kwargs)

        # Native env — caller owns document lifecycle (@with_native_doc).
        if doc is None:
            raise ValueError("create_context(env='native') requires doc= (use @with_native_doc)")
        if services is None:
            from plugin.doc.document_helpers import DocumentService
            from plugin.framework.event_bus import EventBus
            services = ServiceRegistry()
            services.register("document", DocumentService())
            services.register("events", EventBus())

        return ToolContext(doc=doc, ctx=ctx, doc_type=doc_type, services=services, caller="test", **ctx_kwargs)

    @staticmethod
    def execute_tool(doc, ctx, name, args=None, *, doc_type="calc", services=None, **ctx_kwargs):
        """Run a registered tool against a live (or stub) document.

        Defaults to ``get_services()`` so native Calc/Draw suites share one path.
        ``KeyError`` / ``ValueError`` from the registry become
        ``{"status": "error", "error": ...}`` (same contract as the old per-file helpers).
        """
        from plugin.main import get_tools, get_services

        if services is None:
            services = get_services()
        tctx = TestingFactory.create_context(
            doc=doc,
            ctx=ctx,
            env="native",
            doc_type=doc_type,
            services=services,
            **ctx_kwargs,
        )
        try:
            return get_tools().execute(name, tctx, **(args or {}))
        except (KeyError, ValueError) as e:
            return {"status": "error", "error": str(e)}


def with_native_doc(doc_type="writer", hidden=True, reuse=None):
    """Decorator to inject a native LibreOffice document into a test function and guarantee teardown.

    Calc: experimental wipe-and-reuse of one hidden spreadsheet (faster than factory+close).
    Writer: factory load/close by default (reuse leaks HTML/CharWeight). Pass reuse=True to try pooling.
    Draw/Impress never reuse. Factory-open DisposedException is annotated with
    the previous native test (docs/framework/uno-test-lifecycle.md).
    """
    def decorator(func):
        import functools
        import inspect

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Resolve ctx from args if present (native_test functions may receive ctx as first arg or kwargs)
            ctx = kwargs.get("ctx", None)
            if ctx is None and len(args) > 0:
                ctx = args[0]

            # GHA 33703959362: no TEST end after execute-done. Enter/exit here
            # splits a body hang from wipe-and-reuse teardown after return.
            log_teardown = func.__name__ == "test_insert_cell_html"
            global _LOG_NATIVE_DOC_TEARDOWN
            prev_teardown_log = _LOG_NATIVE_DOC_TEARDOWN
            if log_teardown:
                _LOG_NATIVE_DOC_TEARDOWN = True
                from plugin.testing_runner import _progress

                _progress(
                    "with_native_doc: enter name=%s doc_type=%s" % (func.__name__, doc_type)
                )
            try:
                with TestingFactory.native_doc(ctx, doc_type=doc_type, hidden=hidden, reuse=reuse) as doc:
                    sig = inspect.signature(func)
                    call_kwargs = {}
                    # Inject by parameter name so ctx is never dropped when doc is added.
                    if "ctx" in sig.parameters:
                        call_kwargs["ctx"] = ctx
                    if "doc" in sig.parameters:
                        call_kwargs["doc"] = doc
                    if call_kwargs:
                        result = func(**call_kwargs)
                    elif len(sig.parameters) == 1:
                        result = func(doc)
                    else:
                        result = func(*args, **kwargs)
                    if log_teardown:
                        from plugin.testing_runner import _progress

                        _progress(
                            "with_native_doc: body returned name=%s; teardown start"
                            % func.__name__
                        )
                    return result
            finally:
                _LOG_NATIVE_DOC_TEARDOWN = prev_teardown_log
                if log_teardown:
                    from plugin.testing_runner import _progress

                    _progress("with_native_doc: teardown done name=%s" % func.__name__)
        return wrapper
    return decorator

def create_mock_client():
    """Creates a pre-configured MagicMock for an LlmClient."""
    from unittest.mock import MagicMock
    mock_client = MagicMock()
    mock_client.config = MagicMock()
    mock_client.config.get.return_value = False
    return mock_client

def create_mock_http_response(
    status_code=200,
    json_data=None,
    *,
    reason=None,
    body=None,
    sse_lines=None,
    iter_side_effect=None,
    headers=None,
):
    """Mock ``http.client.HTTPResponse`` for pytest (no UNO, no live HTTP).

    * ``json_data`` / ``body`` feed sync ``response.read()``.
    * ``sse_lines`` feeds ``for line in response`` / ``iterate_sse`` (bytes or str).
    * ``iter_side_effect`` is raised after those lines (timeout / connection reset
      mid-stream). HTTP 4xx/5xx use ``status`` + ``reason`` + body. ``LlmClient``
      retries 429/503 up to three total attempts with backoff; other statuses raise immediately.
    """
    from unittest.mock import MagicMock
    import http.client
    import json

    mock_resp = MagicMock()
    mock_resp.status = status_code
    mock_resp.reason = (
        reason if reason is not None else http.client.responses.get(status_code, "")
    )
    header_map = dict(headers or {})

    def _getheader(name, default=None):
        return header_map.get(name, header_map.get(str(name).lower(), default))

    mock_resp.getheader.side_effect = _getheader

    if body is None and json_data is not None:
        body = json.dumps(json_data).encode("utf-8")
    mock_resp.read.return_value = b"" if body is None else body

    lines = []
    if sse_lines is not None:
        for line in sse_lines:
            if isinstance(line, str):
                line = line.encode("utf-8")
            if not line.endswith(b"\n"):
                line = line + b"\n"
            lines.append(line)

    if iter_side_effect is not None:
        def _iter():
            yield from lines
            raise iter_side_effect

        # return_value (not side_effect): ``for line in response`` matches
        # existing LlmClient tests that set ``__iter__.return_value = iter(...)``.
        mock_resp.__iter__.return_value = _iter()
    else:
        mock_resp.__iter__.return_value = iter(lines)
    return mock_resp