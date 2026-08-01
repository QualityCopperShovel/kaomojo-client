from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import stat
import unittest
from unittest.mock import patch

from kaomojo_client.cli import load_key, observations, save_key


class ClientTest(unittest.TestCase):
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
            self.assertEqual(result[0]["model"], "gpt-test")
            self.assertTrue(result[0]["conversation_hash"].startswith("sha256:"))

    def test_invalid_key_is_rejected(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "does not look"):
                save_key(Path(directory) / "credentials.json", "not-a-key")


if __name__ == "__main__":
    unittest.main()
