import unittest
from unittest.mock import patch

from anthropic_proxy import (
    CompatHandler,
    assert_listen_host_allowed,
    compact_anthropic_tool_results,
    credential_matches,
    extract_request_credential,
    is_loopback_host,
    normalize_anthropic_request,
)


class NormalizeAnthropicRequestTests(unittest.TestCase):
    def test_merges_late_system_message_into_adjacent_user(self):
        payload = {
            "model": "qwen3.8:27b",
            "system": [{"type": "text", "text": "Base policy"}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
                {"role": "system", "content": [{"type": "text", "text": "Repository context"}]},
            ],
        }

        normalized, count = normalize_anthropic_request(payload)

        self.assertEqual(count, 1)
        self.assertEqual([message["role"] for message in normalized["messages"]], ["user"])
        self.assertEqual(normalized["system"], payload["system"])
        self.assertEqual(
            "".join(block["text"] for block in normalized["messages"][0]["content"]),
            "Hello\n\n<system-reminder>\nRepository context\n</system-reminder>",
        )

    def test_keeps_completed_turns_stable_when_a_new_reminder_arrives(self):
        first = {
            "system": "Base policy",
            "messages": [
                {"role": "user", "content": "First"},
                {"role": "system", "content": "First reminder"},
            ],
        }
        second = {
            "system": "Base policy",
            "messages": [
                *first["messages"],
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second"},
                {"role": "system", "content": "Second reminder"},
            ],
        }

        normalized_first, _ = normalize_anthropic_request(first)
        normalized_second, _ = normalize_anthropic_request(second)

        self.assertEqual(normalized_first["system"], normalized_second["system"])
        self.assertEqual(normalized_first["messages"][0], normalized_second["messages"][0])

    def test_preserves_request_without_late_system(self):
        payload = {"messages": [{"role": "user", "content": "Hello"}]}

        normalized, count = normalize_anthropic_request(payload)

        self.assertIs(normalized, payload)
        self.assertEqual(count, 0)

    def test_rejects_non_text_late_system_block(self):
        payload = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "system", "content": [{"type": "tool_use", "name": "Read"}]},
            ]
        }

        with self.assertRaisesRegex(ValueError, "only text"):
            normalize_anthropic_request(payload)


class CompactAnthropicToolResultsTests(unittest.TestCase):
    def test_compaction_is_stable_when_new_results_arrive(self):
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "old", "content": "A" * 16000}
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": "summary stays"}]},
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "new", "content": "B" * 16000}
                    ],
                },
            ]
        }

        old_only, _, _ = compact_anthropic_tool_results({"messages": payload["messages"][:1]})
        compacted, count, removed = compact_anthropic_tool_results(payload)

        old_result = compacted["messages"][0]["content"][0]
        new_result = compacted["messages"][2]["content"][0]
        self.assertEqual(count, 2)
        self.assertGreater(removed, 0)
        self.assertEqual(old_result["tool_use_id"], "old")
        self.assertEqual(new_result["tool_use_id"], "new")
        self.assertEqual(old_result, old_only["messages"][0]["content"][0])
        self.assertEqual(len(old_result["content"]), len(new_result["content"]))
        self.assertIn("Local Ollama context guard", old_result["content"])
        self.assertEqual(compacted["messages"][1]["content"][0]["text"], "summary stays")

    def test_compacts_text_blocks_but_preserves_non_text_blocks(self):
        image = {"type": "image", "source": {"type": "base64", "data": "abc"}}
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "mixed",
                            "content": [image, {"type": "text", "text": "X" * 20000}],
                        }
                    ],
                }
            ]
        }

        compacted, count, removed = compact_anthropic_tool_results(payload)

        result = compacted["messages"][0]["content"][0]
        self.assertEqual(count, 1)
        self.assertGreater(removed, 0)
        self.assertEqual(result["content"][0], image)
        self.assertIn("Local Ollama context guard", result["content"][1]["text"])


class CompatAuthTests(unittest.TestCase):
    def test_extracts_x_api_key(self):
        self.assertEqual(
            extract_request_credential({"X-Api-Key": "ollama", "Content-Type": "application/json"}),
            "ollama",
        )

    def test_extracts_authorization_bearer(self):
        self.assertEqual(
            extract_request_credential({"Authorization": "Bearer secret-token"}),
            "secret-token",
        )

    def test_prefers_x_api_key_over_bearer(self):
        self.assertEqual(
            extract_request_credential(
                {"x-api-key": "from-header", "Authorization": "Bearer from-bearer"}
            ),
            "from-header",
        )

    def test_missing_credential_is_none(self):
        self.assertIsNone(extract_request_credential({"Content-Type": "application/json"}))
        self.assertIsNone(extract_request_credential({"Authorization": "Basic abc"}))

    def test_credential_matches_expected_token(self):
        self.assertTrue(credential_matches("ollama", "ollama"))
        self.assertFalse(credential_matches("wrong", "ollama"))
        self.assertFalse(credential_matches(None, "ollama"))
        self.assertFalse(credential_matches("", "ollama"))

    def test_empty_expected_token_disables_auth(self):
        self.assertTrue(credential_matches(None, ""))
        self.assertTrue(credential_matches("anything", ""))

    def test_loopback_hosts(self):
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertTrue(is_loopback_host("[::1]"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("192.168.1.10"))

    def test_non_loopback_bind_requires_token(self):
        assert_listen_host_allowed("127.0.0.1", "")
        assert_listen_host_allowed("0.0.0.0", "ollama")
        with self.assertRaises(SystemExit) as raised:
            assert_listen_host_allowed("0.0.0.0", "")
        self.assertIn("QWEN38_COMPAT_TOKEN", str(raised.exception))

    def test_proxy_returns_401_without_calling_upstream(self):
        handler = CompatHandler.__new__(CompatHandler)
        handler.command = "GET"
        handler.path = "/v1/models"
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 1)
        handler.close_connection = False
        sent: dict[str, object] = {}

        def capture(status: int, error_type: str, message: str) -> None:
            sent["status"] = status
            sent["error_type"] = error_type
            sent["message"] = message

        handler._send_anthropic_error = capture  # type: ignore[method-assign]
        with (
            patch("anthropic_proxy.COMPAT_TOKEN", "ollama"),
            patch("anthropic_proxy.http.client.HTTPConnection") as upstream,
        ):
            handler._proxy()

        self.assertEqual(sent["status"], 401)
        self.assertEqual(sent["error_type"], "authentication_error")
        upstream.assert_not_called()

    def test_proxy_returns_401_on_wrong_token(self):
        handler = CompatHandler.__new__(CompatHandler)
        handler.command = "GET"
        handler.path = "/"
        handler.headers = {"x-api-key": "wrong"}
        handler.client_address = ("127.0.0.1", 1)
        handler.close_connection = False
        sent: dict[str, object] = {}
        handler._send_anthropic_error = (  # type: ignore[method-assign]
            lambda status, error_type, message: sent.update(status=status)
        )
        with (
            patch("anthropic_proxy.COMPAT_TOKEN", "ollama"),
            patch("anthropic_proxy.http.client.HTTPConnection") as upstream,
        ):
            handler._proxy()
        self.assertEqual(sent["status"], 401)
        upstream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
