from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import os
import stat
import unittest
from unittest.mock import patch
import requests

from kaomojo_client.cli import (
    claude_observations,
    baseline_new_sources,
    configure_launchd_schedule,
    configure_systemd_schedule,
    client_lock,
    import_history,
    load_key,
    load_sent_ids,
    load_state,
    observations,
    observation_batches,
    post_batch,
    print_rejections,
    record_rejections,
    SubmissionError,
    parser,
    save_key,
    setup,
)


class ClientTest(unittest.TestCase):
    def test_observation_batches_honor_item_and_body_limits(self):
        observations = [
            {
                "idempotency_key": f"item-{index}",
                "message_start": "(^_^) finished the work",
                "harness": "codex",
                "context": "x" * 200,
            }
            for index in range(501)
        ]
        batches = list(observation_batches(observations))
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(map(len, batches)), 501)
        self.assertTrue(all(len(batch) <= 100 for batch in batches))
        self.assertTrue(all(
            len(json.dumps({"observations": batch}).encode("utf-8")) <= 24 * 1024
            for batch in batches
        ))

    def test_help_names_both_supported_agents(self):
        help_text = parser().format_help()
        self.assertIn("Codex and Claude Code sessions", " ".join(help_text.split()))

    def test_key_round_trip_uses_private_permissions(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "credentials.json"
            save_key(path, "ar_abcdefghijklmnopqrstuvwxyz")
            with patch.dict(os.environ, {"KAOMOJO_API_KEY": "ar_environment_is_not_supported"}):
                self.assertEqual(load_key(path), "ar_abcdefghijklmnopqrstuvwxyz")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_observation_contains_prefix_model_and_hash(self):
        with TemporaryDirectory() as directory:
            sessions = Path(directory)
            records = [
                {"type": "turn_context", "payload": {"model": "gpt-test"}},
                {
                    "type": "response_item",
                    "timestamp": "2026-08-01T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "(＾▽＾) Finished."}],
                    },
                },
            ]
            (sessions / "session.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
            result = list(observations(sessions, set()))
            self.assertEqual(result[0]["message_start"], "(＾▽＾) Finished.")
            self.assertIn("idempotency_key", result[0])
            self.assertNotIn("id", result[0])
            self.assertEqual(result[0]["model"], "gpt-test")
            self.assertTrue(result[0]["conversation_hash"].startswith("sha256:"))

    def test_observation_prefix_is_limited_to_30_characters(self):
        with TemporaryDirectory() as directory:
            sessions = Path(directory)
            text = "(＾▽＾) " + "x" * 100
            record = {
                "type": "response_item",
                "timestamp": "2026-08-01T00:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                },
            }
            (sessions / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            result = list(observations(sessions, set()))
            self.assertEqual(result[0]["message_start"], text[:30])
            self.assertEqual(len(result[0]["message_start"]), 30)

    def test_observation_prefix_replaces_control_characters(self):
        with TemporaryDirectory() as directory:
            sessions = Path(directory)
            record = {
                "type": "response_item",
                "timestamp": "2026-08-01T00:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "(•‿•) First line\nsecond line"}],
                },
            }
            (sessions / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            result = list(observations(sessions, set()))
        self.assertEqual(result[0]["message_start"], "(•‿•) First line second line")

    def test_claude_observation_contains_prefix_source_model_and_hash(self):
        with TemporaryDirectory() as directory:
            projects = Path(directory)
            records = [
                {
                    "type": "assistant",
                    "uuid": "message-uuid",
                    "timestamp": "2026-08-01T00:00:00Z",
                    "message": {
                        "role": "assistant",
                        "model": "claude-opus-test",
                        "content": [
                            {"type": "thinking", "thinking": "private"},
                            {"type": "text", "text": "(╥﹏╥) Fixed it."},
                            {"type": "tool_use", "name": "ignored"},
                        ],
                    },
                },
            ]
            (projects / "session.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
            result = list(claude_observations(projects, set()))
            self.assertEqual(result[0]["message_start"], "(╥﹏╥) Fixed it.")
            self.assertEqual(result[0]["harness"], "claude_code")
            self.assertEqual(result[0]["model"], "claude-opus-test")
            self.assertTrue(result[0]["conversation_hash"].startswith("sha256:"))

    def test_invalid_key_is_rejected(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "does not look"):
                save_key(Path(directory) / "credentials.json", "not-a-key")

    def test_setup_automatically_baselines_existing_observations(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            sessions.mkdir()
            record = {
                "type": "response_item",
                "timestamp": "2026-08-01T00:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "(＾▽＾) Existing."}],
                },
            }
            (sessions / "session.jsonl").write_text(json.dumps(record), encoding="utf-8")
            state = root / "state.json"
            args = SimpleNamespace(
                codex_sessions=sessions,
                claude_projects=root / "missing-claude-projects",
                state=state,
                credentials=root / "credentials.json",
                key_stdin=True,
            )
            with patch("kaomojo_client.cli.sys.stdin.readline", return_value="ar_abcdefghijklmnopqrstuvwxyz\n"):
                with patch("kaomojo_client.cli.configure_schedule") as configure_schedule:
                    setup(args)
            self.assertEqual(len(load_sent_ids(state)), 1)
            configure_schedule.assert_called_once_with(args)

    def test_systemd_schedule_runs_every_five_minutes_with_deadline(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("kaomojo_client.cli.Path.home", return_value=root):
                with patch("kaomojo_client.cli.run_scheduler_command") as run:
                    configure_systemd_schedule(Path("/opt/kaomojo/bin/kaomojo"))
            service = (root / ".config/systemd/user/kaomojo-collect.service").read_text()
            timer = (root / ".config/systemd/user/kaomojo-collect.timer").read_text()
            self.assertIn('ExecStart="/opt/kaomojo/bin/kaomojo" collect', service)
            self.assertIn("TimeoutStartSec=150", service)
            self.assertIn("OnCalendar=*:0/5", timer)
            self.assertIn("AccuracySec=15s", timer)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(
                run.call_args_list[-1].args[0],
                ["systemctl", "--user", "enable", "--now", "kaomojo-collect.timer"],
            )

    def test_launchd_schedule_runs_every_five_minutes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("kaomojo_client.cli.Path.home", return_value=root):
                with patch("kaomojo_client.cli.DEFAULT_STATE", root / "state"):
                    with patch("kaomojo_client.cli.run_scheduler_command") as run:
                        configure_launchd_schedule(Path("/opt/kaomojo/bin/kaomojo"))
            plist_path = root / "Library/LaunchAgents/com.kaomojo.collect.plist"
            with plist_path.open("rb") as source:
                import plistlib
                payload = plistlib.load(source)
            self.assertEqual(payload["StartInterval"], 300)
            self.assertEqual(payload["ProgramArguments"], ["/opt/kaomojo/bin/kaomojo", "collect"])
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0][0:2], ["launchctl", "bootstrap"])

    def test_upgrade_baselines_claude_without_replaying_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            codex = root / "codex"
            claude = root / "claude"
            codex.mkdir()
            claude.mkdir()
            (claude / "session.jsonl").write_text(json.dumps({
                "type": "assistant",
                "uuid": "existing-claude-message",
                "timestamp": "2026-08-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "(￣▽￣) Existing."}],
                },
            }), encoding="utf-8")
            state = root / "state.json"
            state.write_text(json.dumps(["existing-codex-id"]), encoding="utf-8")
            args = SimpleNamespace(
                codex_sessions=codex,
                claude_projects=claude,
                state=state,
            )
            sent_ids, initialized = load_state(state)
            added = baseline_new_sources(args, sent_ids, initialized)
            self.assertEqual(added, {"claude_code": 1})
            sent_ids, initialized = load_state(state)
            self.assertEqual(len(sent_ids), 2)
            self.assertEqual(initialized, {"codex", "claude_code"})

    def test_submission_timeout_terminates(self):
        session = SimpleNamespace(post=lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.Timeout("upstream timed out")
        ))
        with patch("kaomojo_client.cli.time.sleep", return_value=None):
            with self.assertRaisesRegex(requests.Timeout, "upstream timed out"):
                post_batch(session, "ar_abcdefghijklmnopqrstuvwxyz", [{"message_start": "(._.)"}])

    def test_malformed_submission_success_is_rejected(self):
        response = SimpleNamespace(
            status_code=202,
            ok=True,
            json=lambda: {"accepted": 1, "rejected": 0},
            raise_for_status=lambda: None,
        )
        session = SimpleNamespace(post=lambda *args, **kwargs: response)
        with self.assertRaisesRegex(RuntimeError, "malformed success"):
            post_batch(session, "ar_abcdefghijklmnopqrstuvwxyz", [{"message_start": "(._.)"}])

    def test_submission_validation_error_is_concrete(self):
        response = SimpleNamespace(
            status_code=400,
            ok=False,
            json=lambda: {"error": {"message": "message_start contains control characters"}},
            text="",
        )
        session = SimpleNamespace(post=lambda *args, **kwargs: response)
        with self.assertRaisesRegex(RuntimeError, "control characters"):
            post_batch(session, "ar_abcdefghijklmnopqrstuvwxyz", [{"message_start": "bad"}])

    def test_rejected_observations_are_summarized_by_reason(self):
        from collections import Counter
        reasons = Counter()
        record_rejections(reasons, {"results": [
            {"accepted": False, "reason": "No authentic kaomoji"},
            {"accepted": True, "reason": "Authentic kaomoji"},
            {"accepted": False, "reason": "No authentic kaomoji"},
        ]})
        with patch("builtins.print") as output:
            print_rejections(reasons)
        output.assert_any_call("  2 × No authentic kaomoji")

    def test_server_failure_remains_terminal_after_retries(self):
        response = SimpleNamespace(
            status_code=500,
            ok=False,
            json=lambda: {"error": {"message": "database unavailable"}},
            text="",
            headers={},
        )
        session = SimpleNamespace(post=lambda *args, **kwargs: response)
        with patch("kaomojo_client.cli.time.sleep", return_value=None):
            with self.assertRaisesRegex(SubmissionError, "database unavailable"):
                post_batch(session, "ar_abcdefghijklmnopqrstuvwxyz", [{"message_start": "(._.)"}])

    def test_history_import_checkpoints_and_reruns_without_duplicates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            sessions.mkdir()
            records = [{
                "type": "response_item",
                "timestamp": f"2026-08-01T00:00:0{index}Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"(^_{index}^) Existing."}],
                },
            } for index in range(2)]
            (sessions / "session.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8",
            )
            credentials = root / "credentials.json"
            save_key(credentials, "ar_abcdefghijklmnopqrstuvwxyz")
            state = root / "state.json"
            state.write_text(json.dumps({
                "version": 2, "sent_ids": [], "initialized_sources": ["codex"],
            }), encoding="utf-8")
            args = SimpleNamespace(
                codex_sessions=sessions,
                claude_projects=root / "missing-claude",
                state=state,
                credentials=credentials,
                import_state=root / "history-import.json",
                lock=root / "client.lock",
                deadline=120,
            )

            def accepted_batch(session, key, batch, deadline_seconds=120):
                return {
                    "accepted": len(batch),
                    "rejected": 0,
                    "results": [{
                        "idempotency_key": item["idempotency_key"],
                        "accepted": True,
                    } for item in batch],
                }

            observed_order = []

            def ordered_accepted_batch(session, key, batch, deadline_seconds=120):
                observed_order.extend(item["observed_at"] for item in batch)
                return accepted_batch(session, key, batch, deadline_seconds)

            with patch("kaomojo_client.cli.post_batch", side_effect=ordered_accepted_batch) as post:
                import_history(args)
                import_history(args)
            saved = json.loads(args.import_state.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(len(saved["processed_ids"]), 2)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(observed_order, [
                "2026-08-01T00:00:01Z", "2026-08-01T00:00:00Z",
            ])
            self.assertEqual(stat.S_IMODE(args.import_state.stat().st_mode), 0o600)

    def test_history_import_deadline_is_terminal_and_resumable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            sessions.mkdir()
            (sessions / "session.jsonl").write_text(json.dumps({
                "type": "response_item",
                "timestamp": "2026-08-01T00:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "(._.) Existing."}],
                },
            }), encoding="utf-8")
            credentials = root / "credentials.json"
            save_key(credentials, "ar_abcdefghijklmnopqrstuvwxyz")
            state = root / "state.json"
            state.write_text(json.dumps({
                "version": 2, "sent_ids": [], "initialized_sources": ["codex"],
            }), encoding="utf-8")
            args = SimpleNamespace(
                codex_sessions=sessions,
                claude_projects=root / "missing-claude",
                state=state,
                credentials=credentials,
                import_state=root / "history-import.json",
                lock=root / "client.lock",
                deadline=120,
            )
            with patch("kaomojo_client.cli.time.monotonic", side_effect=[0, 121, 121]):
                with self.assertRaisesRegex(RuntimeError, "rerun.*resume"):
                    import_history(args)
            saved = json.loads(args.import_state.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "timed_out")
            self.assertEqual(saved["processed_ids"], [])

    def test_client_lock_rejects_duplicate_operation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "client.lock"
            with client_lock(path):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with client_lock(path):
                        self.fail("duplicate lock unexpectedly acquired")


if __name__ == "__main__":
    unittest.main()
