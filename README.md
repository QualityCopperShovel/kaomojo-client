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

Paste the key at the hidden prompt. The client stores it in `~/.config/kaomojo/credentials.json` with user-only permissions. `KAOMOJO_API_KEY` may be used instead when a platform secret manager injects environment variables.

Skip existing history, then collect new sightings:

```bash
kaomojo collect --initialize
kaomojo collect
```

Run `kaomojo collect` periodically with your preferred scheduler. Codex sessions are read locally. Only the first 100 characters of assistant messages, model provenance when available, timestamps, and conversation hashes are submitted; prompts, full responses, and transcript paths stay local.

See the live [Kaomojo agent guide](https://kaomojo.com/agent-guide.md) for the API and privacy contract.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```
