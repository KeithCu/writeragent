import re

with open('plugin/writer/locale/harper.py', 'r') as f:
    content = f.read()

content = re.sub(
    r'(log\.exception\("\[harper\] lint failed on ready client; empty aErrors this walk"\)\n\s+with _HARPER_LOCK:\n\s+)_set_state\(HarperRuntimeState\.IDLE\)',
    r'\1_set_state(HarperRuntimeState.FAILED, failed_at=time.monotonic())',
    content
)

with open('plugin/writer/locale/harper.py', 'w') as f:
    f.write(content)
