import subprocess
import sys

res = subprocess.run(["pytest", "tests/legacy/test_formula_parsing.py"], env={"PYTHONPATH": "."})
sys.exit(res.returncode)
