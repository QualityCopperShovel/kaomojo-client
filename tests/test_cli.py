from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import os
import stat
import sqlite3
import sys
import unittest
from unittest.mock import patch
import subprocess
import requests

from kaomojo_client.cli import (
    claude_observations,
    hermes_observations,
    baseline_new_sources,
    configure_launchd_schedule,
    configure_systemd_schedule,
    configure_windows_schedule,
    client_lock,
    client_environment,
    import_history,
    load_key,
    load_sent_ids,
    load_state,
    may_contain_kaomoji,
    maybe_auto_update,
    observations,
    observation_batches,
    post_batch,
    print_rejections,
    record_rejections,
    record_warnings,
    print_warnings,
    SubmissionError,
    parser,
    save_key,
    setup,
)


class ClientTest(unittest.TestCase):
    def test_staging_api_url_can_be_selected_before_process_start(self):
        result = subprocess.run(
            [sys.executable, "-c", "from kaomojo_client.cli import API_URL; print(API_URL)"],
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
                "KAOMOJO_API_URL": "https://staging.example/api/v1/kaomojis",
            },
            capture_output=True, text=True, timeout=10, check=True,
        )
        self.assertEqual(result.stdout.strip(), "https://staging.example/api/v1/kaomojis")

    def test_auto_update_installs_only_manifest_pinned_commit_and_verifies_version(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "update.json"
            response = SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "version": "4.15.0",
                    "repository": "https://github.com/QualityCopperShovel/kaomojo-client.git",
                    "commit": "a" * 40,
                },
            )
            completed = [SimpleNamespace(stdout=""), SimpleNamespace(stdout="kaomojo 4.15.0\n")]
            with patch("kaomojo_client.cli.requests.get", return_value=response) as request, patch(
                "kaomojo_client.cli.shutil.which", side_effect=["/usr/bin/pipx", "/bin/kaomojo"]
            ), patch("kaomojo_client.cli.subprocess.run", side_effect=completed) as run:
                self.assertTrue(maybe_auto_update(state))
            request.assert_called_once_with(
                "https://kaomojo.com/api/v1/client-release", timeout=10,
            )
            self.assertIn("@" + ("a" * 40), run.call_args_list[0].args[0][-1])
            self.assertEqual(json.loads(state.read_text())["status"], "updated")

    def test_auto_update_rejects_untrusted_repository_without_blocking(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "update.json"
            response = SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "version": "4.10.0", "repository": "https://evil.invalid/client.git",
                    "commit": "a" * 40,
                },
            )
            with patch("kaomojo_client.cli.requests.get", return_value=response), patch(
                "kaomojo_client.cli.subprocess.run"
            ) as run:
                self.assertFalse(maybe_auto_update(state))
            run.assert_not_called()
            self.assertEqual(json.loads(state.read_text())["status"], "failed")

    def test_client_environment_is_coarse_and_excludes_device_identity(self):
        with patch("kaomojo_client.cli.platform.system", return_value="Darwin"), patch(
            "kaomojo_client.cli.platform.mac_ver", return_value=("15.2.0", ("", "", ""), "")
        ), patch("kaomojo_client.cli.platform.machine", return_value="arm64"):
            environment = client_environment([
                {"harness": "codex"}, {"harness": "claude_code"}, {"harness": "codex"},
            ])
        self.assertEqual(environment["os_family"], "macOS")
        self.assertEqual(environment["os_major"], "15")
        self.assertEqual(environment["harnesses"], ["claude_code", "codex"])
        self.assertEqual(set(environment), {
            "client_name", "client_version", "os_family", "os_major",
            "architecture", "python_version", "harnesses",
        })

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
        self.assertTrue(all(len(batch) <= 20 for batch in batches))
        self.assertTrue(all(
            len(json.dumps({"observations": batch}).encode("utf-8")) <= 24 * 1024
            for batch in batches
        ))

    def test_plain_text_prefilter_is_conservative(self):
        self.assertFalse(may_contain_kaomoji({"message_start": "Finished updating the tests."}))
        for value in ("(＾▽＾) Done", "hello :)", "Done — (._.)", "XD"):
            self.assertTrue(may_contain_kaomoji({"message_start": value}), value)

    def test_prefilter_keeps_messages_whose_face_is_only_at_the_end(self):
        trailing = {
            "message_start": "Finished updating the tests",
            "message_end": "and they all pass now (＾▽＾)",
        }
        self.assertTrue(may_contain_kaomoji(trailing))
        self.assertFalse(may_contain_kaomoji({
            "message_start": "Finished updating the tests",
            "message_end": "and they all pass now.",
        }))

    def test_capacity_failure_is_split_without_reposting_successes(self):
        success = lambda items: SimpleNamespace(
            status_code=202, ok=True, headers={}, text="",
            json=lambda: {
                "accepted": len(items), "rejected": 0,
                "results": [{"idempotency_key": item["idempotency_key"], "accepted": True}
                            for item in items],
            },
        )
        calls = []
        def post(*args, **kwargs):
            items = kwargs["json"]["observations"]
            calls.append(len(items))
            if len(items) > 1:
                return SimpleNamespace(
                    status_code=503, ok=False, headers={"Retry-After": "1"}, text="",
                    json=lambda: {"error": {"code": "extraction_capacity", "message": "split"}},
                )
            return success(items)
        from kaomojo_client.cli import post_resilient_batch
        batch = [{"idempotency_key": str(index), "message_start": "(._.)"}
                 for index in range(2)]
        result = post_resilient_batch(SimpleNamespace(post=post), "ar_abcdefghijklmnopqrstuvwxyz", batch)
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(calls, [2, 1, 1])

    def test_help_names_both_supported_agents(self):
        help_text = parser().format_help()
        self.assertIn("Codex, Claude Code, and Hermes sessions", " ".join(help_text.split()))

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
            self.assertNotIn(
                "message_end", result[0],
                "a message inside one excerpt must never be transmitted twice",
            )
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
            self.assertEqual(result[0]["message_end"], text[30:][-30:])
            self.assertEqual(len(result[0]["message_end"]), 30)

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

    def test_trailing_excerpt_never_overlaps_the_opening_one(self):
        from kaomojo_client.cli import message_excerpts

        self.assertEqual(message_excerpts("(•‿•) short"), ("(•‿•) short", None))
        start, end = message_excerpts("a" * 30 + "b" * 15)
        self.assertEqual(start, "a" * 30)
        self.assertEqual(end, "b" * 15)
        self.assertEqual(len(start) + len(end), 45)
        start, end = message_excerpts("a" * 30 + "b" * 40 + "c" * 30)
        self.assertEqual(end, "c" * 30)
        self.assertEqual(len(start) + len(end), 60)

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

    def test_hermes_observations_read_real_state_schema_without_identity_leaks(self):
        with TemporaryDirectory() as directory:
            state_db = Path(directory) / "state.db"
            connection = sqlite3.connect(state_db)
            connection.executescript("""
                CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    timestamp REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                INSERT INTO sessions VALUES ('private-session-name', 'hermes-test-model');
                INSERT INTO messages (session_id, role, content, timestamp, active)
                    VALUES ('private-session-name', 'user', 'private prompt', 1786579200, 1);
                INSERT INTO messages (session_id, role, content, timestamp, active)
                    VALUES ('private-session-name', 'assistant', '(•‿•) Hermes works', 1786579201, 1);
                INSERT INTO messages (session_id, role, content, timestamp, active)
                    VALUES ('private-session-name', 'assistant', '(._.) rewound', 1786579202, 0);
            """)
            connection.close()
            result = list(hermes_observations(state_db, set()))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["message_start"], "(•‿•) Hermes works")
        self.assertEqual(result[0]["harness"], "hermes")
        self.assertEqual(result[0]["model"], "hermes-test-model")
        self.assertEqual(result[0]["observed_at"], "2026-08-13T00:00:01Z")
        self.assertTrue(result[0]["idempotency_key"].startswith("sha256:"))
        self.assertNotIn("conversation_hash", result[0])
        self.assertNotIn("private-session-name", json.dumps(result[0]))

    def test_hermes_structured_text_is_supported_and_malformed_schema_fails(self):
        with TemporaryDirectory() as directory:
            state_db = Path(directory) / "state.db"
            connection = sqlite3.connect(state_db)
            connection.executescript("""
                CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, timestamp REAL, active INTEGER
                );
                INSERT INTO sessions VALUES ('s', NULL);
            """)
            structured = "\x00json:" + json.dumps([
                {"type": "text", "text": "(＾▽＾) "},
                {"type": "image_url", "image_url": "private"},
                {"type": "output_text", "text": "Done"},
            ])
            connection.execute(
                "INSERT INTO messages VALUES (1, 's', 'assistant', ?, 1786579201, 1)",
                (structured,),
            )
            connection.commit()
            connection.close()
            self.assertEqual(
                list(hermes_observations(state_db, set()))[0]["message_start"],
                "(＾▽＾) Done",
            )

            bad_db = Path(directory) / "bad.db"
            connection = sqlite3.connect(bad_db)
            connection.execute("CREATE TABLE sessions (id TEXT)")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(RuntimeError, "unsupported .* schema"):
                list(hermes_observations(bad_db, set()))

    def test_locked_hermes_database_terminates_with_concrete_error(self):
        with TemporaryDirectory() as directory:
            state_db = Path(directory) / "state.db"
            connection = sqlite3.connect(state_db)
            connection.executescript("""
                CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, timestamp REAL, active INTEGER
                );
            """)
            connection.execute("BEGIN EXCLUSIVE")
            with patch("kaomojo_client.cli.HERMES_SQLITE_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(RuntimeError, "Cannot read Hermes sessions.*locked"):
                    list(hermes_observations(state_db, set()))
            connection.rollback()
            connection.close()

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
                hermes_state=root / "missing-hermes.db",
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
            self.assertIn(f'Environment="HOME={root}"', service)
            self.assertIn(f'Environment="XDG_CONFIG_HOME={root / ".config"}"', service)
            self.assertIn(f'Environment="XDG_STATE_HOME={root / ".local/state"}"', service)
            self.assertIn("TimeoutStartSec=150", service)
            self.assertIn("OnCalendar=*:0/5", timer)
            self.assertIn("AccuracySec=15s", timer)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args_list[0].args[0][0:3], [
                "systemctl", "--user", "link",
            ])
            self.assertFalse(run.call_args_list[0].kwargs["check"])
            self.assertEqual(
                run.call_args_list[-1].args[0],
                ["systemctl", "--user", "enable", "--now", "kaomojo-collect.timer"],
            )
            self.assertEqual(
                run.call_args_list[-1].kwargs["env"]["XDG_CONFIG_HOME"],
                str(root / ".config"),
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

    def test_windows_schedule_runs_every_five_minutes_and_is_verified(self):
        executable = Path("C:/Program Files/Kaomojo/kaomojo.exe")
        with patch("kaomojo_client.cli.run_scheduler_command") as run:
            configure_windows_schedule(executable)
        self.assertEqual(run.call_count, 2)
        create = run.call_args_list[0].args[0]
        self.assertEqual(create[:4], ["schtasks.exe", "/Create", "/TN", "Kaomojo Collect"])
        self.assertEqual(create[create.index("/SC") + 1:create.index("/F")], ["MINUTE", "/MO", "5"])
        self.assertIn('"C:/Program Files/Kaomojo/kaomojo.exe" collect', create)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["schtasks.exe", "/Query", "/TN", "Kaomojo Collect", "/FO", "LIST"],
        )

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
                hermes_state=root / "missing-hermes.db",
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

    def test_missing_model_warnings_are_summarized(self):
        from collections import Counter
        warnings = Counter()
        record_warnings(warnings, {"results": [
            {"accepted": True, "warnings": ["model_not_recorded"]},
            {"accepted": True, "warnings": ["model_not_recorded"]},
        ]})
        with patch("builtins.print") as output:
            print_warnings(warnings)
        output.assert_called_once_with("Warning: 2 observations had no model recorded")

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
                hermes_state=root / "missing-hermes.db",
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
                hermes_state=root / "missing-hermes.db",
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

    def test_history_import_progress_includes_locally_filtered_records(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            sessions.mkdir()
            (sessions / "session.jsonl").write_text(json.dumps({
                "type": "response_item",
                "timestamp": "2026-08-01T00:00:00Z",
                "payload": {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Completed the operation."}],
                },
            }), encoding="utf-8")
            credentials = root / "credentials.json"
            save_key(credentials, "ar_abcdefghijklmnopqrstuvwxyz")
            state = root / "state.json"
            state.write_text(json.dumps({
                "version": 2, "sent_ids": [], "initialized_sources": ["codex"],
            }), encoding="utf-8")
            args = SimpleNamespace(
                codex_sessions=sessions, claude_projects=root / "missing-claude",
                hermes_state=root / "missing-hermes.db",
                state=state, credentials=credentials,
                import_state=root / "history-import.json",
                lock=root / "client.lock", deadline=120,
            )
            with patch("kaomojo_client.cli.post_batch") as post:
                import_history(args)
            saved = json.loads(args.import_state.read_text(encoding="utf-8"))
            self.assertEqual(saved["total"], 1)
            self.assertEqual(len(saved["processed_ids"]), 1)
            post.assert_not_called()

    def test_client_lock_rejects_duplicate_operation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "client.lock"
            with client_lock(path):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with client_lock(path):
                        self.fail("duplicate lock unexpectedly acquired")


if __name__ == "__main__":
    unittest.main()
