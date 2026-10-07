from compute_service.config import read_allowlisted_file
import os
import tempfile

def test_open_allowed_file_fifo():
    with tempfile.TemporaryDirectory() as tmpdir:
        fifo_path = os.path.join(tmpdir, "my_fifo")
        os.mkfifo(fifo_path)
        val, err = read_allowlisted_file(fifo_path, allow_prefixes=(tmpdir,), max_bytes=100)
        assert err["code"] == "NOT_A_FILE"
