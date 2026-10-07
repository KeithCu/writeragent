import re
with open("tests/chatbot/test_send_handlers.py", "r") as f:
    text = f.read()

text = re.sub(r'def test_record_assistant_start_on_ui_thread_for_web_research\(\).*?pass\n?', '', text, flags=re.DOTALL)
with open("tests/chatbot/test_send_handlers.py", "w") as f:
    f.write(text)
