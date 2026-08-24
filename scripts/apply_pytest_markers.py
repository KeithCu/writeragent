import os
import re

MARKER_MAP = {
    "lo": "pytest.mark.lo",
    "eval": "pytest.mark.eval",
    "unit": "pytest.mark.unit"
}

for root, _, files in os.walk("tests"):
    for file in files:
        if file.startswith("test_") and file.endswith(".py"):
            filepath = os.path.join(root, file)
            with open(filepath, 'r') as f:
                content = f.read()

            categories = set()
            if "import uno" in content or "from uno " in content or filepath.endswith("_uno.py"):
                categories.add("lo")
            if "pytest.mark.eval" in content or "eval" in filepath.lower():
                categories.add("eval")

            if "lo" in categories:
                if "MagicMock" in content and "mock" in content.lower():
                    if "patch(" in content or "MagicMock(spec=X" in content:
                         if not filepath.endswith("_uno.py"):
                             categories.remove("lo")
                             categories.add("unit")

            if not categories:
                categories.add("unit")

            if "pytestmark =" in content:
                continue

            has_pytest = "import pytest" in content

            marker_list = [MARKER_MAP[c] for c in categories]
            marker_stmt = f"pytestmark = [{', '.join(marker_list)}]"

            lines = content.split('\n')

            insert_idx = 0
            if not has_pytest:
                for i, line in enumerate(lines):
                    if line.startswith("import ") or line.startswith("from "):
                        if "from __future__" in line:
                            continue
                        insert_idx = i
                        break
                if insert_idx == 0:
                    insert_idx = 1
                lines.insert(insert_idx, "import pytest")
                lines.insert(insert_idx + 1, marker_stmt)
                lines.insert(insert_idx + 2, "")
            else:
                for i, line in enumerate(lines):
                    if line.startswith("import ") or line.startswith("from "):
                        if "from __future__" in line:
                            continue
                        insert_idx = i
                        break
                lines.insert(insert_idx, marker_stmt)
                lines.insert(insert_idx + 1, "")

            with open(filepath, 'w') as f:
                f.write('\n'.join(lines))

print("Applied pytest markers to tests/")
