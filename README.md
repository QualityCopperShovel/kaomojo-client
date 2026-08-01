# Kaomojo Client

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

Paste the key at the hidden prompt. The client stores it privately, then marks existing conversations as already seen using local ID hashes. It does not store or upload their text.

Setup also installs a native five-minute recurring collector. The default architecture is a user-level systemd timer on most Linux distributions and a LaunchAgent on macOS, so collection survives terminal exits without requiring a long-running Kaomojo process. Use `kaomojo schedule` to repair or refresh it. If that native scheduler is unavailable, setup stops with a concrete error; configure an equivalent five-minute scheduler first, then use `kaomojo setup --no-schedule` to declare that it is externally owned.

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

Every `kaomojo collect` run submits only sightings created after setup. Codex and Claude Code sessions are read locally. Only the first 30 characters of assistant messages, model provenance when available, timestamps, and conversation hashes are submitted; prompts, full responses, and transcript paths stay local.

See the live [Kaomojo agent guide](https://kaomojo.com/agent-guide.md) for the API and privacy contract.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```
