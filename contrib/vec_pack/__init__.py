try:
    from .pack import fast_flatten_grid_2d, fast_flatten_grid_1d
except ImportError:
    fast_flatten_grid_2d = None
    fast_flatten_grid_1d = None
