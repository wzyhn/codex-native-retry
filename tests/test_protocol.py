import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_native_retry import cli_native_request, native_request, parse_record


class ProtocolTests(unittest.TestCase):
    def test_empty_input_request_has_no_overrides(self):
        payload = native_request("thread-1")
        self.assertEqual(
            payload,
            {
                "threadId": "thread-1",
                "turnTrigger": "capacity_retry_automatic",
                "input": [],
            },
        )
        self.assertNotIn("model", payload)
        self.assertNotIn("effort", payload)
        self.assertNotIn("cwd", payload)

    def test_cli_empty_input_request_has_no_trigger_or_overrides(self):
        self.assertEqual(cli_native_request("thread-1"), {"threadId": "thread-1", "input": []})

    def test_manual_capacity_trigger_is_explicit(self):
        payload = native_request("thread-1", turn_trigger="capacity_retry_manual")
        self.assertEqual(payload["turnTrigger"], "capacity_retry_manual")

    def test_error_notification_reads_will_retry(self):
        line = '{"method":"error","params":{"threadId":"thread-1","turnId":"turn-1","willRetry":false,"error":{"codexErrorInfo":"serverOverloaded"}}}'
        parsed = parse_record(line)
        self.assertEqual(parsed.error_kind, "server_overloaded")
        self.assertFalse(parsed.will_retry)

    def test_non_capacity_structured_error_is_ignored_by_classifier(self):
        line = '{"method":"error","params":{"threadId":"thread-1","turnId":"turn-1","willRetry":false,"error":{"codexErrorInfo":"usageLimit"}}}'
        parsed = parse_record(line)
        self.assertEqual(parsed.error_kind, "unknown")

    def test_retry_after_is_metadata_only(self):
        line = '{"method":"error","params":{"threadId":"thread-1","turnId":"turn-1","willRetry":false,"retryAfterSeconds":17,"error":{"codexErrorInfo":"serverOverloaded"}}}'
        parsed = parse_record(line)
        self.assertEqual(parsed.retry_after_seconds, 17.0)

    def test_terminal_cli_rollout_capacity_is_explicitly_terminal(self):
        line = (
            '{"type":"event_msg","payload":{"type":"task_complete",'
            '"turn_id":"turn-1","error":{"codex_error_info":"server_overloaded"}}}'
        )
        parsed = parse_record(line)
        self.assertEqual(parsed.error_kind, "server_overloaded")
        self.assertFalse(parsed.will_retry)


if __name__ == "__main__":
    unittest.main()
