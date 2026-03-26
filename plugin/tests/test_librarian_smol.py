import unittest
import time
from unittest.mock import MagicMock, patch
from plugin.modules.chatbot.librarian import SmolToolAdapter, SwitchToDocumentModeTool, LibrarianOnboardingTool
from plugin.modules.chatbot.memory import MemoryTool
from plugin.contrib.smolagents.agents import ToolCallingAgent
from plugin.contrib.smolagents.memory import ActionStep
from plugin.contrib.smolagents.monitoring import Timing

class TestLibrarianSmol(unittest.TestCase):
    def test_tool_adapter_initialization(self):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        
        memory_tool = MemoryTool()
        switch_tool = SwitchToDocumentModeTool()
        
        smol_memory = SmolToolAdapter(memory_tool, ctx)
        smol_switch = SmolToolAdapter(switch_tool, ctx)
        
        # Verify that they are instances of smolagents.tools.BaseTool
        from plugin.contrib.smolagents.tools import BaseTool
        self.assertTrue(isinstance(smol_memory, BaseTool))
        self.assertTrue(isinstance(smol_switch, BaseTool))
        
        # Verify inputs conversion
        self.assertIn("key", smol_memory.inputs)
        self.assertIn("content", smol_memory.inputs)
        self.assertEqual(smol_memory.inputs["key"]["type"], "string")
        self.assertEqual(smol_memory.inputs["content"]["type"], "string")
        
        # Verify forward call
        memory_tool.execute = MagicMock(return_value={"status": "ok"})
        smol_memory.forward(key="favorite_color", content="blue")
        memory_tool.execute.assert_called_once()
        args, kwargs = memory_tool.execute.call_args
        self.assertEqual(kwargs["key"], "favorite_color")
        self.assertEqual(kwargs["content"], "blue")

    def test_agent_initialization_with_adapted_tools(self):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        model = MagicMock()
        
        tools = [
            SmolToolAdapter(MemoryTool(), ctx),
            SmolToolAdapter(SwitchToDocumentModeTool(), ctx)
        ]
        
        # This shouldn't raise "All elements must be instance of BaseTool"
        agent = ToolCallingAgent(
            tools=tools,
            model=model
        )
        self.assertEqual(len(agent.tools), 3) # memory, switch, final_answer
        self.assertIn("upsert_memory", agent.tools)
        self.assertIn("switch_to_document_mode", agent.tools)

    def test_switch_mode_extraction(self):
        ctx = MagicMock()
        ctx.ctx = MagicMock()
        ctx.stop_checker.return_value = False
        
        # Mock ToolCallingAgent to simulate a switch_mode observation
        with patch('plugin.contrib.smolagents.agents.ToolCallingAgent') as mock_agent_class:
            mock_agent = mock_agent_class.return_value
            
            # Simulate steps: one ActionStep with switch_mode
            step1 = ActionStep(step_number=1, timing=Timing(start_time=time.time()))
            step1.observations = "{'status': 'switch_mode', 'message': 'See you in document mode!'}"
            
            mock_agent.run.return_value = [step1]
            
            tool = LibrarianOnboardingTool()
            res = tool.execute(ctx, query="switch me")
            if res["status"] == "error":
                print(f"DEBUG: Tool execution error: {res.get('message')}")
            
            self.assertEqual(res["status"], "switch_mode")
            self.assertEqual(res["result"], "See you in document mode!")

if __name__ == "__main__":
    unittest.main()
