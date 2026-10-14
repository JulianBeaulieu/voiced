# Contributing

Thanks for your interest. voiced is pre-alpha, so expect the ground to move.

## Setup

```sh
git clone https://github.com/JulianBeaulieu/voiced
cd voiced
uv sync --group dev          # core plus dev tools; no models
uv sync --group dev --extra stt --extra tts   # with the MLX engines (Apple Silicon only)
```

## Checks

Run these before opening a pull request; CI runs the same three.

```sh
uv run ruff check . && uv run ruff format .
uv run mypy
uv run pytest
```

Unit tests must pass without any model installed: use the fake engines in
`voiced.testing`. Tests that need real models go under a `models` marker and
are run manually or nightly.

## Rules

- Branch off `main` and open a pull request. Direct pushes to `main` are not
  accepted.
- Conventional commit messages (`feat:`, `fix:`, `docs:`, `chore:`, ...).
  No AI attribution and no `Co-Authored-By` trailers.
- No emoji in code, docs or UI.
- Nothing personal in the repo: no hostnames, IPs, usernames or tokens.
  Deployment settings belong in your own `~/.config/voiced/config.toml`.
- Do not vendor model weights.

## Adding an engine

Implement `voiced.engines.base.STTEngine` or `TTSEngine`, then register it as
an entry point in your package's `pyproject.toml`:

```toml
[project.entry-points."voiced.tts_engines"]
piper = "my_package.piper:PiperEngine"
```

`engine = "piper"` in the config then selects it. The docstrings in
`voiced/engines/base.py` are the contract; `voiced/testing/fakes.py` is a
minimal implementation.
