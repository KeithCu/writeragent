import json
import time
import ast
import traceback
from plugin.framework.http import LlmClient
from plugin.modules.core.services.config import get_api_config
from plugin.framework.logging import debug_log, agent_log
from plugin.modules.core.services.document import get_document_context_for_chat
from plugin.framework.constants import get_chat_system_prompt_for_document
from plugin.modules.core.pricing import fetch_openrouter_pricing, calculate_cost
from plugin.main import get_tools

class EvalRunner:
    def __init__(self, ctx, doc, model_name=None):
        self.ctx = ctx
        self.doc = doc
        self.model_name = model_name
        self.results = []
        self.passed = 0
        self.failed = 0
        self.total_cost = 0.0
        
        # Ensure fresh pricing for benchmarks
        fetch_openrouter_pricing(ctx)
        
        # Build API config for this specific run
        self.api_config = get_api_config(ctx)
        if model_name:
            self.api_config["model"] = model_name
        
        self.client = LlmClient(self.api_config, ctx)

    def run_test(self, name, task, category="Writer", verify_fn=None):
        """Run a single benchmark test with optional verification function."""
        debug_log(f"Eval: Running {name}...", context="Eval")
        
        # 0. Capture state before
        pre_state = self._get_document_state(category)
        
        # 1. Capture initial state/context
        system_prompt = get_chat_system_prompt_for_document(self.doc, "")
        doc_context = get_document_context_for_chat(self.doc, 8000, ctx=self.ctx)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Document Context:\n{doc_context}\n\nTask: {task}"}
        ]
        
        # Choose tools based on category
        registry = get_tools()
        doc_type = category.lower()
        if doc_type == "multimodal":
            tools = registry.get_openai_schemas()
        else:
            tools = registry.get_openai_schemas(doc_type=doc_type)
            
        start_time = time.time()
        try:
            # 2. Execute agent turn
            response = self.client.request_with_tools(messages, max_tokens=1024, tools=tools)
            
            # Accurate cost calculation
            usage = response.get("usage", {})
            turn_cost = calculate_cost(self.ctx, usage, self.api_config.get("model"))
            self.total_cost += turn_cost
            
            # 3. Process tool calls
            tool_calls = response.get("tool_calls", [])
            for tc in tool_calls:
                func = tc.get("function", {})
                t_name = func.get("name")
                t_args_str = func.get("arguments", "{}")
                try:
                    t_args = json.loads(t_args_str)
                except:
                    t_args = {}
                
                # Execute the tool with appropriate dispatcher
                try:
                    from plugin.framework.tool_context import ToolContext
                    tctx = ToolContext(self.doc, self.ctx, doc_type, {}, "eval")
                    registry.execute(t_name, tctx, **t_args)
                except Exception as e:
                    debug_log(f"Tool {t_name} failed: {e}", context="Eval")
            
            # 4. Success Verification
            passed = False
            if verify_fn:
                passed = verify_fn(self.doc, self.ctx, pre_state)
            else:
                # Use LLM-as-a-Judge for semantic verification
                passed = self._verify_with_judge(name, task, category)
                
            if passed:
                self.passed += 1
                self.results.append({"name": name, "status": "OK", "cost": turn_cost, "latency": time.time() - start_time})
            else:
                self.failed += 1
                self.results.append({"name": name, "status": "FAIL (Verification)", "cost": turn_cost, "latency": time.time() - start_time})
                
        except Exception as e:
            self.failed += 1
            self.results.append({"name": name, "status": f"ERROR: {str(e)}", "cost": 0, "latency": time.time() - start_time})
            debug_log(f"Eval Error in {name}: {traceback.format_exc()}", context="Eval")

    def _verify_with_judge(self, name, task, category):
        """Use a cheaper/different model to judge if the task was successful."""
        debug_log(f"Eval: Judging {name}...", context="Eval")
        # Snapshot current state
        doc_context = get_document_context_for_chat(self.doc, 8000, ctx=self.ctx)
        
        judge_prompt = f"""
You are an expert evaluator for LibreOffice automation.
The user wanted to perform this task: {task}
In the context of this document type: {category}

Current Document Content/State:
{doc_context}

Did the model successfully complete the requested task? 
Respond with only 'YES' or 'NO' and a short reason on the next line.
"""
        try:
            # Use request_with_tools for a blocking call to the judge
            response = self.client.request_with_tools([{"role": "user", "content": judge_prompt}], max_tokens=100)
            
            # Track judge cost
            usage = response.get("usage", {})
            cost = calculate_cost(self.ctx, usage, self.api_config.get("model"))
            self.total_cost += cost
            
            content = (response.get("content") or "").upper()
            return "YES" in content
        except Exception as e:
            debug_log(f"Judge failed: {e}", context="Eval")
            return False

    def _get_document_state(self, category):
        """Helper to snapshot document state (rudimentary)."""
        if category == "Calc":
            # For Calc, maybe get active sheet name or a few cell values
            return {"sheet": self.doc.getCurrentController().getActiveSheet().getName()}
        return {}

    def get_summary(self):
        """Return a formatted summary string of the entire run."""
        total = self.passed + self.failed
        acc = (self.passed / total * 100) if total > 0 else 0
        ipd = (self.passed / self.total_cost) if self.total_cost > 0 else 0
        
        summary = [
            f"Benchmark Results for {self.model_name or 'Default Model'}:",
            f"  Passed: {self.passed} / {total} ({acc:.1f}%)",
            f"  Total Cost: ${self.total_cost:.4f}",
            f"  Intelligence-per-Dollar (IpD): {ipd:.1f} pass/$",
            f"  Avg Latency: {sum(r['latency'] for r in self.results)/len(self.results):.2f}s" if self.results else ""
        ]
        return "\n".join(filter(None, summary))

    def get_results(self):
        """Return the detailed results list."""
        return self.results

def run_benchmark_suite(ctx, doc, model_name=None, categories=["Writer", "Calc"]):
    """Main entry point to run the suite. Ported from EVALUATION_PLAN_DETAILED.md."""
    runner = EvalRunner(ctx, doc, model_name)
    
    # 📝 WRITER TESTS (20)
    if "Writer" in categories:
        # Essentials
        runner.run_test("Writer: Format Preservation", "Replace 'John Doe' with 'Jane Smith' in the header (Bold, 14pt).", category="Writer")
        runner.run_test("Writer: Style Application", "Make 'Introduction' a Heading 1.", category="Writer")
        runner.run_test("Writer: Comment Management", "Add a comment 'Review this' to the word 'Uncertain'.", category="Writer")
        runner.run_test("Writer: Bullet Consistency", "Ensure all bullet points in this list end with a period.", category="Writer")
        runner.run_test("Writer: Font Audit", "Change all text in 'Comic Sans' to 'Inter'.", category="Writer")
        
        # Advanced
        runner.run_test("Writer: Table Engineering", "Convert this comma-separated list into a 2-column table with headers.", category="Writer")
        runner.run_test("Writer: Markdown Import", "Replace the second paragraph with a Markdown table from the clipboard.", category="Writer")
        runner.run_test("Writer: TOC Generation", "Insert a Table of Contents at the start of the document.", category="Writer")
        runner.run_test("Writer: Section Break", "Insert a section break and set the next page to Landscape orientation.", category="Writer")
        runner.run_test("Writer: Bulk Cleanup", "Remove all double spaces and ensure every sentence is followed by exactly one space.", category="Writer")
        runner.run_test("Writer: Header/Footer", "Add page numbers in the footer and the document title in the header.", category="Writer")
        
        # Expert
        runner.run_test("Writer: Style Consistency", "Find all text in 'Default' style and change it to 'Quotations'.", category="Writer")
        runner.run_test("Writer: Track Changes Audit", "Accept all changes made by 'Reviewer A' but reject all by 'Reviewer B'.", category="Writer")
        runner.run_test("Writer: Bibliography Fix", "Locate all brackets [1], [2] and ensure they are superscripted.", category="Writer")
        runner.run_test("Writer: Smart Summarization", "Summarize the 'Finding' section into 5 bullet points and insert it into the 'Executive Summary'.", category="Writer")
        runner.run_test("Writer: Logical Rewriting", "Rewrite the third paragraph to be 'professional and concise' while preserving all technical terms.", category="Writer")
        runner.run_test("Writer: Refactoring Sections", "Move the 'Conclusion' after the 'Intro' and rename it 'Goal'.", category="Writer")
        runner.run_test("Writer: Style Mapping", "Map all 'Heading 2' text to become 'Heading 1' and adjust subsequent levels down.", category="Writer")
        runner.run_test("Writer: Conflict Resolution", "There are two definitions for 'API' in this doc. Merge them into one comprehensive definition.", category="Writer")
        runner.run_test("Writer: Final Polish", "Apply a consistent color theme (Blue/Gray) to all headings and tables.", category="Writer")

    # 📊 CALC TESTS (20)
    if "Calc" in categories:
        # Essentials
        runner.run_test("Calc: Formula Mapping", "Calculate the tax (8%) for Column B and put it in Column C.", category="Calc")
        runner.run_test("Calc: Sheet Creation", "Create a new sheet called 'Projections' and copy Column A there.", category="Calc")
        runner.run_test("Calc: Row Clean", "Remove all empty rows in Sheet1.", category="Calc")
        runner.run_test("Calc: Auto-Formatting", "Highlight all cells in Column D greater than 1000 in Red.", category="Calc")
        runner.run_test("Calc: Lookup Logic", "Use VLOOKUP to find the price of 'Apple' from the 'Prices' sheet.", category="Calc")
        
        # Advanced
        runner.run_test("Calc: Data Sorting", "Sort A1:D100 by 'Revenue' descending, after detecting the column.", category="Calc")
        runner.run_test("Calc: Error Debugging", "The formula in D10 is failing. Find out why and fix it.", category="Calc")
        runner.run_test("Calc: Named Ranges", "Create a named range 'SalesData' for A2:Z200.", category="Calc")
        runner.run_test("Calc: Validation", "Restrict Column F to only allow dates between 2020 and 2025.", category="Calc")
        runner.run_test("Calc: Data Transpose", "Take the row headers from A1:E1 and turn them into column headers in A1:A5.", category="Calc")
        runner.run_test("Calc: Pivot Setup", "Create a pivot table summary of this data onto a new sheet.", category="Calc")
        
        # Expert
        runner.run_test("Calc: Auto-Charting", "Create a line chart for the trends in A1:B12.", category="Calc")
        runner.run_test("Calc: Data Recovery", "Fix the broken CSV import that shifted everything by one column.", category="Calc")
        runner.run_test("Calc: Consolidation", "Sum all Column B values from Sheet1, Sheet2, and Sheet3 into Sheet4.", category="Calc")
        runner.run_test("Calc: Conditional Chains", "If Column A is 'Profit', set Column B to 'Green'; if 'Loss', set to 'Red'.", category="Calc")
        runner.run_test("Calc: Trend Analysis", "Look at the last 6 months of data and predict the 7th month using a formula.", category="Calc")
        runner.run_test("Calc: Chart Styling", "Change the theme of the existing chart to 'Dark' and add a title 'Revenue 2026'.", category="Calc")
        runner.run_test("Calc: Sensitivity Analysis", "Increase all 'Cost' values by 10% and record the change in 'Total Profit'.", category="Calc")
        runner.run_test("Calc: Sheet Protect", "Lock all cells with formulas so they cannot be edited.", category="Calc")
        runner.run_test("Calc: Audit Log", "Create a log entries sheet tracking every time 'Net Profit' falls below 0.", category="Calc")

    # 🎨 DRAW TESTS (5)
    if "Draw" in categories:
        runner.run_test("Draw: Shape Creation", "Add a blue rectangle in the center of the page.", category="Draw")
        runner.run_test("Draw: Simple Layout", "Create three circles and align them horizontally.", category="Draw")
        runner.run_test("Draw: Flowchart Gen", "Create a 'Start' oval connected to a 'Process' box.", category="Draw")
        runner.run_test("Draw: Z-Order", "Move the blue square behind the red circle.", category="Draw")
        runner.run_test("Draw: Group Scale", "Group all objects on page 1 and double their size.", category="Draw")

    # 🖼️ MULTIMODAL TESTS (5)
    if "Multimodal" in categories:
        runner.run_test("Multimodal: Chart OCR", "Extract data from this chart image and put it into Sheet2.", category="Multimodal")
        runner.run_test("Multimodal: Image Captioning", "Add a caption below this image based on its content.", category="Multimodal")
        runner.run_test("Multimodal: UI Code-Gen", "Translate this UI sketch into an ODF table mockup.", category="Multimodal")
        runner.run_test("Multimodal: Spatial Audit", "Looking at this diagram, is the 'Database' icon correctly connected to the 'Web Server'?", category="Multimodal")
        runner.run_test("Multimodal: Infographic Summary", "Summarize the key takeaways from this infographic image into the document.", category="Multimodal")

    return {
        "passed": runner.passed,
        "failed": runner.failed,
        "total_cost": runner.total_cost,
        "results": runner.get_results(),
        "summary": runner.get_summary()
    }