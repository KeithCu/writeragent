#!/usr/bin/env python3
"""
Generate Grammar Checker Flowchart Diagram for WriterAgent.

Shows the async grammar checking architecture:
debounce -> queue (latest per paragraph) -> LLM request -> sentence cache -> return cached or new results.

Colors: blues for UI flow, reds for delays/caches.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, List


class FlowColor(str, Enum):
    """Color categories for the flowchart"""
    UI_FLOW = "ui_flow"      # Blues for UI/main thread flow
    DELAY_CACHE = "delay_cache"  # Reds for delays and caches
    PROCESSING = "processing"  # Greens for LLM processing


@dataclass
class FlowNode:
    """Represents a node in the grammar checker flowchart"""
    id: str
    label: str
    description: str
    color: FlowColor
    shape: str = "box"  # box, round, diamond, cylinder, etc.


@dataclass
class FlowEdge:
    """Represents an edge/connection between nodes"""
    from_node: str
    to_node: str
    label: str
    condition: Optional[str] = None


class GrammarCheckerFlowchart:
    """Grammar checker async architecture flowchart"""

    def __init__(self):
        self.nodes = self._define_nodes()
        self.edges = self._define_edges()
        self.node_map = {n.id: n for n in self.nodes}

    def _define_nodes(self) -> List[FlowNode]:
        """Define all nodes in the grammar checking flow"""
        return [
            # UI / Main Thread Flow (Blues)
            FlowNode(
                id="writer_ui",
                label="Writer UI",
                description="LibreOffice Writer user interface - user types text",
                color=FlowColor.UI_FLOW,
                shape="box"
            ),
            FlowNode(
                id="proofreading_call",
                label="doProofreading()",
                description="XProofreader.doProofreading called by LibreOffice Linguistic2",
                color=FlowColor.UI_FLOW,
                shape="round"
            ),
            FlowNode(
                id="check_enabled",
                label="Check Enabled",
                description="Check if doc.grammar_proofreader_enabled is true",
                color=FlowColor.UI_FLOW,
                shape="diamond"
            ),
            FlowNode(
                id="batch_check",
                label="nStartOfSentencePosition == 0",
                description="Only process on sentence-start pass (Lightproof pattern)",
                color=FlowColor.UI_FLOW,
                shape="diamond"
            ),
            FlowNode(
                id="cap_text",
                label="Cap at 500 chars",
                description="Limit to GRAMMAR_PROOFREAD_MAX_CHARS for LLM",
                color=FlowColor.UI_FLOW,
                shape="box"
            ),
            FlowNode(
                id="split_sentences",
                label="Split into Sentences",
                description="Use BreakIterator or regex to split text into sentences",
                color=FlowColor.UI_FLOW,
                shape="box"
            ),
            FlowNode(
                id="cache_lookup",
                label="Sentence Cache Lookup",
                description="Check cache for each sentence (locale + fingerprint key)",
                color=FlowColor.UI_FLOW,
                shape="diamond"
            ),
            FlowNode(
                id="return_cached",
                label="Return Cached Errors",
                description="All sentences cached - return immediately with cached errors",
                color=FlowColor.UI_FLOW,
                shape="box"
            ),
            FlowNode(
                id="partial_cache_hit",
                label="Partial Cache Hit",
                description="Return cached errors now, enqueue uncached sentences",
                color=FlowColor.UI_FLOW,
                shape="box"
            ),
            FlowNode(
                id="return_empty",
                label="Return Empty (Async)",
                description="Cache miss - return empty, squiggles appear later",
                color=FlowColor.UI_FLOW,
                shape="box"
            ),
            FlowNode(
                id="emit_status",
                label="Emit Status",
                description="Emit grammar:status event for sidebar progress",
                color=FlowColor.UI_FLOW,
                shape="box"
            ),
            
            # Delay / Cache Components (Reds)
            FlowNode(
                id="enqueue",
                label="Enqueue Work Item",
                description="Add GrammarWorkItem to _GrammarWorkQueue with sequence number",
                color=FlowColor.DELAY_CACHE,
                shape="cylinder"
            ),
            FlowNode(
                id="work_queue",
                label="_GrammarWorkQueue",
                description="Single sequential worker thread with queue.Queue",
                color=FlowColor.DELAY_CACHE,
                shape="cylinder"
            ),
            FlowNode(
                id="debounce",
                label="1s Debounce",
                description="GRAMMAR_WORKER_PAUSE_TIMEOUT_S - wait for typing to pause",
                color=FlowColor.DELAY_CACHE,
                shape="diamond"
            ),
            FlowNode(
                id="deduplicate",
                label="Deduplicate Batch",
                description="Keep newest per inflight_key (doc_id|locale), drop prefix conflicts",
                color=FlowColor.DELAY_CACHE,
                shape="box"
            ),
            FlowNode(
                id="sentence_cache",
                label="Sentence Cache (LRU 2048)",
                description="_SENTENCE_CACHE: OrderedDict with locale|fp keys",
                color=FlowColor.DELAY_CACHE,
                shape="cylinder"
            ),
            FlowNode(
                id="prefix_compaction",
                label="Prefix Compaction",
                description="Evict incomplete strict-prefix predecessors (max 10 scan)",
                color=FlowColor.DELAY_CACHE,
                shape="box"
            ),
            
            # LLM Processing (Greens)
            FlowNode(
                id="filter_uncached",
                label="Filter Uncached Sentences",
                description="Only send sentences not in cache to LLM",
                color=FlowColor.PROCESSING,
                shape="box"
            ),
            FlowNode(
                id="llm_request",
                label="LLM Request",
                description="Send to LlmClient with grammar system prompt + response_format=json",
                color=FlowColor.PROCESSING,
                shape="round"
            ),
            FlowNode(
                id="parse_response",
                label="Parse JSON Response",
                description="Parse errors from LLM JSON with safe_json_loads + json_repair",
                color=FlowColor.PROCESSING,
                shape="box"
            ),
            FlowNode(
                id="normalize_errors",
                label="Normalize Errors",
                description="Map wrong substrings to absolute positions, handle overlaps",
                color=FlowColor.PROCESSING,
                shape="box"
            ),
            FlowNode(
                id="cache_put",
                label="Cache Sentence Errors",
                description="Store normalized errors in sentence cache per sentence",
                color=FlowColor.DELAY_CACHE,
                shape="cylinder"
            ),
            FlowNode(
                id="llm_concurrency",
                label="llm_request_lane()",
                description="Prevent concurrent grammar/chat LLM calls",
                color=FlowColor.DELAY_CACHE,
                shape="box"
            ),
        ]

    def _define_edges(self) -> List[FlowEdge]:
        """Define all connections between nodes"""
        return [
            # Main UI flow path
            FlowEdge("writer_ui", "proofreading_call", "User types\nLO calls doProofreading"),
            FlowEdge("proofreading_call", "check_enabled", ""),
            FlowEdge("check_enabled", "batch_check", "Enabled"),
            FlowEdge("batch_check", "cap_text", "nStart == 0"),
            FlowEdge("cap_text", "split_sentences", ""),
            FlowEdge("split_sentences", "cache_lookup", ""),
            
            # Cache hit paths
            FlowEdge("cache_lookup", "return_cached", "All cached"),
            FlowEdge("cache_lookup", "partial_cache_hit", "Partial hit"),
            FlowEdge("partial_cache_hit", "enqueue", "Has uncached"),
            FlowEdge("partial_cache_hit", "return_empty", "Also return cached now"),
            FlowEdge("cache_lookup", "enqueue", "Cache miss"),
            FlowEdge("enqueue", "emit_status", ""),
            FlowEdge("enqueue", "return_empty", ""),
            
            # Queue processing path
            FlowEdge("enqueue", "work_queue", "Add with seq#"),
            FlowEdge("work_queue", "debounce", "Worker thread waits"),
            FlowEdge("debounce", "deduplicate", "Pause expired\nBatch collected"),
            FlowEdge("deduplicate", "filter_uncached", "Survivors after dedup"),
            
            # LLM processing path
            FlowEdge("filter_uncached", "llm_concurrency", "Has uncached sentences"),
            FlowEdge("llm_concurrency", "llm_request", "Acquired lane"),
            FlowEdge("llm_request", "parse_response", "JSON response"),
            FlowEdge("parse_response", "normalize_errors", "Parsed items"),
            FlowEdge("normalize_errors", "cache_put", "Normalized errors"),
            FlowEdge("cache_put", "emit_status", "Cached complete"),
            
            # Cache compaction
            FlowEdge("cache_put", "prefix_compaction", "If incomplete sentence"),
            FlowEdge("prefix_compaction", "sentence_cache", "Evict prefix predecessors"),
            
            # Skip paths
            FlowEdge("check_enabled", "return_empty", "Disabled", "Disabled"),
            FlowEdge("batch_check", "return_empty", "nStart != 0", "Incremental call"),
        ]

    def to_mermaid(self) -> str:
        """Convert flowchart to Mermaid.js diagram"""
        lines = []
        lines.append("flowchart TB")
        lines.append("    %% Grammar Checker Async Architecture - Top to Bottom")
        lines.append("    %% Blues: UI flow, Reds: Delays/Caches, Greens: LLM Processing")
        lines.append("")
        
        # Color definitions
        lines.append("    classDef ui_flow fill:#4169e1,color:white,stroke:#000080")
        lines.append("    classDef delay_cache fill:#dc143c,color:white,stroke:#8b0000")
        lines.append("    classDef processing fill:#228b22,color:white,stroke:#006400")
        lines.append("")
        
        # Node definitions with shapes
        # Mermaid flowchart shapes: box (default), round (stadium), diamond, cylinder
        for node in self.nodes:
            if node.shape == "cylinder":
                # Cylinder: [(label)] syntax
                lines.append(f"    {node.id}[(\"{node.label}\")]")
            elif node.shape == "round":
                # Round/Stadium: (label)
                lines.append(f"    {node.id}(\"{node.label}\")")
            elif node.shape == "diamond":
                # Diamond: {label}
                lines.append(f"    {node.id}{{\"{node.label}\"}}")
            else:  # box
                # Box: [label]
                lines.append(f"    {node.id}[\"{node.label}\"]")
            lines.append(f"    class {node.id} {node.color.value}")
        lines.append("")
        
        # Edge definitions
        for edge in self.edges:
            from_id = edge.from_node
            to_id = edge.to_node
            label = edge.label.replace("\n", "<br>")
            if edge.condition:
                lines.append(f"    {from_id} -->|\"{label}\"| {to_id}")
            elif label:
                lines.append(f"    {from_id} -- \"{label}\" --> {to_id}")
            else:
                lines.append(f"    {from_id} --> {to_id}")
        
        return "\n".join(lines)

    def to_markdown(self) -> str:
        """Generate comprehensive markdown documentation"""
        lines = []
        lines.append("# Grammar Checker Async Architecture")
        lines.append("")
        lines.append("Flowchart showing the async grammar checking pipeline in WriterAgent.")
        lines.append("")
        lines.append("## Flow Description")
        lines.append("")
        lines.append("```")
        lines.append("User types in Writer")
        lines.append("    ↓")
        lines.append("LibreOffice calls doProofreading() [XProofreader]")
        lines.append("    ↓")
        lines.append("Check if grammar checker enabled (doc.grammar_proofreader_enabled)")
        lines.append("    ↓ (if disabled, return empty immediately)")
        lines.append("    ↓ (if nStartOfSentencePosition != 0, return empty - incremental)")
        lines.append("Cap text at 500 chars (GRAMMAR_PROOFREAD_MAX_CHARS)")
        lines.append("    ↓")
        lines.append("Split into sentences (BreakIterator or regex)")
        lines.append("    ↓")
        lines.append("Cache lookup per sentence (locale + normalized fingerprint)")
        lines.append("    ├─ All cached → Return cached errors immediately")
        lines.append("    ├─ Partial hit → Return cached NOW + enqueue uncached")
        lines.append("    └─ Cache miss → Enqueue work item")
        lines.append("    ↓")
        lines.append("Emit grammar:status event → Sidebar progress")
        lines.append("    ↓")
        lines.append("Return empty errors (async - squiggles appear later)")
        lines.append("    ↓")
        lines.append("─ Queue Processing (Background Thread) ─────────────────────")
        lines.append("")
        lines.append("_GrammarWorkQueue (daemon thread)")
        lines.append("    ↓")
        lines.append("Wait 1s debounce (GRAMMAR_WORKER_PAUSE_TIMEOUT_S)")
        lines.append("    ↓")
        lines.append("Batch drain + deduplicate (keep newest per doc_id|locale)")
        lines.append("    ↓")
        lines.append("Filter to uncached sentences only")
        lines.append("    ↓")
        lines.append("llm_request_lane() - prevent concurrent LLM calls")
        lines.append("    ↓")
        lines.append("LLM Request (chat_completion_sync with response_format=json)")
        lines.append("    ↓")
        lines.append("Parse JSON response (safe_json_loads + json_repair)")
        lines.append("    ↓")
        lines.append("Normalize errors (map to absolute positions, handle overlaps)")
        lines.append("    ↓")
        lines.append("Cache sentence errors (LRU 2048 entries)")
        lines.append("    ↓")
        lines.append("Prefix compaction: evict incomplete strict-prefix predecessors")
        lines.append("    ↓")
        lines.append("Emit grammar:status complete event")
        lines.append("    ↓")
        lines.append("Writer shows grammar squiggles (on next LO proofreading pass)")
        lines.append("```")
        lines.append("")
        
        # Component legend
        lines.append("## Color Legend")
        lines.append("")
        lines.append("| Color | Category | Description |")
        lines.append("|-------|----------|-------------|")
        lines.append("| Blue (#4169e1) | UI Flow | Main thread, LibreOffice integration |")
        lines.append("| Red (#dc143c) | Delays/Caches | Queue, debounce, deduplication, sentence cache |")
        lines.append("| Green (#228b22) | LLM Processing | LLM requests, parsing, error normalization |")
        lines.append("")
        
        # Shape legend
        lines.append("## Shape Legend")
        lines.append("")
        lines.append("| Shape | Meaning |")
        lines.append("|-------|---------|")
        lines.append("| Box | Process/Action |")
        lines.append("| Round | External/Entry Point |")
        lines.append("| Diamond | Decision/Condition |")
        lines.append("| Cylinder | Data Store/Cache |")
        lines.append("")
        
        # Key Components
        lines.append("## Key Components")
        lines.append("")
        lines.append("### Files Involved")
        lines.append("")
        lines.append("1. **`ai_grammar_proofreader.py`** - UNO XProofreader implementation")
        lines.append("   - `WriterAgentAiGrammarProofreader` class")
        lines.append("   - `doProofreading()` - Main entry point from LibreOffice")
        lines.append("   - `_GrammarWorkQueue` - Sequential worker thread")
        lines.append("   - `_run_llm_and_cache()` - LLM execution and caching")
        lines.append("")
        lines.append("2. **`grammar_proofread_engine.py`** - Pure Python helpers")
        lines.append("   - `Sentence cache` (_SENTENCE_CACHE) - LRU OrderedDict, max 2048")
        lines.append("   - `GrammarWorkItem` - Dataclass for queue items")
        lines.append("   - `deduplicate_grammar_batch()` - Newest-wins dedup logic")
        lines.append("   - `parse_grammar_json()` - Robust JSON parsing")
        lines.append("   - `normalize_errors_for_text()` - Position mapping")
        lines.append("   - `split_into_sentences()` - Sentence splitting")
        lines.append("   - `cache_put_sentence()` - Cache with prefix compaction")
        lines.append("")
        
        # Flow Details
        lines.append("## Detailed Flow Notes")
        lines.append("")
        lines.append("### Debounce Mechanism")
        lines.append("- Worker waits **1 second** (GRAMMAR_WORKER_PAUSE_TIMEOUT_S) after last enqueue")
        lines.append("- Prevents LLM calls while user is actively typing")
        lines.append("- Reduces backend stampedes and unnecessary calls")
        lines.append("")
        lines.append("### Deduplication")
        lines.append("- **inflight_key**: `{doc_id}|{locale}` (no text fingerprint)")
        lines.append("- Keeps only newest per key using sequence numbers")
        lines.append("- Within (doc_id, locale) group: drops prefix-related conflicts (newest wins)")
        lines.append("- Handles mid-sentence edits, not just growing-prefix typing")
        lines.append("")
        lines.append("### Sentence Cache")
        lines.append("- Key: `sent|{locale}|{sha256_fingerprint}`")
        lines.append("- Normalization: strips trailing whitespace + ignores punctuation after first terminator")
        lines.append("- " + "`Hello.` and `Hello...` share same cache entry")
        lines.append("- " + "`Hello?` and `Hello?...` share same cache entry")
        lines.append("- Complete sentences are **protected** from prefix eviction")
        lines.append("")
        lines.append("### Prefix Compaction")
        lines.append("- For incomplete sentences: scan newest 10 cache entries")
        lines.append("- Evict strict-prefix predecessors of same locale")
        lines.append("- Example: typing \"The qu\", \"The qui\", ..., \"The quick\" → only \"The quick\" cached")
        lines.append("- Prevents LRU churn during incremental typing")
        lines.append("- O(n) where n ≤ 10 → very fast")
        lines.append("")
        lines.append("### LLM Call Optimization")
        lines.append("- Only uncached sentences sent to LLM")
        lines.append("- Concatenated with space separator")
        lines.append("- `response_format={'type': 'json_object'}` for OpenAI-compatible")
        lines.append("- Fixed budget: GRAMMAR_PROOFREAD_MAX_RESPONSE_TOKENS = 512")
        lines.append("- `llm_request_lane()` prevents concurrent grammar/chat LLM calls")
        lines.append("")
        
        return "\n".join(lines)


def main():
    """Generate grammar checker diagram"""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python generate_grammar_checker_diagram.py <format>")
        print("Formats: mermaid, markdown")
        sys.exit(1)

    format_type = sys.argv[1].lower()
    flowchart = GrammarCheckerFlowchart()

    if format_type == "mermaid":
        print(flowchart.to_mermaid())
    elif format_type == "markdown":
        print(flowchart.to_markdown())
    else:
        print(f"Unknown format: {format_type}")
        sys.exit(1)


if __name__ == "__main__":
    main()
