# ppt-master in WriterAgent (contrib)

WriterAgent-specific adapters for [hugohe3/ppt-master](https://github.com/hugohe3/ppt-master). **Unmodified upstream code is not vendored here** — it loads from a cloned skill tree on disk.

**Dev reference clone (optional):** repo root `ppt-master/`.

## Shipped modules (`plugin/contrib/ppt_master/`)

| File | Purpose |
|------|---------|
| `shape_ops.py` | `ShapeOp` / `SlideBuildPlan` for UNO export |
| `coords.py` | SVG viewBox → LibreOffice 1/100 mm |
| `svg_convert.py` | Minimal SVG → `ShapeOp` (UNO path) |
| `upstream.py` | Load upstream `pptx_discovery` from skill tree |
| `config.py` | Path helpers under `PPT_MASTER_DATA_ROOT` (requires `apply_data_root_env`) |

Host integration: [`plugin/ppt_master/`](../../ppt_master/). Design doc: [`docs/ppt-master-integration-plan.md`](../../../docs/ppt-master-integration-plan.md).

## Install upstream

```bash
git clone https://github.com/hugohe3/ppt-master.git
```

**Settings → Python** → **PPT-Master data path** → `.../ppt-master/skills/ppt-master`. Use **Test** to verify `SKILL.md`, `templates/`, and `scripts/svg_to_pptx/` are present.

## Merge policy

- **Do not** copy `scripts/svg_to_pptx/` into contrib unless you must **change** upstream code.
- When forking upstream lines, comment out replaced code (see `plugin/contrib/nbformat/README.md`).
