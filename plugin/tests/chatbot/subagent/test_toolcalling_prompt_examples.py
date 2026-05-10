# WriterAgent - tests for ToolCallingAgent system prompt example blocks
import unittest
from unittest.mock import MagicMock

from plugin.contrib.smolagents.agents import ToolCallingAgent


class TestToolcallingPromptExamples(unittest.TestCase):
    def test_custom_system_prompt_examples_appear_in_rendered_prompt(self):
        model = MagicMock()
        marker = "CUSTOM_EXAMPLES_MARKER_XYZ123"
        agent = ToolCallingAgent(tools=[], model=model, system_prompt_examples=marker)
        prompt = agent.initialize_system_prompt()
        self.assertIn(marker, prompt)
        self.assertNotIn("__EXAMPLES_BLOCK__", prompt)

    def test_default_examples_when_system_prompt_examples_is_none(self):
        from plugin.contrib.smolagents.toolcalling_agent_prompts import DEFAULT_EXAMPLES_BLOCK

        model = MagicMock()
        agent = ToolCallingAgent(tools=[], model=model, system_prompt_examples=None)
        prompt = agent.initialize_system_prompt()
        self.assertIn("Guangzhou", prompt)
        self.assertIn(DEFAULT_EXAMPLES_BLOCK.strip().split("\n")[0], prompt)


if __name__ == "__main__":
    unittest.main()
