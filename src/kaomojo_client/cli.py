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
DEFAULT_CLAUDE_PROJECTS = Path(
    os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")
) / "projects"


def content_text(content, allowed_types=None):
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item.get("text", "")
        for item in content
        if (
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and (allowed_types is None or item.get("type") in allowed_types)
        )
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
                "idempotency_key": observation_id,
                "message_start": text[:50],
                "source": "codex",
                "observed_at": timestamp,
                "conversation_hash": conversation_hash,
            }
            if current_model:
                item["model"] = current_model
            if context:
                item["context"] = context
            yield item


def claude_observations(projects_dir, sent_ids, context=None):
    for path in sorted(projects_dir.rglob("*.jsonl")):
        snapshot = path.read_bytes()
        conversation_hash = f"sha256:{hashlib.sha256(snapshot).hexdigest()}"
        for line in snapshot.decode("utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            text = content_text(message.get("content"), {"text"})
            timestamp = record.get("timestamp")
            if not text or not isinstance(timestamp, str):
                continue
            observation_id = hashlib.sha256(
                f'{path}:{record.get("uuid", "")}:{timestamp}:{text}'.encode("utf-8")
            ).hexdigest()[:32]
            if observation_id in sent_ids:
                continue
            item = {
                "idempotency_key": observation_id,
                "message_start": text[:50],
                "source": "claude_code",
                "observed_at": timestamp,
                "conversation_hash": conversation_hash,
            }
            model = normalized_model(message.get("model"))
            if model:
                item["model"] = model
            if context:
                item["context"] = context
            yield item


def source_readers(codex_sessions, claude_projects, sent_ids, context=None):
    readers = {}
    if codex_sessions.is_dir():
        readers["codex"] = observations(codex_sessions, sent_ids, context)
    if claude_projects.is_dir():
        readers["claude_code"] = claude_observations(
            claude_projects, sent_ids, context,
        )
    if not readers:
        raise RuntimeError(
            f"No Codex sessions at {codex_sessions} or Claude Code projects at {claude_projects}"
        )
    return readers


def all_observations(codex_sessions, claude_projects, sent_ids, context=None):
    for reader in source_readers(
        codex_sessions, claude_projects, sent_ids, context,
    ).values():
        yield from reader


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
    return load_state(path)[0]


def load_state(path):
    if not path.exists():
        return set(), set()
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value), {"codex"}
    if (
        not isinstance(value, dict)
        or value.get("version") != 2
        or not isinstance(value.get("sent_ids"), list)
        or not all(isinstance(item, str) for item in value["sent_ids"])
        or not isinstance(value.get("initialized_sources"), list)
        or not all(isinstance(item, str) for item in value["initialized_sources"])
    ):
        raise ValueError(f"Invalid state file: {path}")
    return set(value["sent_ids"]), set(value["initialized_sources"])


def save_state(path, sent_ids, initialized_sources):
    atomic_private_json(path, {
        "version": 2,
        "sent_ids": sorted(sent_ids),
        "initialized_sources": sorted(initialized_sources),
    })


def baseline_new_sources(args, sent_ids, initialized_sources):
    readers = source_readers(
        args.codex_sessions, args.claude_projects, set(),
    )
    added = {}
    for source, reader in readers.items():
        if source in initialized_sources:
            continue
        ids = {item["idempotency_key"] for item in reader}
        sent_ids.update(ids)
        initialized_sources.add(source)
        added[source] = len(ids)
    save_state(args.state, sent_ids, initialized_sources)
    return added


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
    if args.context and len(args.context) > 200:
        raise ValueError("--context must be at most 200 characters")
    if not args.state.exists():
        raise RuntimeError("Run `kaomojo setup` first")
    sent_ids, initialized_sources = load_state(args.state)
    newly_initialized = baseline_new_sources(args, sent_ids, initialized_sources)
    for source, count in newly_initialized.items():
        print(f"Initialized {source}: {count} existing observations marked as seen")
    pending = list(all_observations(
        args.codex_sessions, args.claude_projects, sent_ids, args.context,
    ))
    api_key = load_key(args.credentials)
    accepted = rejected = 0
    with requests.Session() as session:
        for start in range(0, len(pending), 100):
            batch = pending[start : start + 100]
            result = post_batch(session, api_key, batch)
            accepted += result["accepted"]
            rejected += result["rejected"]
            sent_ids.update(item["idempotency_key"] for item in batch)
            save_state(args.state, sent_ids, initialized_sources)
    print(f"Complete: {accepted} accepted, {rejected} rejected, {len(pending)} processed")


def setup(args):
    key = sys.stdin.readline().strip() if args.key_stdin else getpass.getpass("Paste your Kaomojo API key: ").strip()
    save_key(args.credentials, key)
    print(f"Saved your API key securely in {args.credentials}")
    sent_ids, initialized_sources = load_state(args.state)
    added = baseline_new_sources(args, sent_ids, initialized_sources)
    if added:
        for source, count in added.items():
            print(f"Initialized {source}: {count} existing observations marked as seen")
    else:
        print("Collection was already initialized; existing state was preserved")


def parser():
    root = argparse.ArgumentParser(prog="kaomojo")
    root.add_argument("--credentials", type=Path, default=DEFAULT_CONFIG / "credentials.json")
    commands = root.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup", help="Save your API key with user-only permissions")
    setup_parser.add_argument("--key-stdin", action="store_true", help=argparse.SUPPRESS)
    setup_parser.add_argument("--codex-sessions", type=Path, default=DEFAULT_SESSIONS)
    setup_parser.add_argument("--claude-projects", type=Path, default=DEFAULT_CLAUDE_PROJECTS)
    setup_parser.add_argument("--state", type=Path, default=DEFAULT_STATE / "codex-state.json")
    setup_parser.set_defaults(handler=setup)
    collect_parser = commands.add_parser("collect", help="Collect new sightings from Codex sessions")
    collect_parser.add_argument("--codex-sessions", type=Path, default=DEFAULT_SESSIONS)
    collect_parser.add_argument("--claude-projects", type=Path, default=DEFAULT_CLAUDE_PROJECTS)
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
