import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.models.claude import MessagesAPIRequest


class MessagesAPIRequestToolParsingTests(unittest.TestCase):
    def test_accepts_custom_tool_payload_without_top_level_input_schema(self) -> None:
        request = MessagesAPIRequest.model_validate(
            {
                "model": "claude-opus-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Search for the latest CNY USD rate"}],
                "tools": [
                    {
                        "type": "custom",
                        "name": "WebSearch",
                        "custom": {
                            "description": "Search the web for public information",
                            "input_schema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"},
                                },
                                "required": ["query"],
                            },
                        },
                    }
                ],
            }
        )

        self.assertEqual(request.tools[0].name, "WebSearch")
        self.assertEqual(
            request.tools[0].input_schema,
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        )

    def test_accepts_mcp_tool_payload_with_camel_case_input_schema(self) -> None:
        request = MessagesAPIRequest.model_validate(
            {
                "model": "claude-opus-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Call the MCP tool"}],
                "tools": [
                    {
                        "type": "mcp",
                        "name": "filesystem__read_file",
                        "server_name": "filesystem",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                            },
                            "required": ["path"],
                        },
                    }
                ],
            }
        )

        self.assertEqual(request.tools[0].name, "filesystem__read_file")
        self.assertEqual(
            request.tools[0].input_schema,
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        )

    def test_accepts_server_web_search_tool_without_input_schema(self) -> None:
        request = MessagesAPIRequest.model_validate(
            {
                "model": "claude-opus-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Search for the latest CNY USD rate"}],
                "tools": [
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": 5,
                    }
                ],
            }
        )

        self.assertEqual(request.tools[0].name, "web_search")

    def test_think_suffix_normalizes_model_and_enables_thinking(self) -> None:
        request = MessagesAPIRequest.model_validate(
            {
                "model": "claude-sonnet-4-6-think",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Think carefully"}],
            }
        )

        self.assertEqual(request.model, "claude-sonnet-4-6")
        self.assertTrue(request.force_web_thinking)
        self.assertIsNotNone(request.thinking)
        self.assertEqual(request.thinking.type, "enabled")
        self.assertNotIn(
            "_clove_force_web_thinking", request.model_dump(exclude_none=True)
        )

    def test_think_suffix_preserves_budget_tokens(self) -> None:
        request = MessagesAPIRequest.model_validate(
            {
                "model": "claude-sonnet-4-6-think",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": "Think carefully"}],
                "thinking": {"type": "disabled", "budget_tokens": 2048},
            }
        )

        self.assertEqual(request.model, "claude-sonnet-4-6")
        self.assertEqual(request.thinking.type, "enabled")
        self.assertEqual(request.thinking.budget_tokens, 2048)
        self.assertEqual(request.max_tokens, 2049)


class ThinkModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_think_suffix_skips_api_processor(self) -> None:
        try:
            from app.processors.claude_ai.claude_api_processor import ClaudeAPIProcessor
            from app.processors.claude_ai.context import ClaudeAIContext
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing optional dependency: {exc}")

        request = MessagesAPIRequest.model_validate(
            {
                "model": "claude-sonnet-4-6-think",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Think carefully"}],
            }
        )
        context = ClaudeAIContext(
            original_request=Mock(headers={}),
            messages_api_request=request,
        )

        processor = ClaudeAPIProcessor()

        with patch(
            "app.processors.claude_ai.claude_api_processor.account_manager.get_account_for_oauth",
            new=AsyncMock(side_effect=AssertionError("Claude API should be skipped")),
        ):
            result = await processor.process(context)

        self.assertIs(result, context)
        self.assertIsNone(result.response)
        self.assertIsNone(request.system)

    async def test_think_suffix_enables_web_extended_mode_for_non_pro_accounts(self) -> None:
        try:
            from app.processors.claude_ai.claude_web_processor import ClaudeWebProcessor
            from app.processors.claude_ai.context import ClaudeAIContext
        except ModuleNotFoundError as exc:
            self.skipTest(f"missing optional dependency: {exc}")

        request = MessagesAPIRequest.model_validate(
            {
                "model": "claude-sonnet-4-6-think",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Think carefully"}],
            }
        )
        context = ClaudeAIContext(
            original_request=Mock(headers={}),
            messages_api_request=request,
        )

        mock_session = AsyncMock()
        mock_session.session_id = "session_test"
        mock_session.account = Mock(is_pro=False)
        mock_session.send_message = AsyncMock(return_value=Mock())

        processor = ClaudeWebProcessor()

        with patch(
            "app.processors.claude_ai.claude_web_processor.session_manager.get_or_create_session",
            new=AsyncMock(return_value=mock_session),
        ), patch(
            "app.processors.claude_ai.claude_web_processor.process_messages",
            new=AsyncMock(return_value=("Think carefully", [])),
        ):
            result = await processor.process(context)

        self.assertIs(result, context)
        mock_session.set_paprika_mode.assert_awaited_once_with("extended")
        self.assertEqual(context.claude_web_request.model, "claude-sonnet-4-6")


if __name__ == "__main__":
    unittest.main()
