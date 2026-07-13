# WriterAgent - Python Compute Service Server Tests
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import json
import socket
import threading
import time
import unittest
import urllib.request
import urllib.error
from compute_service.server import ThreadingHTTPServer, ComputeHandler

def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

class TestComputeServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.port = get_free_port()
        cls.server = ThreadingHTTPServer(("", cls.port), ComputeHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.url = f"http://localhost:{cls.port}"
        # Give the server a small moment to spin up
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def test_health_check(self) -> None:
        req = urllib.request.Request(f"{self.url}/health")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(body.get("status"), "healthy")

    def test_simple_execution(self) -> None:
        payload = {"code": "result = 3 ** 4"}
        req = urllib.request.Request(
            f"{self.url}/v1/execute",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(body.get("status"), "ok")
            self.assertEqual(body.get("result"), 81)

    def test_numpy_execution(self) -> None:
        payload = {
            "code": "import numpy as np\nresult = float(np.mean(data))",
            "data": [10, 20, 30, 40]
        }
        req = urllib.request.Request(
            f"{self.url}/v1/execute",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(body.get("status"), "ok")
            self.assertEqual(body.get("result"), 25.0)

    def test_sandboxed_safety_blocked_import(self) -> None:
        payload = {"code": "import os\nresult = os.name"}
        req = urllib.request.Request(
            f"{self.url}/v1/execute",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(body.get("status"), "error")
            self.assertIn("not allowed", body.get("message", ""))

    def test_matplotlib_plot_serialization(self) -> None:
        payload = {
            "code": (
                "import matplotlib.pyplot as plt\n"
                "fig, ax = plt.subplots()\n"
                "ax.plot([0, 1], [0, 1])\n"
                "result = fig"
            )
        }
        req = urllib.request.Request(
            f"{self.url}/v1/execute",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(body.get("status"), "ok")
            result = body.get("result")
            self.assertIsInstance(result, dict)
            self.assertEqual(result.get("__wa_payload__"), "image")
            self.assertEqual(result.get("format"), "svg")
            # Verify it is base64 encoded
            img_data = result.get("data")
            self.assertTrue(len(img_data) > 0)
            # Try to decode to ensure it is valid base64
            decoded = base64.b64decode(img_data)
            self.assertTrue(b"svg" in decoded or b"xml" in decoded)

if __name__ == "__main__":
    unittest.main()
