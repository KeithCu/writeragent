# ppt-master in WriterAgent (contrib)

WriterAgent-specific adapters for [hugohe3/ppt-master](https://github.com/hugohe3/ppt-master) (MIT). **Unmodified upstream scripts are not vendored here** — they load from a cloned skill tree on disk. WriterAgent ships a **forked `skill/SKILL.md`** (orchestration only); the upstream pin below applies to scripts/assets in the user clone, not the forked doc.

**Dev reference clone (optional):** repo root `ppt-master/`.

**Upstream pin (dev clone):** `f888bff138f96e9e5e2e43f8c90c66695e979f60`

Do **not** re-add `plugin/contrib/ppt_master/bundled/` or `backends/` — upstream `scripts/svg_to_pptx/` stays external.

## Shipped modules (`plugin/contrib/ppt_master/`)

| File | Purpose | Relationship |
|------|---------|--------------|
| `coords.py` | Default Impress slide dimensions (16:9 hmm) | WriterAgent-only |
| `upstream.py` | Load upstream `pptx_discovery`; project SVG/notes discovery | WriterAgent runtime loader |
| `config.py` | Path helpers under `PPT_MASTER_DATA_ROOT` | WriterAgent-only |
| `skill/SKILL.md` | Forked orchestration doc (WriterAgent sidebar) | Fork of upstream `SKILL.md`; scripts/templates stay in user data root |
| `skill_paths.py` | Resolve bundled `SKILL.md` vs data-root fallback | WriterAgent-only |

Host integration: [`plugin/ppt_master/`](../../ppt_master/) (`pptx_build`, `uno_pptx_import`, `uno_pptx_deck`, `uno_shape_postprocess`). Design doc: [`docs/ppt-master-integration-plan.md`](../../../docs/ppt-master-integration-plan.md#roadmap).

## Symbol map (WriterAgent → upstream)

| WriterAgent symbol | Upstream equivalent | Active route |
|--------------------|---------------------|--------------|
| `upstream.collect_svg_files` | `pptx_discovery.find_svg_files` | project discovery / PPTX build input |
| `upstream.collect_svg_files_upstream` | `pptx_discovery.find_svg_files` | runtime file load |
| `upstream.collect_notes_upstream` | `pptx_discovery.find_notes_files` | runtime file load |
| `uno_pptx_import.import_pptx_to_doc` | (none) | Primary UNO export |

## Annotation conventions

Shipped Python under `plugin/contrib/ppt_master/` and `plugin/ppt_master/` is **WriterAgent-original**. Upstream attribution and the symbol map live **here in README** only.

## Install upstream

```bash
git clone https://github.com/hugohe3/ppt-master.git
```

**Settings → Python** → **PPT-Master data path** → `.../ppt-master/skills/ppt-master`. Use **Test** to verify `SKILL.md`, `templates/`, and `scripts/svg_to_pptx/` are present.

## Merge policy

- **Do not** copy `scripts/svg_to_pptx/` into contrib unless you must **change** upstream code.
- UNO export uses LibreOffice's native PPTX filter → copy shapes into Impress (see `uno_pptx_import`).

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
