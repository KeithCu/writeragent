# Due to the sandbox instability and the complexity of `test_venv_worker.py` mocks that require extensive updates for my split refactor,
# I will rollback `tests/scripting/test_venv_worker.py` and leave it for the human as suggested by "submit what you have now as the PR to master ... and I'll finish the tests on the branch myself".
import os
os.system("git checkout tests/scripting/test_venv_worker.py")
