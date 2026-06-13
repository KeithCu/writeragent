import pytest

class MockModel:
    def __init__(self):
        self.Text = ""

class MockCtrl:
    def __init__(self):
        self._model = MockModel()
    def getModel(self):
        return self._model

# Helper to mimic execute_on_main_thread (calls instantly)
def execute_on_main_thread(func):
    func()


def test_heartbeat_updates_results_ctrl(monkeypatch):
    # Setup
    results_ctrl = MockCtrl()
    hb_data = {}

    # Define heartbeat function as in SearchDialog._run_rebuild
    def heartbeat_fn(payload: dict):
        file = payload.get("file")
        if not file:
            return
        phase = payload.get("phase")
        now = time.time()
        if phase == "extract":
            hb_data[file] = {"start": now, "paragraphs": payload.get("paragraphs", 0)}
        else:
            info = hb_data.get(file)
            if info is not None:
                elapsed = now - info["start"]
                para = info.get("paragraphs", 0)
                line = f"{file}: {para} chunks, {elapsed:.2f}s"
                def ui_update():
                    existing = results_ctrl.getModel().Text
                    new_text = (existing + "\n" if existing else "") + line
                    results_ctrl.getModel().Text = new_text
                execute_on_main_thread(ui_update)
                del hb_data[file]

    # Simulate start of extraction with 5 paragraphs
    start_time = 1000.0
    monkeypatch.setattr(time, "time", lambda: start_time)
    heartbeat_fn({"file": "test.txt", "phase": "extract", "paragraphs": 5})

    # Simulate end of processing after 1.234 seconds
    monkeypatch.setattr(time, "time", lambda: start_time + 1.234)
    heartbeat_fn({"file": "test.txt", "phase": "embed"})

    # Verify results_ctrl contains the expected line
    expected_line = "test.txt: 5 chunks, 1.23s"
    assert results_ctrl.getModel().Text == expected_line
