# Vendored nbformat

Subset of [jupyter/nbformat](https://github.com/jupyter/nbformat) (BSD-3-Clause) for reading `.ipynb` files inside WriterAgent.

**Upstream pin:** [v5.10.4](https://github.com/jupyter/nbformat/releases/tag/v5.10.4) (optional dev clone commit `ceaf199`).

**Merge policy:** Keep upstream code line-for-line where possible. **Comment out** paths we cannot ship (v1–v3, `traitlets`, `fastjsonschema` validation) — do not delete them — so `diff` against upstream stays small.

**Shipped:** v4 JSON read/write helpers — `rejoin_lines`, `strip_transient`, `NotebookNode`, `read_ipynb`.

**Not shipped:** v1/v2/v3 packages, JSON schema validation (`fastjsonschema`), `traitlets`, `jupyter_core`, top-level `nbformat/__init__.py` (`validate`, `convert`, `as_version`).

**Deferred:** nbformat v3 upgrade (`v4/convert.py` in upstream). Revisit when users need legacy `.ipynb` files; see [enabling_numpy_in_libreoffice.md](../../docs/enabling_numpy_in_libreoffice.md#jupyter-notebook-import-ipynb).

**Vendored files (synced from upstream):**

- `nbformat/notebooknode.py`
- `nbformat/_struct.py`
- `nbformat/v4/rwbase.py`
- `nbformat/v4/nbjson.py`
- `nbformat/v4/__init__.py` (convert/nbbase imports commented)
- `nbformat/reader.py` (v1–v3 and `ValidationError` commented; `versions = {4: v4}`)

**Re-sync from a local clone:**

```bash
diff -u nbformat/nbformat/<file> plugin/contrib/nbformat/<file>
```

Keep a dev-only clone at repo root `nbformat/` (add to `.gitignore` if desired).
