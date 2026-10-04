
with open("plugin/notebook/notebook_controls.py", "r") as f:
    code = f.read()

code = code.replace(
"""class NotebookFormContainerListener(BaseContainerListener):
    \"\"\"When the form view realizes another control, attach the shared ▶ listener.\"\"\"

    _form_listener: NotebookFormRunListener
    _doc_key_val: str
    _form_level: bool

    def __init__(self, form_listener: NotebookFormRunListener, container: Any) -> None:""",
"""class NotebookFormContainerListener(BaseContainerListener):
    \"\"\"When the form view realizes another control, attach the shared ▶ listener.\"\"\"

    _form_listener: NotebookFormRunListener
    _doc_key_val: str
    _form_level: bool
    _container: Any

    def __init__(self, form_listener: NotebookFormRunListener, container: Any) -> None:"""
)

with open("plugin/notebook/notebook_controls.py", "w") as f:
    f.write(code)
