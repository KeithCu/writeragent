# Vendored LSP helpers

| Module | Source | License |
|--------|--------|---------|
| `position_codec.py` | [pygls v2.1.1 `workspace/position_codec.py`](https://github.com/openlawlibrary/pygls/blob/v2.1.1/pygls/workspace/position_codec.py) | Apache-2.0 |
| `json_rpc_framing.py` | [pylspclient `json_rpc_endpoint.py`](https://github.com/yeger00/pylspclient) | MIT |

**Merge policy:** `lsprotocol` dependency removed from `position_codec.py`; `lsp_errors` removed from framing (WriterAgent uses stdlib exceptions). Refresh manually when upstream fixes are needed.
