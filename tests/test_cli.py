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
    load_key,
    load_sent_ids,
    load_state,
    observations,
    post_batch,
    parser,
    save_key,
    setup,
)


class ClientTest(unittest.TestCase):
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

    def test_observation_prefix_is_limited_to_50_characters(self):
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
            self.assertEqual(result[0]["message_start"], text[:50])
            self.assertEqual(len(result[0]["message_start"]), 50)

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
            self.assertEqual(result[0]["source"], "claude_code")
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
                setup(args)
            self.assertEqual(len(load_sent_ids(state)), 1)

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


if __name__ == "__main__":
    unittest.main()
