# Python in Excel packages vs. the WriterAgent compute image

This note compares the packages Microsoft ships with Python in Excel to the wheels baked into our compute image (`compute_service/Dockerfile`). It covers what to add and what to keep out.

Related: [Why LibreOffice should not copy Microsoft's `=PY()` design](ms-py-compatibility.md).

## What Python in Excel ships

Python in Excel runs in a Microsoft cloud container. That container uses the Anaconda Distribution, built from source by Anaconda. Microsoft and Anaconda describe "a curated set of secured libraries", and Anaconda says there are more than 400 packages. Microsoft publishes only a "subset" table. It does not publish a full list with versions.

Five libraries are imported by default in the workbook initialization pane:

- `numpy as np`
- `pandas as pd`
- `matplotlib.pyplot as plt`
- `seaborn as sns`
- `statsmodels as sm`

Microsoft also documents the following libraries. They are available, but you have to import them yourself. The versions come from a `!pip list` that a user posted in May 2025. Microsoft updates the environment from time to time, and existing workbooks stay pinned to the environment they were created on, so treat these versions as an example and not a contract.

| Category | Library | Observed version |
|---|---|---|
| Data | pandas | 2.2.2 |
| Data | NumPy | 1.26.4 |
| Data | PyTables (`tables`) | not in the posted list |
| Stats / science | SciPy | 1.13.1 |
| Stats / science | statsmodels | 0.14.2 |
| Stats / science | SymPy | 1.12 |
| Stats / science | PyWavelets | 1.5.0 |
| Stats / science | Prince (multivariate EDA) | 0.13.0 |
| Stats / science | Astropy | 6.1.0 |
| ML | scikit-learn | 1.4.2 |
| ML | imbalanced-learn | 0.12.3 |
| ML | mlxtend | 0.23.1 |
| Viz | Matplotlib | 3.8.4 |
| Viz | seaborn | 0.13.2 |
| Viz | plotnine | 0.13.6 |
| Viz | squarify (treemaps) | 0.4.3 |
| Viz | wordcloud | 1.9.3 |
| NLP / text | NLTK, with the brown, punkt, stopwords, treebank, vader and wordnet2022 corpora preloaded | 3.8.1 |
| NLP / text | TheFuzz (fuzzy matching, on top of rapidfuzz) | 0.22.1 |
| NLP / text | snowballstemmer | 2.2.0 |
| Utilities | beautifulsoup4 (parse only; no fetching) | 4.12.3 |
| Utilities | Faker | 25.8.0 |
| Utilities | IPython | 8.25.0 |
| Utilities | Pillow | 10.3.0 |
| Utilities | qrcode | 7.4.2 |
| Utilities | tabulate | 0.9.0 |

The same user's listing shows more packages that Microsoft does not document but that are installed: openpyxl 3.1.2, xlrd 2.0.1, networkx 3.2.1, pyarrow 14.0.2, numba 0.59.1, dask, xarray, gensim, plotly, bokeh, holoviews/panel/datashader, the Jupyter stack, and requests (installed, but it cannot reach the network).

### Limits

- **No network.** "The Python code doesn't have network access." Libraries "will not have the ability to make network requests or access your files and data on your local machine."
- **No local files or user identity.** Code has no access to your computer, devices, account or user token. Data comes in only through `xl()` (cell ranges, tables and Power Query connections). Results go out only through the `=PY()` cell value, as a value or an image.
- **No pip install and no custom packages.** You can only import what Anaconda ships. Your local Python install has no effect, and Microsoft Q&A confirms that packages outside the set (for example QuantLib, Zipline and Pyfolio) are not available. Anaconda's separate *Anaconda Code* add-in is the route to custom packages. It runs locally on Pyodide/WASM and is not part of Python in Excel.
- **Python version.** Microsoft does not publish the Python version. The runtime is managed and updated automatically, with workbooks pinned to their environment. Anaconda's 2023 preview blog showed Python 3.9.10. The 2024–2025 package set above matches Anaconda's 2024 distributions, which are Python 3.12 (2024.10 ships 3.12.7).
- **Compute.** Containers are per-workbook, hypervisor-isolated and destroyed after use, and nothing persists between sessions. Formulas that run too long return `#TIMEOUT!` (the timeout is configurable in Options). The standard tier is slower, and a paid add-on gives premium compute.
- **Fonts.** Plots support only English, Chinese (Simplified), French, German, Japanese and Spanish fonts.

## Comparison with our image

The image is `python:3.12-slim`, and the wheels are installed `--only-binary` into `/app/pkgs`. Sizes are the approximate compressed wheels for cp312 manylinux x86_64, taken from PyPI's current releases. The installed size is usually 2–4× larger.

Status values:

- **In image:** pinned on master today.
- **Being added (#2517):** statsmodels, seaborn, openpyxl and networkx.
- **Recommended:** common, light and useful offline.
- **Optional:** light but niche. Add one when a user asks for it.
- **Skip:** heavy, needs the network, or not useful in a cell.

| Package | Excel | Status | Approx wheel | Notes |
|---|---|---|---|---|
| numpy | default import | In image | 17 MB | |
| pandas | default import | In image | 12 MB | |
| scipy | documented | In image | 35 MB | |
| matplotlib | default import | In image | 10 MB | |
| Pillow | documented | In image | 7 MB | |
| sympy (+ mpmath) | documented | In image | 7 MB | |
| pint | not shipped | In image | 0.4 MB | Our addition (units) |
| statsmodels (+ patsy) | default import | Being added (#2517) | 12 MB | |
| seaborn | default import | Being added (#2517) | 0.3 MB | |
| openpyxl | installed | Being added (#2517) | 0.3 MB | |
| networkx | documented | Being added (#2517) | 2 MB | |
| scikit-learn (+ joblib, threadpoolctl) | documented | Recommended | 10 MB | The ML package Excel users most expect |
| tabulate | documented | Recommended | <0.1 MB | Text tables |
| thefuzz + rapidfuzz | documented | Recommended | 3 MB | Fuzzy matching for joins and cleanup |
| beautifulsoup4 (+ soupsieve) | documented | Recommended | 0.2 MB | Parsing HTML strings already in cells |
| xlrd | installed | Recommended | 0.1 MB | Legacy `.xls`, complements openpyxl |
| plotnine (+ mizani) | documented | Optional | 2 MB | ggplot-style plots |
| PyWavelets | documented | Optional | 4.5 MB | |
| imbalanced-learn | documented | Optional | 0.2 MB | Needs scikit-learn |
| mlxtend | documented | Optional | 1.4 MB | Needs scikit-learn |
| prince | documented | Optional | 0.2 MB | Needs scikit-learn |
| Faker | documented | Optional | 2 MB | |
| qrcode | documented | Optional | <0.1 MB | |
| squarify | documented | Optional | <0.1 MB | |
| wordcloud | documented | Optional | 0.6 MB | |
| snowballstemmer | documented | Optional | 0.1 MB | |
| NLTK | documented | Optional | 1.7 MB + corpora | Useless without corpora, which have to be baked in because there is no network |
| IPython | documented | Skip | 0.6 MB + deps | Not useful in a cell |
| astropy | documented | Skip | 10 MB | Niche |
| tables (PyTables / HDF5) | documented | Skip | 7 MB | Niche, and pulls in native HDF5 |
| pyarrow | installed | Skip | 54 MB | Heavy. pandas works without it |
| numba + llvmlite | installed | Skip | 64 MB | Heavy JIT |
| gensim | installed | Skip | 28 MB | |
| plotly, bokeh, holoviews/panel | installed | Skip | 7–10 MB each | Interactive HTML, nowhere to render it |
| dask, xarray | installed | Skip | 1.5 MB each + deps | No benefit at sheet scale |
| Jupyter stack, requests | installed | Skip | — | No kernel UI and no network |
| scikit-image | not shipped | Skip | 14 MB | |
| torch / CUDA, transformers, docling-type stacks | not shipped | Skip | 500 MB+ | Keep out of the image |

## Recommended default list

Once #2517 lands, the image will hold numpy, pandas, scipy, matplotlib, Pillow, sympy, pint, statsmodels, seaborn, openpyxl and networkx. That covers Excel's five default imports.

I recommend adding **scikit-learn, tabulate, thefuzz (with rapidfuzz), beautifulsoup4 and xlrd** next. Together they are about 14 MB of compressed wheels, and they cover nearly everything a typical Python in Excel workbook imports. Leave the optional packages out until a user asks for one, and keep every package in the skip rows out of the image.

## Sources

- Microsoft, Open-source libraries and Python in Excel: https://support.microsoft.com/en-us/excel/python/open-source-libraries-and-python-in-excel
- Microsoft, Data security and Python in Excel: https://support.microsoft.com/en-us/excel/python/data-security-and-python-in-excel
- Microsoft, Python in Excel initialization settings: https://support.microsoft.com/en-us/excel/python/python-in-excel-initialization-settings
- Microsoft, Introduction to Python in Excel: https://support.microsoft.com/en-us/excel/python/introduction-to-python-in-excel
- Microsoft, Troubleshoot Python in Excel errors: https://support.microsoft.com/en-us/excel/python/troubleshoot-python-in-excel-errors
- Microsoft, Python in Excel availability: https://support.microsoft.com/en-us/excel/python/python-in-excel-availability
- Microsoft Q&A, Additional libraries for Python in Excel (user `!pip list`, May 2025): https://learn.microsoft.com/en-us/answers/questions/5453799/additional-libraries-for-python-in-excel
- Anaconda, Python in Excel: https://www.anaconda.com/partners/python-in-excel
- Anaconda, Create your first ML experiment with Python in Excel (Python 3.9.10 in preview): https://www.anaconda.com/blog/create-your-first-machine-learning-experiment-with-python-in-excel
- Anaconda, Distribution 2024.x release notes: https://www.anaconda.com/docs/getting-started/anaconda/release/2024.x
- Anaconda, Anaconda Code (local, custom packages): https://www.anaconda.com/docs/legacy/excel/code
- Wheel sizes: PyPI JSON API (`https://pypi.org/pypi/<name>/json`), October 2026
