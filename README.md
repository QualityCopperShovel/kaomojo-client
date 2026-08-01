# Kaomojo Client

The default client for collecting kaomoji sightings from local Codex sessions and submitting them to [Kaomojo](https://kaomojo.com).

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

Paste the key at the hidden prompt. The client stores it in the operating system's standard per-user configuration directory with user-only permissions and prints the exact location. It also automatically marks conversations already on disk as seen.

Start collecting:

```bash
kaomojo collect
```

Every `kaomojo collect` run submits only sightings created after setup. Run it periodically with your preferred scheduler. Codex sessions are read locally. Only the first 50 characters of assistant messages, model provenance when available, timestamps, and conversation hashes are submitted; prompts, full responses, and transcript paths stay local.

See the live [Kaomojo agent guide](https://kaomojo.com/agent-guide.md) for the API and privacy contract.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```
