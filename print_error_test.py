import sys
import os
os.environ["UNIT_TESTING"] = "1"
from unittest.mock import MagicMock, patch
from plugin.writer.specialized.indexes import IndexesInsertTocEntry
tool = IndexesInsertTocEntry()
ctx = MagicMock()
doc = ctx.doc
doc.getDocumentIndexes.return_value.getCount.return_value = 1
idx = MagicMock()
idx.getServiceName.return_value = "com.sun.star.text.ContentIndex"
doc.getDocumentIndexes.return_value.getByIndex.return_value = idx
with patch("plugin.writer.specialized.indexes._paragraphs_in_anchor", return_value=[]):
    res = tool.execute(ctx, content="new\nline\r", page="1\n2\r", hyperlink_url="#target\n|\routline", dry_run=True)
    print(res)
