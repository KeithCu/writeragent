import re

with open("plugin/chatbot/web_research.py", "r") as f:
    content = f.read()

search_block = """class VisitWebpageCdpTool(Tool):
    name: str = "visit_webpage"
    description: str = "Visits a webpage at the given url and reads its content as a markdown string. Use this to browse webpages."
    inputs: dict[str, dict[str, str | type[Any] | bool]] = {"url": {"type": "string", "description": "The url of the webpage to visit."}}
    output_type: str = "string"
    cdp_url: str
    max_output_length: int"""

replace_block = """class VisitWebpageCdpTool(Tool):
    name: str = "visit_webpage"
    description: str = "Visits a webpage at the given url and reads its content as a markdown string. Use this to browse webpages."
    inputs: dict[str, dict[str, str | type[Any] | bool]] = {"url": {"type": "string", "description": "The url of the webpage to visit."}}
    output_type: str = "string"
    cdp_url: str
    max_output_length: int
    stop_checker: Any"""

content = content.replace(search_block, replace_block)

with open("plugin/chatbot/web_research.py", "w") as f:
    f.write(content)
