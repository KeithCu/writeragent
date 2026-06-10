# Vendored langdetect

Subset of [Mimino666/langdetect](https://github.com/Mimino666/langdetect) v1.0.9 (MIT).

**Profile allowlist:** grammar proofreader BCP-47 registry only (34 profiles):

`bg, bn, ca, cs, da, de, el, en, es, et, fi, fr, hi, hr, hu, id, it, ja, ko, lt, lv, nl, no, pl, pt, ro, ru, sk, sv, tr, uk, ur, zh-cn, zh-tw`

**Re-sync from PyPI:**

```bash
make langdetect-contrib
```

**Merge policy:** Refresh via `scripts/update_langdetect_contrib.py` only; do not hand-edit upstream modules except the documented Py3-only `detector.py` patch (drops `six`).
