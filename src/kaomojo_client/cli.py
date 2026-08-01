"""Command-line client for collecting Kaomojo sightings from Codex sessions."""

from pathlib import Path
import argparse
import getpass
import hashlib
import json
import os
import sys
import time

import requests
from platformdirs import user_config_path, user_state_path


API_URL = "https://kaomojo.com/api/v1/kaomojis"
DEFAULT_CONFIG = user_config_path("kaomojo", appauthor=False)
DEFAULT_STATE = user_state_path("kaomojo", appauthor=False)
DEFAULT_SESSIONS = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"


def content_text(content):
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ).strip()


def normalized_model(value):
    return value.strip() if isinstance(value, str) and value.strip() != "<synthetic>" else None


def observations(session_dir, sent_ids, context=None):
    for path in sorted(session_dir.rglob("*.jsonl")):
        current_model = None
        snapshot = path.read_bytes()
        conversation_hash = f"sha256:{hashlib.sha256(snapshot).hexdigest()}"
        for line in snapshot.decode("utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload")
            if record.get("type") == "turn_context" and isinstance(payload, dict):
                current_model = normalized_model(payload.get("model")) or current_model
                continue
            if record.get("type") == "world_state" and isinstance(payload, dict):
                state = payload.get("state")
                if isinstance(state, dict):
                    current_model = normalized_model(state.get("model")) or current_model
                continue
            if record.get("type") != "response_item" or not isinstance(payload, dict):
                continue
            if payload.get("type") != "message" or payload.get("role") != "assistant":
                continue
            text = content_text(payload.get("content"))
            timestamp = record.get("timestamp")
            if not text or not isinstance(timestamp, str):
                continue
            observation_id = hashlib.sha256(
                f"{path}:{timestamp}:{text}".encode("utf-8")
            ).hexdigest()[:32]
            if observation_id in sent_ids:
                continue
            item = {
                "id": observation_id,
                "message_start": text[:100],
                "source": "codex",
                "observed_at": timestamp,
                "conversation_hash": conversation_hash,
            }
            if current_model:
                item["model"] = current_model
            if context:
                item["context"] = context
            yield item


def atomic_private_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def save_key(path, key):
    if not key.startswith("ar_") or len(key) < 20:
        raise ValueError("That does not look like a Kaomojo API key")
    atomic_private_json(path, {"api_key": key})


def load_key(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError("Run `kaomojo setup` first") from error
    key = payload.get("api_key", "").strip() if isinstance(payload, dict) else ""
    if not key:
        raise RuntimeError(f"No API key found in {path}")
    return key


def load_sent_ids(path):
    if not path.exists():
        return set()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Invalid state file: {path}")
    return set(value)


def post_batch(session, api_key, batch):
    deadline = time.monotonic() + 120
    for attempt in range(4):
        try:
            response = session.post(
                API_URL,
                headers={"X-API-Key": api_key},
                json={"observations": batch},
                timeout=min(100, max(1, deadline - time.monotonic())),
            )
        except requests.RequestException:
            if attempt == 3 or time.monotonic() >= deadline:
                raise
            time.sleep(min(2**attempt, max(0, deadline - time.monotonic())))
            continue
        if response.status_code not in {429, 503}:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload.get("accepted"), int) or not isinstance(payload.get("rejected"), int):
                raise RuntimeError("Kaomojo returned a malformed success response")
            return payload
        if attempt == 3 or time.monotonic() >= deadline:
            response.raise_for_status()
        try:
            delay = int(response.headers.get("Retry-After", 2**attempt))
        except ValueError:
            delay = 2**attempt
        time.sleep(min(delay, max(0, deadline - time.monotonic())))
    raise RuntimeError("Kaomojo submission did not reach a terminal state")


def collect(args):
    if not args.sessions.is_dir():
        raise RuntimeError(f"Codex sessions directory not found: {args.sessions}")
    if args.context and len(args.context) > 200:
        raise ValueError("--context must be at most 200 characters")
    if not args.state.exists():
        raise RuntimeError("Run `kaomojo setup` first")
    sent_ids = load_sent_ids(args.state)
    pending = list(observations(args.sessions, sent_ids, args.context))
    api_key = load_key(args.credentials)
    accepted = rejected = 0
    with requests.Session() as session:
        for start in range(0, len(pending), 100):
            batch = pending[start : start + 100]
            result = post_batch(session, api_key, batch)
            accepted += result["accepted"]
            rejected += result["rejected"]
            sent_ids.update(item["id"] for item in batch)
            atomic_private_json(args.state, sorted(sent_ids))
    print(f"Complete: {accepted} accepted, {rejected} rejected, {len(pending)} processed")


def setup(args):
    if not args.sessions.is_dir():
        raise RuntimeError(f"Codex sessions directory not found: {args.sessions}")
    key = sys.stdin.readline().strip() if args.key_stdin else getpass.getpass("Paste your Kaomojo API key: ").strip()
    save_key(args.credentials, key)
    print(f"Saved your API key securely in {args.credentials}")
    if args.state.exists():
        print("Collection was already initialized; existing state was preserved")
        return
    existing = list(observations(args.sessions, set()))
    atomic_private_json(args.state, sorted(item["id"] for item in existing))
    print(f"Initialized collection: {len(existing)} existing observations marked as seen")


def parser():
    root = argparse.ArgumentParser(prog="kaomojo")
    root.add_argument("--credentials", type=Path, default=DEFAULT_CONFIG / "credentials.json")
    commands = root.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup", help="Save your API key with user-only permissions")
    setup_parser.add_argument("--key-stdin", action="store_true", help=argparse.SUPPRESS)
    setup_parser.add_argument("--sessions", type=Path, default=DEFAULT_SESSIONS)
    setup_parser.add_argument("--state", type=Path, default=DEFAULT_STATE / "codex-state.json")
    setup_parser.set_defaults(handler=setup)
    collect_parser = commands.add_parser("collect", help="Collect new sightings from Codex sessions")
    collect_parser.add_argument("--sessions", type=Path, default=DEFAULT_SESSIONS)
    collect_parser.add_argument("--state", type=Path, default=DEFAULT_STATE / "codex-state.json")
    collect_parser.add_argument("--context", help="Optional de-identified context, at most 200 characters")
    collect_parser.set_defaults(handler=collect)
    return root


def main():
    args = parser().parse_args()
    try:
        args.handler(args)
    except (OSError, ValueError, RuntimeError, requests.RequestException) as error:
        raise SystemExit(f"Error: {error}") from error


if __name__ == "__main__":
    main()
