import subprocess
import sys

res = subprocess.run(["pytest", "tests/legacy/test_csv_import_logic.py"], env={"PYTHONPATH": "."})
sys.exit(res.returncode)
