# Kaomojo Client

Codex and Claude Code session parsing is provided by the shared, local-only, MIT-licensed `coding-agent-sessions` package. Kaomojo retains its own API key, payload projection, checkpoints, and product identity.

The default client for collecting kaomoji sightings from local Codex and Claude Code sessions and submitting them to [Kaomojo](https://kaomojo.com).

## Install

Python 3.10 or newer is required. Install with `pipx`:

```bash
pipx install git+https://github.com/QualityCopperShovel/kaomojo-client.git
```

## Set up

Create an API key at [kaomojo.com](https://kaomojo.com), then run:

```bash
kaomojo setup
```

When setup asks for it, paste your Kaomojo API key. The input is masked so the key is not displayed on screen. The client stores it privately, then marks existing conversations as already seen using local ID hashes. It does not store or upload their text.

Setup also schedules collection every five minutes in the background, using systemd on most Linux distributions, a LaunchAgent on macOS, and the current user's Windows Task Scheduler on Windows. It keeps working after the terminal closes without leaving Kaomojo running continuously. Use `kaomojo schedule` to repair or refresh it. If no supported scheduler is available, setup stops with a clear error; configure an equivalent scheduler, then rerun setup with `--no-schedule`.

The scheduled collector checks for an approved client release at most once per day and updates automatically through `pipx`. The release manifest comes from kaomojo.com and pins an immutable commit in the official repository, so changing the GitHub default branch alone cannot distribute an update. Update checks and installations have fixed deadlines; a failure is recorded locally and printed as a warning without blocking collection.

Start collecting:

```bash
kaomojo collect
```

## Update

Because the client is installed directly from GitHub, refresh it with:

```bash
pipx install --force --pip-args=--no-cache-dir git+https://github.com/QualityCopperShovel/kaomojo-client.git
```

The forced no-cache install is necessary because `pipx upgrade` and `pipx reinstall` may reuse the original VCS build.

Every `kaomojo collect` run submits only sightings created after setup. By default it scans all JSONL sessions in the user's Codex sessions directory and every project in the user's Claude Code projects directory, not only the current project. Only the first 30 and last 30 characters of assistant messages, model provenance when available, timestamps, and conversation hashes are submitted; prompts, full responses, and transcript paths stay local.

For compatibility debugging, each request also reports the Kaomojo client version, OS family and major version, CPU architecture, Python major/minor version, and harnesses represented in that request. It never sends a hostname, username, device identifier, path, location, or installed-package list. The server retains only the latest environment for each account.

Sightings without model provenance are still accepted. The API returns a structured `model_not_recorded` warning for each affected observation, and the client summarizes those warnings after collection.

## Import earlier sightings

Setup starts fresh by default. To scan conversations that existed before setup, run:

```bash
kaomojo import-history
```

This still sends only each assistant message's first 30 and last 30 characters for kaomoji extraction—never the middle of a message, prompts, full responses, or transcript files. Large imports process newest history first, skip definite plain-ASCII prose locally, pack up to 20 observations per request, checkpoint after every completed batch, and stop after one hour; rerun the same command to resume. If the service reports an extraction-capacity failure, the client splits only that batch and preserves every per-item result. Classification rejections are checkpointed and summarized by reason at the end. Stable IDs make retries safe, including for users who already completed part of an import.

Kaomojo's own end-to-end test harness can set `KAOMOJO_API_URL` to the isolated staging collector. Ordinary installations should leave it unset and use `https://kaomojo.com`.

See the live [Kaomojo agent guide](https://kaomojo.com/agent-guide.md) for the API and privacy contract.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```
