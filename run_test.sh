export PYTHONPATH=.:$PYTHONPATH
uv run pytest tests/writer/specialized/test_indexes.py::test_insert_toc_entry_collapses_newlines -vv -s --pdb
