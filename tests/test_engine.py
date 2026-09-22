import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_native_retry import ActivityEvent, ErrorEvent, RecoveryEngine, RolloutWatcher, parse_record


def capacity(thread="thread-1", turn="turn-1", will_retry=False):
    return ErrorEvent("now", thread, turn, "server_overloaded", will_retry, "test")


class RecoveryEngineTests(unittest.TestCase):
    def test_default_first_retry_is_immediate(self):
        engine = RecoveryEngine(jitter_ratio=0)
        engine.handle_error(capacity(), now=0)
        self.assertEqual(len(engine.poll_due(now=0)), 1)

    def test_requested_consecutive_capacity_schedule(self):
        engine = RecoveryEngine(jitter_ratio=0, max_attempts=0)
        engine.handle_error(capacity(), now=0)
        episode = engine.poll_due(now=0)[0]
        expected = [3, 5, 10, 15, 30, 60, 60]
        for delay in expected:
            self.assertEqual(engine.native_result(episode, False, now=0).action, "pending")
            self.assertEqual(len(engine.poll_due(now=delay - 0.01)), 0)
            self.assertEqual(len(engine.poll_due(now=delay)), 1)

    def test_success_then_new_turn_resets_to_immediate(self):
        engine = RecoveryEngine(jitter_ratio=0)
        engine.handle_error(capacity(turn="turn-1"), now=0)
        episode = engine.poll_due(now=0)[0]
        engine.native_result(episode, True, next_turn_id="turn-2")
        engine.handle_activity(ActivityEvent("now", "thread-1", "turn-3", "turn_started"))
        engine.handle_error(capacity(turn="turn-3"), now=10)
        self.assertEqual(len(engine.poll_due(now=10)), 1)

    def test_jitter_stays_within_configured_bounds(self):
        low = RecoveryEngine(retry_delays=[3], jitter_ratio=0.2, random_fn=lambda: 0.0)
        high = RecoveryEngine(retry_delays=[3], jitter_ratio=0.2, random_fn=lambda: 1.0)
        low.handle_error(capacity(), now=0)
        high.handle_error(capacity(thread="thread-2"), now=0)
        self.assertEqual(len(low.poll_due(now=2.39)), 0)
        self.assertEqual(len(low.poll_due(now=2.4)), 1)
        self.assertEqual(len(high.poll_due(now=3.59)), 0)
        self.assertEqual(len(high.poll_due(now=3.6)), 1)

    def test_a_will_retry_true_does_not_schedule(self):
        engine = RecoveryEngine(initial_delay=5, jitter_ratio=0)
        self.assertEqual(engine.handle_error(capacity(will_retry=True), now=0).action, "ignore")
        self.assertEqual(engine.poll_due(now=100), [])

    def test_late_will_retry_true_cancels_existing_episode(self):
        engine = RecoveryEngine(initial_delay=5, jitter_ratio=0)
        engine.handle_error(capacity(will_retry=False), now=0)
        self.assertEqual(engine.handle_error(capacity(will_retry=True), now=0).reason, "codex_will_retry")
        self.assertEqual(engine.poll_due(now=100), [])

    def test_b_terminal_capacity_creates_pending_episode(self):
        engine = RecoveryEngine(initial_delay=5, jitter_ratio=0)
        self.assertEqual(engine.handle_error(capacity(), now=0).action, "pending")
        self.assertEqual(len(engine.poll_due(now=4.9)), 0)
        self.assertEqual(len(engine.poll_due(now=5)), 1)

    def test_c_duplicate_error_is_deduped(self):
        engine = RecoveryEngine(initial_delay=5, jitter_ratio=0)
        self.assertEqual(engine.handle_error(capacity(), now=0).action, "pending")
        self.assertEqual(engine.handle_error(capacity(), now=0).action, "duplicate")

    def test_d_new_turn_cancels_pending(self):
        engine = RecoveryEngine(initial_delay=5, jitter_ratio=0)
        engine.handle_error(capacity(), now=0)
        decision = engine.handle_activity(ActivityEvent("now", "thread-1", "turn-2", "turn_started"))
        self.assertEqual(decision.action, "cancel")
        self.assertEqual(engine.poll_due(now=100), [])

    def test_e_manual_cancel_invalidates_retry(self):
        engine = RecoveryEngine(initial_delay=5, jitter_ratio=0)
        engine.handle_error(capacity(), now=0)
        engine.cancel_thread("thread-1", "manual_retry")
        self.assertEqual(engine.poll_due(now=100), [])

    def test_f_failed_native_result_uses_exponential_backoff(self):
        engine = RecoveryEngine(initial_delay=5, max_delay=60, jitter_ratio=0)
        engine.handle_error(capacity(), now=0)
        episode = engine.poll_due(now=5)[0]
        self.assertEqual(engine.native_result(episode, False, now=5).action, "pending")
        self.assertEqual(len(engine.poll_due(now=14.9)), 0)
        self.assertEqual(len(engine.poll_due(now=15)), 1)

    def test_g_to_j_non_capacity_errors_fail_closed(self):
        engine = RecoveryEngine(initial_delay=0, jitter_ratio=0)
        for kind in ("usage_limit", "context_window", "policy", "unknown"):
            event = ErrorEvent("now", "thread", kind, kind, False, "test")
            self.assertEqual(engine.handle_error(event).action, "ignore")

    def test_missing_will_retry_fails_closed(self):
        engine = RecoveryEngine(initial_delay=0, jitter_ratio=0)
        self.assertEqual(engine.handle_error(capacity(will_retry=None)).reason, "missing_will_retry")

    def test_transport_uncertainty_stops_without_replay(self):
        engine = RecoveryEngine(initial_delay=0, jitter_ratio=0)
        engine.handle_error(capacity(), now=0)
        episode = engine.poll_due(now=0)[0]
        self.assertEqual(engine.native_uncertain(episode).action, "stop")
        self.assertEqual(engine.poll_due(now=100), [])

    def test_dry_run_reschedules_without_sending(self):
        engine = RecoveryEngine(initial_delay=5, jitter_ratio=0, max_attempts=2)
        engine.handle_error(capacity(), now=0)
        episode = engine.poll_due(now=5)[0]
        engine.mark_dry_run(episode, now=5)
        self.assertEqual(episode.state, "would_retry")
        self.assertEqual(len(engine.poll_due(now=14.9)), 0)
        self.assertEqual(len(engine.poll_due(now=15)), 1)

    def test_rollout_shape_extracts_structured_error_without_body(self):
        line = '{"timestamp":"2026-09-20T00:00:00Z","type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-1","error":{"message":"Selected model is at capacity. Please try a different model.","codex_error_info":"server_overloaded"}}}'
        parsed = parse_record(line, Path("rollout-2026-09-20T00-00-00-thread-1.jsonl"))
        self.assertEqual(parsed.error_kind, "server_overloaded")
        self.assertFalse(parsed.will_retry)

    def test_startup_recovers_existing_latest_capacity(self):
        line = (
            '{"timestamp":"2026-09-22T00:00:00Z","type":"event_msg",'
            '"payload":{"type":"task_complete","thread_id":"thread-1",'
            '"turn_id":"turn-1","error":{"codex_error_info":"server_overloaded"}}}'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-2026-09-22-00000000-0000-4000-8000-000000000123.jsonl"
            path.write_text(line + "\n", encoding="utf-8")
            engine = RecoveryEngine(initial_delay=0, jitter_ratio=0)
            watcher = RolloutWatcher(Path(directory), engine, lambda _entry: None)
            watcher.prime(recover_existing_capacity=True)
            due = engine.poll_due(now=time.monotonic())
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0].event.turn_id, "turn-1")

    def test_startup_does_not_recover_capacity_followed_by_new_turn(self):
        lines = [
            '{"timestamp":"2026-09-22T00:00:00Z","type":"event_msg",'
            '"payload":{"type":"task_complete","thread_id":"thread-1",'
            '"turn_id":"turn-1","error":{"codex_error_info":"server_overloaded"}}}',
            '{"timestamp":"2026-09-22T00:00:01Z","type":"event_msg",'
            '"payload":{"type":"turn_started","thread_id":"thread-1",'
            '"turn_id":"turn-2"}}',
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-2026-09-22-00000000-0000-4000-8000-000000000123.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            engine = RecoveryEngine(initial_delay=0, jitter_ratio=0)
            watcher = RolloutWatcher(Path(directory), engine, lambda _entry: None)
            watcher.prime(recover_existing_capacity=True)
            self.assertEqual(engine.poll_due(now=0), [])


if __name__ == "__main__":
    unittest.main()
