"""Command-line client for collecting Kaomojo sightings from Codex sessions."""

from pathlib import Path
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import getpass
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time
import unicodedata

import requests
from coding_agent_sessions import claude_assistant_events, codex_assistant_events
from platformdirs import user_config_path, user_state_path
from . import __version__


API_URL = os.environ.get("KAOMOJO_API_URL", "https://kaomojo.com/api/v1/kaomojis")
DEFAULT_CONFIG = user_config_path("kaomojo", appauthor=False)
DEFAULT_STATE = user_state_path("kaomojo", appauthor=False)
DEFAULT_SESSIONS = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
DEFAULT_CLAUDE_PROJECTS = Path(
    os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")
) / "projects"
COLLECTION_INTERVAL_SECONDS = 300
SCHEDULER_TIMEOUT_SECONDS = 15
WINDOWS_TASK_NAME = "Kaomojo Collect"
DEFAULT_IMPORT_DEADLINE_SECONDS = 3600
MAX_BATCH_ITEMS = 20
TARGET_BATCH_BYTES = 24 * 1024
MIN_REQUEST_SECONDS = 5
# Kaomojo accepts at most this many characters per excerpt, and two excerpts
# per observation: the opening one and the non-overlapping closing one.
EXCERPT_LIMIT = 30
UPDATE_MANIFEST_URL = "https://kaomojo.com/api/v1/client-release"
UPDATE_REPOSITORY = "https://github.com/QualityCopperShovel/kaomojo-client.git"
UPDATE_INTERVAL_SECONDS = 24 * 60 * 60
UPDATE_TIMEOUT_SECONDS = 180


class SubmissionError(RuntimeError):
    def __init__(self, status_code, message, code=None, retry_after=None):
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after
        super().__init__(message)


ASCII_FACE_PATTERN = re.compile(
    r"(?:[:;=8xX][\-^'\"]?[)(/\\DPpOo]|[)(/\\DPpOo][\-^'\"]?[:;=8xX])"
)


def excerpt_may_contain_kaomoji(excerpt):
    """Reject only plain ASCII prose that cannot contain a text face."""
    if any(ord(character) > 127 for character in excerpt):
        return True
    if ASCII_FACE_PATTERN.search(excerpt):
        return True
    return any(character in excerpt for character in "()[]{}<>\\/^_|~`")


def may_contain_kaomoji(observation):
    """True when either submitted excerpt could hold a face.

    Both ends are checked: agents open with a kaomoji when instructed to, but
    some sign off with one instead, and a start-only test drops those.
    """
    return any(
        excerpt_may_contain_kaomoji(observation[field])
        for field in ("message_start", "message_end")
        if observation.get(field)
    )


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Update manifest contains an invalid version")
    return tuple(int(part) for part in value.split("."))


def maybe_auto_update(state_path, now=None):
    now = now or datetime.now(timezone.utc)
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            checked_at = datetime.fromisoformat(state["checked_at"])
            if (now - checked_at).total_seconds() < UPDATE_INTERVAL_SECONDS:
                return False
        except (OSError, ValueError, KeyError, TypeError):
            pass
    state = {"checked_at": now.isoformat(), "status": "checking", "error": None}
    atomic_private_json(state_path, state)
    try:
        response = requests.get(UPDATE_MANIFEST_URL, timeout=10)
        response.raise_for_status()
        manifest = response.json()
        if not isinstance(manifest, dict) or set(manifest) != {"version", "repository", "commit"}:
            raise RuntimeError("Update manifest is malformed")
        target_version = manifest["version"]
        if manifest["repository"] != UPDATE_REPOSITORY:
            raise RuntimeError("Update manifest repository is not trusted")
        if not isinstance(manifest["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]):
            raise RuntimeError("Update manifest commit is invalid")
        if version_tuple(target_version) <= version_tuple(__version__):
            state["status"] = "current"
            atomic_private_json(state_path, state)
            return False
        pipx = shutil.which("pipx")
        if not pipx:
            raise RuntimeError("pipx is unavailable; cannot install the approved update")
        source = f"git+{UPDATE_REPOSITORY}@{manifest['commit']}"
        subprocess.run(
            [pipx, "install", "--force", "--pip-args=--no-cache-dir", source],
            check=True, capture_output=True, text=True, timeout=UPDATE_TIMEOUT_SECONDS,
        )
        executable = shutil.which("kaomojo")
        if not executable:
            raise RuntimeError("Updated Kaomojo executable is unavailable")
        verified = subprocess.run(
            [executable, "--version"], check=True, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        if verified != f"kaomojo {target_version}":
            raise RuntimeError(f"Updated client failed version verification: {verified}")
        state.update({"status": "updated", "installed_version": target_version})
        atomic_private_json(state_path, state)
        print(f"Updated Kaomojo client to {target_version}")
        return True
    except (OSError, ValueError, RuntimeError, requests.RequestException, subprocess.SubprocessError) as error:
        state.update({"status": "failed", "error": str(error)})
        atomic_private_json(state_path, state)
        print(f"Warning: automatic update failed: {error}", file=sys.stderr)
        return False


def client_environment(batch):
    system = platform.system()
    os_family = "macOS" if system == "Darwin" else (system or "Unknown")
    if system == "Darwin":
        version = platform.mac_ver()[0] or platform.release()
    else:
        version = platform.release()
    return {
        "client_name": "kaomojo-client",
        "client_version": __version__,
        "os_family": os_family,
        "os_major": version.split(".", 1)[0] or "Unknown",
        "architecture": platform.machine() or "Unknown",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "harnesses": sorted({
            item["harness"] for item in batch
            if isinstance(item.get("harness"), str) and item["harness"]
        }),
    }


def sanitize_excerpt(text):
    """Make an excerpt API-safe: no control characters, no surrounding space."""
    return "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in text
    ).strip()


def message_excerpts(text):
    """The opening and closing 30-character excerpts of a message.

    The tail is taken from what remains after the opening excerpt, so the two
    never overlap and a message shorter than 60 characters is never sent
    twice. The tail is None when the whole message fits in the opening
    excerpt, or when it holds nothing but whitespace.
    """
    return sanitize_excerpt(text[:EXCERPT_LIMIT]), (
        sanitize_excerpt(text[EXCERPT_LIMIT:][-EXCERPT_LIMIT:]) or None
    )


def build_observation(event, context=None):
    start, end = message_excerpts(event["text"])
    item = {
        "idempotency_key": event["local_event_id"],
        "message_start": start,
        "harness": event["harness"],
        "observed_at": event["timestamp"],
        "conversation_hash": event["conversation_hash"],
    }
    if end:
        item["message_end"] = end
    if event["model"]:
        item["model"] = event["model"]
    if context:
        item["context"] = context
    return item


def observations(session_dir, sent_ids, context=None):
    for event in codex_assistant_events(session_dir):
        if event["local_event_id"] not in sent_ids:
            yield build_observation(event, context)


def claude_observations(projects_dir, sent_ids, context=None):
    for event in claude_assistant_events(projects_dir):
        if event["local_event_id"] not in sent_ids:
            yield build_observation(event, context)


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


def post_batch(session, api_key, batch, deadline_seconds=120):
    if deadline_seconds <= 0:
        raise ValueError("Submission deadline must be positive")
    deadline = time.monotonic() + deadline_seconds
    for attempt in range(4):
        remaining = deadline - time.monotonic()
        if remaining < MIN_REQUEST_SECONDS:
            raise TimeoutError("Kaomojo submission deadline expired before another usable attempt")
        try:
            response = session.post(
                API_URL,
                headers={"X-API-Key": api_key},
                json={
                    "observations": batch,
                    "client_environment": client_environment(batch),
                },
                timeout=min(100, remaining),
            )
        except requests.RequestException:
            if attempt == 3 or time.monotonic() >= deadline:
                raise
            time.sleep(min(2**attempt, max(0, deadline - time.monotonic())))
            continue
        error_code = None
        error_message = None
        try:
            error_payload = response.json().get("error", {})
            if isinstance(error_payload, dict):
                error_code = error_payload.get("code")
                error_message = error_payload.get("message")
        except (ValueError, AttributeError):
            pass
        if response.status_code != 429 and response.status_code < 500:
            if not response.ok:
                raise SubmissionError(
                    response.status_code,
                    f"Kaomojo rejected the batch with HTTP {response.status_code}: "
                    f"{error_message or response.text[:200] or 'unknown error'}",
                    code=error_code,
                )
            payload = response.json()
            results = payload.get("results")
            if (
                not isinstance(payload.get("accepted"), int)
                or not isinstance(payload.get("rejected"), int)
                or payload["accepted"] + payload["rejected"] != len(batch)
                or not isinstance(results, list)
                or len(results) != len(batch)
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("idempotency_key"), str)
                    or not isinstance(item.get("accepted"), bool)
                    for item in results
                )
            ):
                raise RuntimeError("Kaomojo returned a malformed success response")
            return payload
        retry_after = response.headers.get("Retry-After")
        try:
            retry_after = max(0.0, float(retry_after))
        except (TypeError, ValueError):
            retry_after = None
        if error_code == "extraction_capacity":
            raise SubmissionError(
                response.status_code,
                error_message or "Kaomoji extraction capacity was exceeded",
                code=error_code,
                retry_after=retry_after,
            )
        if attempt == 3 or deadline - time.monotonic() < MIN_REQUEST_SECONDS:
            raise SubmissionError(
                response.status_code,
                error_message or f"Kaomoji submission failed with HTTP {response.status_code}",
                code=error_code,
                retry_after=retry_after,
            )
        delay = retry_after if retry_after is not None else 2**attempt
        time.sleep(min(delay, max(0, deadline - time.monotonic())))


def post_resilient_batch(session, api_key, batch, deadline_seconds=120):
    """Split only capacity-limited batches; preserve every per-item result."""
    started = time.monotonic()
    try:
        return post_batch(session, api_key, batch, deadline_seconds)
    except SubmissionError as error:
        if error.code != "extraction_capacity" or len(batch) == 1:
            raise
        remaining = deadline_seconds - (time.monotonic() - started)
        if remaining < MIN_REQUEST_SECONDS * 2:
            raise TimeoutError("Kaomojo submission deadline expired while splitting a batch") from error
        midpoint = len(batch) // 2
        left = post_resilient_batch(session, api_key, batch[:midpoint], remaining / 2)
        right = post_resilient_batch(session, api_key, batch[midpoint:], remaining / 2)
        return {
            "accepted": left["accepted"] + right["accepted"],
            "rejected": left["rejected"] + right["rejected"],
            "results": left["results"] + right["results"],
        }
def record_rejections(reasons, result):
    reasons.update(
        item.get("reason", "Rejected without a reason")
        for item in result["results"]
        if not item["accepted"]
    )


def print_rejections(reasons):
    if not reasons:
        return
    print("Rejected observations:")
    for reason, count in reasons.most_common():
        print(f"  {count} × {reason}")


def record_warnings(warnings, result):
    warnings.update(
        warning
        for item in result["results"]
        for warning in item.get("warnings", [])
    )


def print_warnings(warnings):
    if warnings.get("model_not_recorded"):
        print(f"Warning: {warnings['model_not_recorded']} observations had no model recorded")


def observation_batches(observations):
    """Pack API batches near the service limit without crossing its body cap."""
    batch = []
    body_size = len(json.dumps({"observations": []}).encode("utf-8"))
    for observation in observations:
        item_size = len(json.dumps(observation).encode("utf-8"))
        candidate_size = body_size + item_size + (2 if batch else 0)
        if batch and (len(batch) >= MAX_BATCH_ITEMS or candidate_size > TARGET_BATCH_BYTES):
            yield batch
            batch = [observation]
            body_size = len(json.dumps({"observations": []}).encode("utf-8")) + item_size
        else:
            batch.append(observation)
            body_size = candidate_size
        if body_size > TARGET_BATCH_BYTES:
            raise ValueError("One observation is too large for Kaomojo's request limit")
    if batch:
        yield batch


@contextmanager
def client_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    with path.open("a+", encoding="utf-8") as lock:
        path.chmod(0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another Kaomojo collection operation is already running") from error
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def collect(args):
    with client_lock(args.lock):
        collect_locked(args)


def collect_locked(args):
    if args.context and len(args.context) > 200:
        raise ValueError("--context must be at most 200 characters")
    if not args.state.exists():
        raise RuntimeError("Run `kaomojo setup` first")
    maybe_auto_update(args.state.parent / "update.json")
    sent_ids, initialized_sources = load_state(args.state)
    newly_initialized = baseline_new_sources(args, sent_ids, initialized_sources)
    for source, count in newly_initialized.items():
        print(f"Initialized {source}: {count} existing observations marked as seen")
    pending = list(all_observations(
        args.codex_sessions, args.claude_projects, sent_ids, args.context,
    ))
    filtered = [item for item in pending if not may_contain_kaomoji(item)]
    pending = [item for item in pending if may_contain_kaomoji(item)]
    if filtered:
        sent_ids.update(item["idempotency_key"] for item in filtered)
        save_state(args.state, sent_ids, initialized_sources)
    api_key = load_key(args.credentials)
    accepted = rejected = 0
    rejection_reasons = Counter()
    warnings = Counter()
    with requests.Session() as session:
        for batch in observation_batches(pending):
            result = post_resilient_batch(session, api_key, batch)
            accepted += result["accepted"]
            rejected += result["rejected"]
            record_rejections(rejection_reasons, result)
            record_warnings(warnings, result)
            sent_ids.update(item["idempotency_key"] for item in batch)
            save_state(args.state, sent_ids, initialized_sources)
    print(
        f"Complete: {accepted} accepted, {rejected} rejected, "
        f"{len(filtered)} plain-text prefixes skipped locally"
    )
    print_rejections(rejection_reasons)
    print_warnings(warnings)


def new_import_state():
    now = datetime.now(timezone.utc).isoformat()
    return {
        "version": 1,
        "status": "pending",
        "processed_ids": [],
        "started_at": now,
        "updated_at": now,
        "total": None,
        "error": None,
    }


def load_import_state(path):
    if not path.exists():
        return new_import_state()
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("status") not in {
            "pending", "running", "timed_out", "failed", "cancelled", "completed",
        }
        or not isinstance(value.get("processed_ids"), list)
        or not all(isinstance(item, str) for item in value["processed_ids"])
    ):
        raise ValueError(f"Invalid history import state file: {path}")
    return value


def save_import_state(path, state, status, total, error=None):
    state.update({
        "status": status,
        "processed_ids": sorted(set(state["processed_ids"])),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "error": error,
    })
    atomic_private_json(path, state)


def import_history(args):
    with client_lock(args.lock):
        import_history_locked(args)


def import_history_locked(args):
    if not args.state.exists():
        raise RuntimeError("Run `kaomojo setup` before importing history")
    if args.deadline < 120:
        raise ValueError("--deadline must be at least 120 seconds")
    deadline = time.monotonic() + args.deadline
    api_key = load_key(args.credentials)
    state = load_import_state(args.import_state)
    processed_ids = set(state["processed_ids"])
    pending = list(all_observations(
        args.codex_sessions, args.claude_projects, processed_ids,
    ))
    pending.sort(key=lambda item: item["observed_at"], reverse=True)
    filtered = [item for item in pending if not may_contain_kaomoji(item)]
    pending = [item for item in pending if may_contain_kaomoji(item)]
    state["processed_ids"].extend(item["idempotency_key"] for item in filtered)
    total = len(processed_ids) + len(filtered) + len(pending)
    save_import_state(args.import_state, state, "running", total)
    print(
        f"History import: {len(processed_ids)} already processed, "
        f"{len(filtered) + len(pending)} remaining "
        f"({len(filtered)} plain-text prefixes skipped locally, "
        f"{len(pending)} submission candidates)"
    )
    accepted = rejected = 0
    rejection_reasons = Counter()
    warnings = Counter()
    try:
        with requests.Session() as session:
            for batch in observation_batches(pending):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    save_import_state(
                        args.import_state, state, "timed_out", total,
                        "Overall history import deadline exceeded; rerun to resume",
                    )
                    raise RuntimeError(
                        "History import deadline exceeded; rerun `kaomojo import-history` to resume"
                    )
                result = post_resilient_batch(
                    session, api_key, batch, deadline_seconds=min(120, remaining),
                )
                accepted += result["accepted"]
                rejected += result["rejected"]
                record_rejections(rejection_reasons, result)
                record_warnings(warnings, result)
                state["processed_ids"].extend(item["idempotency_key"] for item in batch)
                save_import_state(args.import_state, state, "running", total)
                print(
                    f"History import: {len(state['processed_ids'])}/{total} processed "
                    f"({accepted} accepted, {rejected} rejected this run)"
                )
    except KeyboardInterrupt as error:
        save_import_state(args.import_state, state, "cancelled", total, "Cancelled by user")
        raise RuntimeError(
            "History import cancelled; rerun `kaomojo import-history` to resume"
        ) from error
    except (requests.RequestException, RuntimeError) as error:
        if state["status"] != "timed_out":
            status = "timed_out" if time.monotonic() >= deadline else "failed"
            save_import_state(args.import_state, state, status, total, str(error))
        raise
    save_import_state(args.import_state, state, "completed", total)
    print(f"History import complete: {accepted} accepted, {rejected} rejected this run")
    print_rejections(rejection_reasons)
    print_warnings(warnings)


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
    if not getattr(args, "no_schedule", False):
        configure_schedule(args)


def run_scheduler_command(command, check=True, env=None):
    try:
        return subprocess.run(
            command,
            check=check,
            capture_output=True,
            text=True,
            timeout=SCHEDULER_TIMEOUT_SECONDS,
            env=env,
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"Required scheduler command is unavailable: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Scheduler command timed out after {SCHEDULER_TIMEOUT_SECONDS} seconds: {command[0]}"
        ) from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "unknown scheduler error").strip()
        raise RuntimeError(f"Scheduler command failed: {detail}") from error


def systemd_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def configure_systemd_schedule(executable):
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    unit_dir = config_home / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    service = unit_dir / "kaomojo-collect.service"
    timer = unit_dir / "kaomojo-collect.timer"
    service.write_text(
        "[Unit]\n"
        "Description=Collect new Kaomojo sightings\n"
        "Documentation=https://kaomojo.com/agent-guide\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment={systemd_quote('HOME=' + str(Path.home()))}\n"
        f"Environment={systemd_quote('XDG_CONFIG_HOME=' + str(config_home))}\n"
        f"Environment={systemd_quote('XDG_STATE_HOME=' + str(state_home))}\n"
        f"ExecStart={systemd_quote(executable)} collect\n"
        "TimeoutStartSec=150\n",
        encoding="utf-8",
    )
    timer.write_text(
        "[Unit]\n"
        "Description=Collect new Kaomojo sightings every five minutes\n\n"
        "[Timer]\n"
        "OnCalendar=*:0/5\n"
        "Persistent=true\n"
        "AccuracySec=15s\n"
        "RandomizedDelaySec=15s\n"
        "Unit=kaomojo-collect.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n",
        encoding="utf-8",
    )
    scheduler_env = os.environ.copy()
    scheduler_env["XDG_CONFIG_HOME"] = str(config_home)
    run_scheduler_command(
        ["systemctl", "--user", "link", str(service), str(timer)],
        check=False,
        env=scheduler_env,
    )
    run_scheduler_command(
        ["systemctl", "--user", "daemon-reload"], env=scheduler_env,
    )
    run_scheduler_command(
        ["systemctl", "--user", "enable", "--now", timer.name], env=scheduler_env,
    )
    print(f"Scheduled collection every five minutes with {timer}")


def configure_launchd_schedule(executable):
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    label = "com.kaomojo.collect"
    plist = agents_dir / f"{label}.plist"
    log_dir = DEFAULT_STATE / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": label,
        "ProgramArguments": [str(executable), "collect"],
        "RunAtLoad": True,
        "StartInterval": COLLECTION_INTERVAL_SECONDS,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "collect.log"),
        "StandardErrorPath": str(log_dir / "collect-error.log"),
    }
    with plist.open("wb") as output:
        plistlib.dump(payload, output)
    domain = f"gui/{os.getuid()}"
    run_scheduler_command(
        ["launchctl", "bootout", domain, str(plist)],
        check=False,
    )
    run_scheduler_command(["launchctl", "bootstrap", domain, str(plist)])
    print(f"Scheduled collection every five minutes with {plist}")


def configure_windows_schedule(executable):
    task_command = subprocess.list2cmdline([str(executable), "collect"])
    run_scheduler_command([
        "schtasks.exe", "/Create", "/TN", WINDOWS_TASK_NAME,
        "/TR", task_command, "/SC", "MINUTE", "/MO", "5", "/F",
    ])
    run_scheduler_command([
        "schtasks.exe", "/Query", "/TN", WINDOWS_TASK_NAME, "/FO", "LIST",
    ])
    print(f"Scheduled collection every five minutes with Windows Task Scheduler ({WINDOWS_TASK_NAME})")


def configure_schedule(args):
    executable = Path(sys.argv[0]).resolve()
    if sys.platform.startswith("linux"):
        configure_systemd_schedule(executable)
    elif sys.platform == "darwin":
        configure_launchd_schedule(executable)
    elif sys.platform == "win32":
        configure_windows_schedule(executable)
    else:
        raise RuntimeError(
            f"Automatic recurring collection is not supported on {sys.platform}; "
            "run `kaomojo collect` every five minutes with the native scheduler"
        )


def parser():
    root = argparse.ArgumentParser(prog="kaomojo")
    root.add_argument("--version", action="version", version=f"kaomojo {__version__}")
    root.add_argument("--credentials", type=Path, default=DEFAULT_CONFIG / "credentials.json")
    commands = root.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup", help="Save your API key with user-only permissions")
    setup_parser.add_argument("--key-stdin", action="store_true", help=argparse.SUPPRESS)
    setup_parser.add_argument("--codex-sessions", type=Path, default=DEFAULT_SESSIONS)
    setup_parser.add_argument("--claude-projects", type=Path, default=DEFAULT_CLAUDE_PROJECTS)
    setup_parser.add_argument("--state", type=Path, default=DEFAULT_STATE / "codex-state.json")
    setup_parser.add_argument(
        "--no-schedule",
        action="store_true",
        help="Do not configure recurring five-minute collection",
    )
    setup_parser.set_defaults(handler=setup)
    schedule_parser = commands.add_parser(
        "schedule", help="Configure recurring five-minute collection",
    )
    schedule_parser.set_defaults(handler=configure_schedule)
    collect_parser = commands.add_parser(
        "collect", help="Collect new sightings from Codex and Claude Code sessions",
    )
    collect_parser.add_argument("--codex-sessions", type=Path, default=DEFAULT_SESSIONS)
    collect_parser.add_argument("--claude-projects", type=Path, default=DEFAULT_CLAUDE_PROJECTS)
    collect_parser.add_argument("--state", type=Path, default=DEFAULT_STATE / "codex-state.json")
    collect_parser.add_argument("--lock", type=Path, default=DEFAULT_STATE / "client.lock")
    collect_parser.add_argument("--context", help="Optional de-identified context, at most 200 characters")
    collect_parser.set_defaults(handler=collect)
    import_parser = commands.add_parser(
        "import-history",
        help="Import sightings from conversations that existed before setup",
    )
    import_parser.add_argument("--codex-sessions", type=Path, default=DEFAULT_SESSIONS)
    import_parser.add_argument("--claude-projects", type=Path, default=DEFAULT_CLAUDE_PROJECTS)
    import_parser.add_argument("--state", type=Path, default=DEFAULT_STATE / "codex-state.json")
    import_parser.add_argument(
        "--import-state", type=Path, default=DEFAULT_STATE / "history-import.json",
    )
    import_parser.add_argument("--lock", type=Path, default=DEFAULT_STATE / "client.lock")
    import_parser.add_argument(
        "--deadline",
        type=int,
        default=DEFAULT_IMPORT_DEADLINE_SECONDS,
        help="Overall deadline in seconds (default: 3600); rerun to resume",
    )
    import_parser.set_defaults(handler=import_history)
    return root


def main():
    args = parser().parse_args()
    try:
        args.handler(args)
    except (OSError, ValueError, RuntimeError, requests.RequestException) as error:
        raise SystemExit(f"Error: {error}") from error


if __name__ == "__main__":
    main()
