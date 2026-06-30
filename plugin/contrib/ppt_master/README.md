# ppt-master in WriterAgent (contrib)

WriterAgent-specific adapters for [hugohe3/ppt-master](https://github.com/hugohe3/ppt-master) (MIT). **Unmodified upstream code is not vendored here** — it loads from a cloned skill tree on disk.

**Dev reference clone (optional):** repo root `ppt-master/`.

**Upstream pin (dev clone):** `f888bff138f96e9e5e2e43f8c90c66695e979f60`

Do **not** re-add `plugin/contrib/ppt_master/bundled/` or `backends/` — upstream `scripts/svg_to_pptx/` stays external.

## Shipped modules (`plugin/contrib/ppt_master/`)

| File | Purpose | Relationship |
|------|---------|--------------|
| `shape_ops.py` | `ShapeOp` / `SlideBuildPlan` for UNO export | WriterAgent-only |
| `coords.py` | SVG viewBox → LibreOffice 1/100 mm | Parallel rewrite of upstream EMU sizing |
| `svg_convert.py` | Minimal SVG → `ShapeOp` (UNO path) | Parallel rewrite of `drawingml_converter.py` |
| `upstream.py` | Load upstream `pptx_discovery` from skill tree | WriterAgent runtime loader |
| `config.py` | Path helpers under `PPT_MASTER_DATA_ROOT` | WriterAgent-only |

Host integration: [`plugin/ppt_master/`](../../ppt_master/) (WriterAgent-only UNO layer). Design doc + roadmap: [`docs/ppt-master-integration-plan.md`](../../../docs/ppt-master-integration-plan.md#roadmap).

## Symbol map (WriterAgent → upstream)

| WriterAgent symbol | Upstream equivalent | Active route |
|--------------------|---------------------|--------------|
| `coords.parse_viewbox` | (inline in drawingml_converter) | UNO hmm |
| `coords.px_to_hmm` | `drawingml_utils.EMU_PER_PX` + `pptx_dimensions` | UNO hmm |
| `coords.slide_dims_for_viewbox` | `pptx_dimensions.slide_emu_size` | UNO hmm |
| `shape_ops.ShapeOp` | DrawingML XML strings from converter | NEW interchange format |
| `shape_ops.SlideBuildPlan` | slide XML + rels from `pptx_builder` | NEW interchange format |
| `svg_convert.svg_to_slide_plan` | `drawingml_converter.convert_svg_to_slide_shapes` | UNO ShapeOp |
| `svg_convert.collect_svg_files` | `pptx_discovery.find_svg_files` (via `upstream.py` when installed) | hybrid |
| `upstream.collect_svg_files_upstream` | `pptx_discovery.find_svg_files` | runtime file load |
| `config.data_root` | upstream skill-tree layout / `config` | NEW env wrapper |

## Annotation conventions

Shipped Python under `plugin/contrib/ppt_master/` and `plugin/ppt_master/` is **WriterAgent-original** (parallel rewrites and UNO glue). **Do not** add upstream MIT headers, `UPSTREAM NOTE` blocks, or `'''` reference snippets in those `.py` files — upstream attribution and the symbol map live **here in README** only.

When you **vendor or fork** actual upstream lines (nbformat-style), annotate **only in that file**: comment out replaced upstream code with `'''` blocks and keep upstream copyright in the block. See [`plugin/contrib/nbformat/README.md`](../nbformat/README.md).

## Install upstream

```bash
git clone https://github.com/hugohe3/ppt-master.git
```

**Settings → Python** → **PPT-Master data path** → `.../ppt-master/skills/ppt-master`. Use **Test** to verify `SKILL.md`, `templates/`, and `scripts/svg_to_pptx/` are present.

## Merge policy

- **Do not** copy `scripts/svg_to_pptx/` into contrib unless you must **change** upstream code.
- When forking upstream lines, comment out replaced code with `'''` blocks (see nbformat README).
- `svg_convert.py` is a **parallel rewrite**, not a line-for-line fork of `drawingml_converter.py`.

## Re-sync from dev clone

Compare parallel implementations against upstream (not expecting identical diffs):

```bash
diff -u ppt-master/skills/ppt-master/scripts/svg_to_pptx/drawingml_converter.py plugin/contrib/ppt_master/svg_convert.py
diff -u ppt-master/skills/ppt-master/scripts/svg_to_pptx/pptx_dimensions.py plugin/contrib/ppt_master/coords.py
diff -u ppt-master/skills/ppt-master/scripts/svg_to_pptx/pptx_discovery.py plugin/contrib/ppt_master/upstream.py
```

## MIT License (upstream ppt-master)

WriterAgent adapter code in this directory is **GPL-3.0-or-later**. Upstream [ppt-master](https://github.com/hugohe3/ppt-master) is **MIT**:

```
MIT License

Copyright (c) 2025-2026 Hugo He

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
