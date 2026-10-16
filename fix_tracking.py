with open("plugin/writer/tracking.py", "r") as f:
    text = f.read()

import re

# Block 6
pattern6 = re.compile(r'            try:\n                start = target_redline\.getPropertyValue\("RedlineStart"\)(.*?)            finally:(.*?)        except Exception as e:\n            return self\._tool_error\(f"Failed to process change \{index\}: \{e\}"\)\n', re.DOTALL)
def repl(m):
    body1 = "                start = target_redline.getPropertyValue(\"RedlineStart\")" + m.group(1)
    body1_dedented = "\n".join([line[4:] if line.startswith("            ") else line for line in body1.split("\n")])
    body2 = m.group(2)
    return body1_dedented + "            finally:" + body2
text = pattern6.sub(repl, text)

with open("plugin/writer/tracking.py", "w") as f:
    f.write(text)
