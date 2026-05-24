# cython: language_level=3
# WriterAgent - Cython accelerator for serialization packing.

import array
import math
from cpython.object cimport PyObject
from cpython.list cimport PyList_GetItem, PyList_Size
from cpython.float cimport PyFloat_AsDouble, PyFloat_Check
from cpython.long cimport PyLong_AsLong, PyLong_Check
from cpython.bool cimport PyBool_Check

cdef extern from "Python.h":
    int PyObject_TypeCheck(object, PyObject*)
    object PyObject_Str(object)
    int PyUnicode_Check(object)

def fast_flatten_grid_2d(list grid, int ncols):
    """
    Cython-accelerated 2D grid flattening.
    Returns (buffer_bytes, strings, column_states, column_has_none, has_non_numeric)
    """
    cdef int nrows = len(grid)
    cdef int ncells = nrows * ncols
    
    # We use a double array for the buffer
    import array
    cdef object buf = array.array('d', [0.0] * ncells)
    cdef double[:] buf_view = buf
    
    cdef dict strings = {}
    cdef list column_states = [0] * ncols
    cdef list column_has_none = [False] * ncols
    cdef bint has_non_numeric = False
    
    cdef int r, c, idx = 0
    cdef object row, val
    cdef double fval
    cdef double nan = math.nan
    cdef int st
    
    for r in range(nrows):
        row = <object>PyList_GetItem(grid, r)
        if PyList_Size(row) != ncols:
            raise ValueError(f"Uneven row lengths in data grid at row {r}")
            
        for c in range(ncols):
            val = <object>PyList_GetItem(row, c)
            
            if val is None:
                buf_view[idx] = nan
                column_has_none[c] = True
            elif PyUnicode_Check(val):
                has_non_numeric = True
                buf_view[idx] = nan
                strings[idx] = val
            elif not has_non_numeric:
                # Fast path: numeric
                if PyFloat_Check(val):
                    fval = PyFloat_AsDouble(val)
                    buf_view[idx] = fval
                    if <int>column_states[c] != 3:
                        column_states[c] = 3
                elif PyLong_Check(val):
                    fval = <double>PyLong_AsLong(val)
                    buf_view[idx] = fval
                    st = <int>column_states[c]
                    if st < 2:
                        column_states[c] = 2
                elif PyBool_Check(val):
                    fval = 1.0 if val else 0.0
                    buf_view[idx] = fval
                    st = <int>column_states[c]
                    if st == 0:
                        column_states[c] = 1
                else:
                    # Fallback for complex/other types
                    has_non_numeric = True
                    buf_view[idx] = nan
                    strings[idx] = PyObject_Str(val)
            else:
                # Already non-numeric mode
                buf_view[idx] = nan
                strings[idx] = PyObject_Str(val)
            
            idx += 1
            
    return buf, strings, column_states, column_has_none, has_non_numeric
