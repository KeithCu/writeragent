# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: nonecheck=False
# cython: initializedcheck=False
# cython: cdivision=True
# cython: overflowcheck=False
# cython: always_failsafe=False
# WriterAgent - Cython accelerator for serialization packing.

import array
from libc.math cimport NAN as c_nan
from cpython.list cimport PyList_GET_ITEM, PyList_GET_SIZE
from cpython.float cimport PyFloat_AsDouble, PyFloat_Check
from cpython.long cimport PyLong_AsLong, PyLong_Check
from cpython.bool cimport PyBool_Check

cdef extern from "Python.h":
    object PyObject_Str(object)
    int PyUnicode_Check(object)


cdef inline void _update_column_state(list column_states, int c, object val):
    # Column state, shared with payload_codec._flatten_update_column_state.
    # Do not merge the Python and Cython loops.
    #   0 empty, 1 bool, 2 int, 3 float.
    #   bool then int -> int (True and 1 are both 1.0 in the buffer).
    #   any float -> float, and float stays float.
    #   object / Decimal / other numeric that already became a C double -> float.
    cdef int st = <int>column_states[c]
    cdef object dtype
    cdef object kind

    if st == 3:
        return
    if PyFloat_Check(val):
        column_states[c] = 3
    elif PyBool_Check(val):
        if st == 0:
            column_states[c] = 1
    elif PyLong_Check(val):
        if st < 2:
            column_states[c] = 2
    else:
        # NumPy scalars and other numeric-like values reach this path after PyFloat_AsDouble succeeds.
        dtype = getattr(val, "dtype", None)
        if dtype is not None:
            kind = getattr(dtype, "kind", None)
            if kind == "f":
                column_states[c] = 3
            elif kind == "i" or kind == "u":
                if st < 2:
                    column_states[c] = 2
            elif kind == "b":
                if st == 0:
                    column_states[c] = 1
            else:
                column_states[c] = 3
        else:
            column_states[c] = 3


cdef inline bint _flatten_cell(
    object val,
    int c,
    int idx,
    double[:] buf_view,
    dict strings,
    list column_states,
    list column_has_none,
    bint has_non_numeric,
):
    cdef double fval

    cdef object dtype

    if val is None:
        buf_view[idx] = c_nan
        column_has_none[c] = True
    elif PyUnicode_Check(val):
        has_non_numeric = True
        buf_view[idx] = c_nan
        strings[idx] = val
    elif PyFloat_Check(val) or PyBool_Check(val):
        # Hot fast-path for CPython built-in float and bool.
        fval = PyFloat_AsDouble(val)
        buf_view[idx] = fval
        _update_column_state(column_states, c, val)
    elif PyLong_Check(val):
        # What was wrong: PyFloat_AsDouble on a huge int (10**400) raises
        # OverflowError. cpython.float declares it ``except? -1``, so Cython
        # raises at the call and a following PyErr_Occurred check never runs.
        # The error escaped this function, _try_accel fell back to stdlib, and
        # the matching Python int branch crashed. Why this works: catch
        # OverflowError and store str(val), matching _numeric_cell_to_float.
        # Integers float() accepts stay numeric, including past 53 bits.
        # The integer -1 is -1.0 with no exception, so it is not text.
        try:
            fval = PyFloat_AsDouble(val)
        except OverflowError:
            has_non_numeric = True
            buf_view[idx] = c_nan
            strings[idx] = PyObject_Str(val)
        else:
            buf_view[idx] = fval
            _update_column_state(column_states, c, val)
    else:
        # NumPy scalars, other custom numeric classes, or non-numeric objects.
        # Kind "c" (np.complex64/128/clongdouble): PyFloat_AsDouble keeps the
        # real part and does not set an error. Python complex fails the
        # conversion and is already text. Check kind before converting.
        dtype = getattr(val, "dtype", None)
        if dtype is not None and getattr(dtype, "kind", None) == "c":
            has_non_numeric = True
            buf_view[idx] = c_nan
            strings[idx] = PyObject_Str(val)
        else:
            # Same except? -1 trap as the int arm: the error check after
            # PyFloat_AsDouble was unreachable, so a failed conversion raised
            # out of this function instead of storing str(val).
            try:
                fval = PyFloat_AsDouble(val)
            except (OverflowError, TypeError, ValueError):
                has_non_numeric = True
                buf_view[idx] = c_nan
                strings[idx] = val if PyUnicode_Check(val) else PyObject_Str(val)
            else:
                buf_view[idx] = fval
                _update_column_state(column_states, c, val)

    return has_non_numeric

def fast_flatten_grid_2d(list grid, int ncols):
    """
    Cython-accelerated 2D grid flattening.
    Returns (buffer_bytes, strings, column_states, column_has_none, has_non_numeric)
    """
    cdef int nrows = PyList_GET_SIZE(grid)
    cdef int ncells = nrows * ncols
    
    cdef object buf = array.array('d', [0.0] * ncells)
    cdef double[:] buf_view = buf
    
    cdef dict strings = {}
    cdef list column_states = [0] * ncols
    cdef list column_has_none = [False] * ncols
    cdef bint has_non_numeric = False
    
    cdef int r, c, idx = 0
    cdef object row, val
    
    for r in range(nrows):
        row = <object>PyList_GET_ITEM(grid, r)
        if PyList_GET_SIZE(row) != ncols:
            raise ValueError(f"Uneven row lengths in data grid at row {r}")
            
        for c in range(ncols):
            val = <object>PyList_GET_ITEM(row, c)
            has_non_numeric = _flatten_cell(val, c, idx, buf_view, strings, column_states, column_has_none, has_non_numeric)
            idx += 1
            
    return buf, strings, column_states, column_has_none, has_non_numeric

def fast_flatten_grid_1d(list grid):
    """
    Cython-accelerated 1D grid flattening.
    Returns (buffer_bytes, strings, column_states, column_has_none, has_non_numeric)
    """
    cdef int ncells = PyList_GET_SIZE(grid)
    
    cdef object buf = array.array('d', [0.0] * ncells)
    cdef double[:] buf_view = buf
    
    cdef dict strings = {}
    cdef list column_states = [0]
    cdef list column_has_none = [False]
    cdef bint has_non_numeric = False
    
    cdef int idx = 0
    cdef object val
    
    for idx in range(ncells):
        val = <object>PyList_GET_ITEM(grid, idx)
        has_non_numeric = _flatten_cell(val, 0, idx, buf_view, strings, column_states, column_has_none, has_non_numeric)
            
    return buf, strings, column_states, column_has_none, has_non_numeric
