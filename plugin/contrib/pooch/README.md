# Vendored Pooch v1.8.2 subset (BSD-3-Clause)

Pruned from [fatiando/pooch](https://github.com/fatiando/pooch) for Harper binary download/cache:

- `core.py` — `retrieve()`, `stream_download()`, `download_action()`
- `hashes.py` — SHA256 verification (xxhash optional block removed)
- `utils.py` — `get_logger`, `make_local_storage`, `temporary_file` only
- `downloaders.py` — `HTTPDownloader` using stdlib `urllib` (upstream uses `requests`)
- `processors.py` — `Untar`, `Unzip` with path-traversal guard on member names

Not included: `Pooch` registry class, FTP/SFTP/DOI downloaders, `Decompress`, `os_cache` (`platformdirs`).
