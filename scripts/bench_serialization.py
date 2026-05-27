#!/usr/bin/env python3
# WriterAgent - benchmark asymmetric serialization (host stdlib vs child NumPy).
# Run outside LibreOffice: python scripts/bench_serialization.py
"""Compare json_list, json+sg, pickle5, and pickle5+sg for host→venv and venv→host paths.

See plugin/scripting/payload_codec.py for why the split_grid envelope exists (NumPy frombuffer).
"""
from __future__ import annotations

import argparse
import json
import pickle
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from plugin.scripting.payload_codec import (  # noqa: E402
    BINARY_MIN_CELLS,
    MAX_BENCH_CELLS,
    ForceBinary,
    cell_count,
    child_pack_result,
    child_unpack_data,
    host_pack_data,
    host_unpack_data,
    fast_flatten_grid_2d,
)
from tests.scripting.payload_codec_test_support import (  # noqa: E402
    legacy_b64_child_pack_split_grid,
    legacy_b64_child_unpack_split_grid,
    legacy_b64_host_pack_split_grid,
    legacy_b64_host_unpack_split_grid,
)


def child_materialize_list(wire: Any) -> Any:
    """Baseline slow path: np.array after json.loads/pickle.loads produced Python lists."""

    import numpy as np
    return np.array(wire, dtype=np.float64)


@dataclass
class BenchRow:
    direction: str
    kind: str
    shape: str
    cells: int
    wire_format: str  # json_list | json+sg | pickle5 | pickle5+sg
    host_pack_ms: float
    host_dump_ms: float
    peer_load_ms: float
    materialize_ms: float
    total_ms: float
    wire_bytes: int
    json_wire_bytes: int | None = None  # paired json_list size for comparison
    wire_vs_json: str = ""  # e.g. "52% of json" on json+sg rows
    mat_faster_x: float | None = None  # json_list mat / format mat; >1 => format faster
    total_faster_x: float | None = None  # json_list total / format total; >1 => format faster
    faster_total: str = ""  # e.g. "★ pickle5+sg"


def _median(times: list[float]) -> float:
    if not times:
        return 0.0
    s = sorted(times)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def _bench(fn: Callable[[], Any], *, warmup: int, iters: int) -> tuple[Any, float]:
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    last: Any = None
    for _ in range(iters):
        t0 = time.perf_counter()
        last = fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return last, _median(samples)


def _bench_parts(
    parts: list[tuple[str, Callable[[Any], Any]]],
    *,
    warmup: int,
    iters: int,
) -> tuple[dict[str, float], Any]:
    out: dict[str, float] = {}
    current_input: Any = None

    # Warmup all parts in sequence to ensure 'current_input' is populated for each stage
    for _, fn in parts:
        current_input = fn(current_input)

    # Measure each part
    current_input = None
    for name, fn in parts:
        samples: list[float] = []

        for _ in range(iters):
            t0 = time.perf_counter()
            fn(current_input)
            samples.append((time.perf_counter() - t0) * 1000)

        out[name] = _median(samples)
        current_input = fn(current_input)
    return out, current_input


def make_grid(nrows: int, ncols: int) -> list[Any] | list[list[Any]]:
    if nrows == 1 and ncols == 1:
        return [random.random()]
    if nrows == 1:
        return [random.random() for _ in range(ncols)]
    if ncols == 1:
        return [[random.random()] for _ in range(nrows)]
    return [[random.random() for _ in range(ncols)] for _ in range(nrows)]


def shape_label(nrows: int, ncols: int) -> str:
    if nrows == 1 and ncols == 1:
        return "scalar"
    if nrows == 1:
        return f"1x{ncols}"
    if ncols == 1:
        return f"{nrows}x1"
    return f"{nrows}x{ncols}"


def grid_shapes() -> list[tuple[int, int, str]]:
    specs = [
        (1, 1, "scalar"),
        (3, 3, "grid"),
        (4, 4, "grid"),
        (10, 10, "grid"),
        (100, 100, "grid"),
        (1, 1000, "grid"),
        (1000, 1, "grid"),
        (10, 1, "list1d"),
        (100, 1, "list1d"),
        (20_000, 5, "grid"),  # 100_000 cells — all numeric via make_grid
    ]
    out: list[tuple[int, int, str]] = []
    for nrows, ncols, kind in specs:
        if cell_count((nrows, ncols)) > MAX_BENCH_CELLS:
            continue
        out.append((nrows, ncols, kind))
    return out


def run_ingress(
    grid: list[Any] | list[list[Any]],
    shape: tuple[int, ...],
    *,
    force: ForceBinary,
    min_cells: int,
    warmup: int,
    iters: int,
    wire_format: str,
) -> tuple[dict[str, float], int]:
    if wire_format in ("pickle5", "pickle5+sg"):
        pack_force: ForceBinary = "never" if wire_format == "pickle5" else "always"

        parts = [
            ("host_pack", lambda _: host_pack_data(grid, min_cells=min_cells, force=pack_force)),
            ("host_dump", lambda data: pickle.dumps({"id": "b", "data": data}, protocol=5)),
            ("child_load", lambda b: pickle.loads(b)["data"]),
            ("child_mat", lambda data: child_unpack_data(data) if wire_format == "pickle5+sg" else child_materialize_list(data)),
        ]
    elif wire_format == "json+sg":
        parts = [
            ("host_pack", lambda _: legacy_b64_host_pack_split_grid(grid)),
            ("host_dump", lambda data: json.dumps({"id": "b", "data": data}, default=str) + "\n"),
            ("child_load", lambda line: json.loads(line)["data"]),
            ("child_mat", lambda data: legacy_b64_child_unpack_split_grid(data)),
        ]
    else:  # json_list
        parts = [
            ("host_pack", lambda _: host_pack_data(grid, min_cells=min_cells, force="never")),
            ("host_dump", lambda data: json.dumps({"id": "b", "data": data}, default=str) + "\n"),
            ("child_load", lambda line: json.loads(line)["data"]),
            ("child_mat", lambda data: child_materialize_list(data)),
        ]

    times, _last_res = _bench_parts(parts, warmup=warmup, iters=iters)

    wire_data = parts[0][1](None)
    wire_bytes_raw = parts[1][1](wire_data)

    if isinstance(wire_bytes_raw, str):
        wire_bytes = len(wire_bytes_raw.encode("utf-8"))
    else:
        wire_bytes = len(wire_bytes_raw)

    return times, wire_bytes


def run_egress(
    nrows: int,
    ncols: int,
    *,
    force: ForceBinary,
    min_cells: int,
    warmup: int,
    iters: int,
    wire_format: str,
) -> tuple[dict[str, float], int]:
    import numpy as np

    if nrows == 1 and ncols == 1:
        result: Any = float(random.random())
        kind_pack = lambda: result
    else:
        arr = np.random.rand(nrows, ncols) if ncols > 1 else np.random.rand(nrows)
        kind_pack = lambda: arr

    if wire_format in ("pickle5", "pickle5+sg"):
        pack_force: ForceBinary = "never" if wire_format == "pickle5" else "always"

        parts = [
            ("child_pack", lambda _: child_pack_result(kind_pack(), min_cells=min_cells, force=pack_force)),
            ("child_dump", lambda res: pickle.dumps({"id": "b", "result": res}, protocol=5)),
            ("host_load", lambda b: pickle.loads(b)["result"]),
            ("host_mat", lambda res: host_unpack_data(res, as_nested_list=True)),
        ]
    elif wire_format == "json+sg":
        def pack_b64(r: Any) -> dict[str, Any]:
            if isinstance(r, np.ndarray):
                return legacy_b64_child_pack_split_grid(r)
            return legacy_b64_host_pack_split_grid(r)

        parts = [
            ("child_pack", lambda _: pack_b64(kind_pack())),
            ("child_dump", lambda res: json.dumps({"id": "b", "result": res}, default=str) + "\n"),
            ("host_load", lambda line: json.loads(line)["result"]),
            ("host_mat", lambda res: legacy_b64_host_unpack_split_grid(res)),
        ]
    else:  # json_list
        parts = [
            ("child_pack", lambda _: child_pack_result(kind_pack(), min_cells=min_cells, force="never")),
            ("child_dump", lambda res: json.dumps({"id": "b", "result": res}, default=str) + "\n"),
            ("host_load", lambda line: json.loads(line)["result"]),
            ("host_mat", lambda res: host_unpack_data(res, as_nested_list=True)),
        ]

    times, _last_res = _bench_parts(parts, warmup=warmup, iters=iters)

    wire_data = parts[0][1](None)
    wire_bytes_raw = parts[1][1](wire_data)

    if isinstance(wire_bytes_raw, str):
        wire_bytes = len(wire_bytes_raw.encode("utf-8"))
    else:
        wire_bytes = len(wire_bytes_raw)

    return times, wire_bytes


def run_child_only(
    grid: list[Any] | list[list[Any]],
    *,
    min_cells: int,
    warmup: int,
    iters: int,
) -> tuple[float, float, float, float, float, float, float]:
    data_list = host_pack_data(grid, min_cells=min_cells, force="never")
    data_json_sg = legacy_b64_host_pack_split_grid(grid)
    data_pickle_split_grid = host_pack_data(grid, min_cells=min_cells, force="always")
    line_list = json.dumps({"data": data_list}) + "\n"
    line_json_sg = json.dumps({"data": data_json_sg}) + "\n"
    bytes_pickle = pickle.dumps({"data": data_list}, protocol=5)
    bytes_pickle_sg = pickle.dumps({"data": data_pickle_split_grid}, protocol=5)

    def mat_list() -> Any:
        return child_materialize_list(json.loads(line_list)["data"])

    def mat_json_sg() -> Any:
        return legacy_b64_child_unpack_split_grid(json.loads(line_json_sg)["data"])

    def mat_pickle5() -> Any:
        return child_materialize_list(pickle.loads(bytes_pickle)["data"])

    def mat_pickle5_sg() -> Any:
        return child_unpack_data(pickle.loads(bytes_pickle_sg)["data"])

    _, list_ms = _bench(mat_list, warmup=warmup, iters=iters)
    _, json_sg_ms = _bench(mat_json_sg, warmup=warmup, iters=iters)
    _, pickle5_ms = _bench(mat_pickle5, warmup=warmup, iters=iters)
    _, pickle5_sg_ms = _bench(mat_pickle5_sg, warmup=warmup, iters=iters)

    sp_json_sg = list_ms / json_sg_ms if json_sg_ms > 0 else 0.0
    sp_pickle = list_ms / pickle5_ms if pickle5_ms > 0 else 0.0
    sp_pickle_sg = list_ms / pickle5_sg_ms if pickle5_sg_ms > 0 else 0.0
    return list_ms, json_sg_ms, pickle5_ms, pickle5_sg_ms, sp_json_sg, sp_pickle, sp_pickle_sg


def main() -> None:
    try:
        import numpy as np  # noqa: F401
    except ImportError:
        print("NumPy required for child-side benchmarks. Install numpy in this interpreter.")
        sys.exit(1)

    p = argparse.ArgumentParser(
        description="Benchmark json_list vs json+sg vs pickle5 vs pickle5+sg (host Python / child NumPy)."
    )
    p.add_argument("--direction", choices=("ingress", "egress", "both"), default="both")
    p.add_argument("--force-binary", choices=("auto", "always", "never"), default="auto")
    p.add_argument(
        "--min-cells",
        type=int,
        default=BINARY_MIN_CELLS,
        help="Use split_grid envelope when cell count is at least this (default from payload_codec)",
    )
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--iters", type=int, default=15)
    p.add_argument("--child-only", action="store_true", help="Only compare np.array vs frombuffer on same wire lines")
    p.add_argument("--json", action="store_true", dest="json_out")
    args = p.parse_args()
    force: ForceBinary = args.force_binary

    if fast_flatten_grid_2d is not None:
        print(">>> Using Cython accelerator for Split-Grid packing.\n")
    else:
        print(">>> Using Pure Python for Split-Grid packing.\n")

    if args.child_only:
        print(
            "child_only: materialize ms — json_list (np.array) vs json+sg (frombuffer) "
            "vs pickle5 (np.array) vs pickle5+sg (frombuffer)"
        )
        print(
            f"{'shape':<12} {'cells':>8} {'json_list_ms':>14} {'json+sg_ms':>14} "
            f"{'pickle5_ms':>14} {'pickle5+sg_ms':>14} {'json+sg_x':>9} {'pickle_x':>9} {'pickle+sg_x':>11}"
        )
        for nrows, ncols, _ in grid_shapes():
            grid = make_grid(nrows, ncols)
            list_ms, json_sg_ms, pickle5_ms, pickle5_sg_ms, sp_json_sg, sp_pickle, sp_pickle_sg = run_child_only(
                grid,
                min_cells=args.min_cells,
                warmup=args.warmup,
                iters=args.iters,
            )
            print(
                f"{shape_label(nrows, ncols):<12} {cell_count((nrows, ncols)):>8} {list_ms:>14.4f} "
                f"{json_sg_ms:>14.4f} {pickle5_ms:>14.4f} {pickle5_sg_ms:>14.4f} "
                f"{sp_json_sg:>8.2f}x {sp_pickle:>8.2f}x {sp_pickle_sg:>10.2f}x"
            )
        print("\n  json+sg_x    = json+sg (frombuffer) vs json_list (np.array).")
        print("  pickle_x     = pickle5 nested lists (np.array) vs json_list (np.array).")
        print("  pickle+sg_x  = pickle5+sg split_grid (frombuffer) vs json_list (np.array).")
        return

    rows: list[BenchRow] = []
    for nrows, ncols, kind in grid_shapes():
        grid = make_grid(nrows, ncols)
        shape = (nrows, ncols) if ncols > 1 or nrows > 1 else (1,)
        if nrows == 1 and ncols > 1:
            shape = (ncols,)
        cells = cell_count(shape if nrows != 1 or ncols != 1 else (1,))

        if args.direction in ("ingress", "both"):
            for wire_format in ("json_list", "json+sg", "pickle5", "pickle5+sg"):
                if force == "never" and wire_format == "json+sg":
                    continue
                t, wire_b = run_ingress(
                    grid,
                    shape,
                    force=force,
                    min_cells=args.min_cells,
                    warmup=args.warmup,
                    iters=args.iters,
                    wire_format=wire_format,
                )
                total = t["host_pack"] + t["host_dump"] + t["child_load"] + t["child_mat"]
                rows.append(
                    BenchRow(
                        "ingress",
                        kind,
                        shape_label(nrows, ncols),
                        cells,
                        wire_format,
                        t["host_pack"],
                        t["host_dump"],
                        t["child_load"],
                        t["child_mat"],
                        total,
                        wire_b,
                    )
                )

        if args.direction in ("egress", "both"):
            for wire_format in ("json_list", "json+sg", "pickle5", "pickle5+sg"):
                if nrows == 1 and ncols == 1 and wire_format == "json+sg":
                    continue
                t, wire_b = run_egress(
                    nrows,
                    ncols,
                    force=force,
                    min_cells=args.min_cells,
                    warmup=args.warmup,
                    iters=args.iters,
                    wire_format=wire_format,
                )
                total = t["child_pack"] + t["child_dump"] + t["host_load"] + t["host_mat"]
                rows.append(
                    BenchRow(
                        "egress",
                        kind,
                        shape_label(nrows, ncols),
                        cells,
                        wire_format,
                        t["child_pack"],
                        t["child_dump"],
                        t["host_load"],
                        t["host_mat"],
                        total,
                        wire_b,
                    )
                )

    # Pair json_list vs json+sg vs pickle5 vs pickle5+sg per shape for size and speed comparisons
    by_key: dict[tuple[str, str, str], dict[str, BenchRow]] = {}
    for r in rows:
        key = (r.direction, r.shape, r.kind)
        by_key.setdefault(key, {})[r.wire_format] = r
    for paths in by_key.values():
        json_r = paths.get("json_list")
        if json_r is None:
            continue
        json_b = json_r.wire_bytes
        json_r.json_wire_bytes = json_b
        json_r.wire_vs_json = "baseline (json)"

        for fmt in ("json+sg", "pickle5", "pickle5+sg"):
            fmt_r = paths.get(fmt)
            if fmt_r is None:
                continue
            fmt_b = fmt_r.wire_bytes
            fmt_r.json_wire_bytes = json_b
            if json_b > 0:
                pct = 100.0 * fmt_b / json_b
                fmt_r.wire_vs_json = f"{pct:.0f}% of json ({fmt_b}/{json_b} B)"
            if fmt_r.materialize_ms > 0:
                fmt_r.mat_faster_x = json_r.materialize_ms / fmt_r.materialize_ms
            if fmt_r.total_ms > 0:
                fmt_r.total_faster_x = json_r.total_ms / fmt_r.total_ms

        valid_paths = [r for r in paths.values() if r.total_ms > 0]
        if valid_paths:
            fastest_r = min(valid_paths, key=lambda x: x.total_ms)
            for r in valid_paths:
                if r is fastest_r:
                    r.faster_total = f"★ {r.wire_format}"

    hdr = (
        f"{'direction':<10} {'kind':<8} {'shape':<10} {'cells':>7} {'wire_format':<13} "
        f"{'pack':>8} {'dump':>8} {'load':>8} {'materialize':>11} {'total_ms':>9} "
        f"{'wire_KiB':>9} {'vs_json_wire':<28} {'mat_x':>8} {'total_x':>8} {'e2e_faster':<12}"
    )
    print(hdr)
    for r in rows:
        wire_kib = r.wire_bytes / 1024.0
        mat_x = f"{r.mat_faster_x:.2f}x" if r.mat_faster_x is not None else ""
        tot_x = f"{r.total_faster_x:.2f}x" if r.total_faster_x is not None else ""
        print(
            f"{r.direction:<10} {r.kind:<8} {r.shape:<10} {r.cells:>7} {r.wire_format:<13} "
            f"{r.host_pack_ms:>8.3f} {r.host_dump_ms:>8.3f} {r.peer_load_ms:>8.3f} {r.materialize_ms:>11.3f} "
            f"{r.total_ms:>9.3f} {wire_kib:>9.2f} {r.wire_vs_json:<28} {mat_x:>8} {tot_x:>8} {r.faster_total:<12}"
        )

    print(
        "\nColumn guide:\n"
        "  direction     ingress = Host -> Child (LibreOffice Host sending data into the sandboxed Child worker).\n"
        "                egress = Child -> Host (sandboxed Child worker returning data back to the LibreOffice Host).\n"
        "  wire_format   json_list = nested floats in JSON (slow materialize).\n"
        "                json+sg = split_grid envelope with Base64 buffer in a JSON line (fast frombuffer).\n"
        "                pickle5 = Pickle protocol 5 with nested lists (force=never; slow np.array materialize).\n"
        "                pickle5+sg = Pickle protocol 5 with split_grid envelope (force=always; fast frombuffer).\n"
        "  wire_KiB      Line/payload size on the wire for this row.\n"
        "  vs_json_wire  On comparing rows: size vs paired json_list row "
        "(same shape/direction). json_list row shows 'baseline (json)'.\n"
        "  materialize   timings for rebuilding data: ingress (child np.array or frombuffer) / egress (host unpack to lists).\n"
        "  mat_x, total_x, e2e_faster  Shown only for speedups relative to the baseline.\n"
        "  mat_x         Speedup factor (e.g. 4.74x): json_list materialize / format materialize.\n"
        "  total_x       Speedup factor (e.g. 2.06x): json_list total_ms / format total_ms.\n"
        "  e2e_faster    wire_format that won on total_ms for this shape."
    )

    if args.json_out:
        import json as json_mod

        print(json_mod.dumps([r.__dict__ for r in rows], indent=2))


if __name__ == "__main__":
    main()
