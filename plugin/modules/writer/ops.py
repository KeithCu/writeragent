import json

from plugin.framework.logging import debug_log
from plugin.modules.core.services.document import (
    get_paragraph_ranges,
    find_paragraph_for_range,
    build_heading_tree,
    ensure_heading_bookmarks,
    resolve_locator,
    get_document_length
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _err(message):
    return json.dumps({"status": "error", "message": message})
