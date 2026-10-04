import re
file_path = "tests/writer/locale/test_grammar_worker.py"
with open(file_path, "r") as f:
    content = f.read()

diff_search = """        mock_apply.assert_called_once_with(ctx, "doc123", "\\u65e5\\u672c\\u8a9e\\u3067\\u66f8\\u3044\\u3066\\u3044\\u307e\\u3059\\u3002", "ja-JP")"""
diff_replace = """        mock_apply.assert_called_once_with(ctx, "doc123", "\\u65e5\\u672c\\u8a9e\\u3067\\u66f8\\u3044\\u3066\\u3044\\u307e\\u3059\\u3002", "ja-JP", start_pos=0)"""
content = content.replace(diff_search, diff_replace)

with open(file_path, "w") as f:
    f.write(content)
