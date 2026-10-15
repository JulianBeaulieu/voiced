# Contributing

Thanks for your interest. voiced is pre-alpha and the ground still moves,
but the engine interfaces are frozen and the conventions below are stable.

## Development setup

Requirements: [uv](https://docs.astral.sh/uv/), Python 3.11 to 3.13 (uv
installs 3.12 from `.python-version` if needed). The MLX engines need an
Apple Silicon Mac plus `brew install espeak-ng ffmpeg`; everything else runs
anywhere.

```sh
git clone https://github.com/JulianBeaulieu/voiced
cd voiced
uv sync --group dev                              # core plus dev tools, no models
uv sync --group dev --extra stt --extra tts      # add the MLX engines (Apple Silicon)
```

Run a service in the foreground against the fake engines:

```sh
VOICED_TTS__ENGINE=fake uv run voiced serve tts    # http://127.0.0.1:8880/health
VOICED_STT__ENGINE=fake uv run voiced serve stt    # http://127.0.0.1:8881/health
```

Set `VOICED_CONFIG=/nonexistent` as well if your `~/.config/voiced/config.toml`
contains keys your branch does not know yet; unknown keys are errors.

## Checks

CI runs exactly these four on a GitHub `macos-14` runner. Run them before
you open a pull request.

```sh
uv run ruff check .
uv run ruff format --check .     # or `ruff format .` to fix
uv run mypy
uv run pytest
```

| Tool | Rules |
|---|---|
| ruff | Line length 100. Rule sets `E`, `F`, `I`, `UP`, `B`, `ASYNC`, `RUF`. Imports sorted by ruff. `ruff format` also formats Python code blocks in Markdown, so keep doc snippets formatted. |
| mypy | `strict` over the `voiced` package. Third-party MLX and Wyoming packages are `ignore_missing_imports`; add new untyped dependencies to that list in `pyproject.toml`, do not sprinkle `type: ignore`. |
| pytest | `asyncio_mode = "auto"`; `DeprecationWarning` from `voiced.*` is an error. |

## Tests

The default `pytest` run must pass on a machine with no model, no GPU and no
MLX packages installed, because that is what CI is. Tests that need ffmpeg
skip when it is absent; CI installs it, so install it locally too
(`brew install ffmpeg`) to run them. Use `FakeSTTEngine` and
`FakeTTSEngine` from `voiced.testing` and the `lifespan_client` helper for
HTTP tests.

Tests that need real models or Metal carry the `model` marker (registered
in `pyproject.toml`) and are skipped by `tests/conftest.py` unless
`VOICED_MODEL_TESTS=1` is set:

```python
import pytest

pytestmark = pytest.mark.model  # whole module


@pytest.mark.model
async def test_kokoro_first_chunk_latency(): ...
```

```sh
VOICED_MODEL_TESTS=1 uv run pytest -m model          # only the model tests
VOICED_MODEL_TESTS=1 uv run pytest                   # everything
```

The `model-tests` job in `.github/workflows/ci.yml` runs the same command on
demand (`workflow_dispatch`) on a runner you name.

Audio fixtures go in `tests/fixtures/`, 16 kHz mono WAV, a few seconds,
under 300 KB each, free of personal content. Never commit model weights.

## Conventions

- Branch off `main` and open a pull request. `main` is protected.
- Conventional commit messages: `feat(stt): ...`, `fix(api): ...`,
  `docs: ...`, `chore(ci): ...`, `test(wyoming): ...`. Scopes are the package
  directories (`stt`, `tts`, `api`, `wyoming`, `cli`, `launchd`, `engines`,
  `ci`, `docs`). One logical change per commit.
- No AI attribution and no `Co-Authored-By` trailers in commits or pull
  requests.
- No emoji in code, docs, UI or commit messages.
- Nothing personal in the repo: no hostnames, IP addresses, usernames or
  tokens. Deployment settings belong in your own
  `~/.config/voiced/config.toml`, outside the repository. `.env` files are
  never read by the code and must never be committed.
- The engine interfaces in `src/voiced/engines/base.py`, the
  [audio conventions](docs/architecture.md#audio-conventions) and the
  `/health` schema are frozen. Propose a change in an issue before touching
  them.
- Non-obvious design choices get an ADR under `docs/decisions/` with the
  next free number, and a line in `docs/decisions/README.md`.
- Engines run every blocking model call through
  `voiced.threads.run_blocking`, or a `voiced.threads.ModelThread` when the
  engine keeps MLX streams, which belong to one thread, and transports call engines only inside
  `voiced.inference.inference()`; see
  [ADR 0005](docs/decisions/0005-shared-inference-and-audio-primitives.md).
- Runtime dependencies go in `[project] dependencies`; MLX-only packages in
  the `stt` or `tts` extras; test and lint tools in the `dev` group.
- Documentation that describes behaviour which has not landed yet carries an
  HTML comment `<!-- TODO(release): ... -->` on its own line. The canonical
  list of open items is
  `grep -rn 'TODO(release)' README.md CONTRIBUTING.md CHANGELOG.md docs`;
  resolve the item and delete the comment in the same change.

## Adding an engine

Engines live in their own packages and are selected by name in the config.
The walkthrough, with a complete example and the entry-point declaration, is
in [docs/architecture.md](docs/architecture.md#adding-an-engine). Engines
bundled with voiced follow the same path; the only difference is that their
entry points are declared in this repository's `pyproject.toml`.

## Pull requests

Fill in the template. A good description says what was built, how to try it
by hand, any interface questions, and the exact commands you ran with their
results. Keep pull requests to one topic; split refactors from features.

Reviews look for: tests that run without models, no new personal data, no
emoji, the frozen interfaces untouched, and docs updated when behaviour
changed (`README.md`, `docs/`, `config.example.toml`, `CHANGELOG.md`).

Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md) for anything
a user would notice.

## Releasing

Maintainers only. Bump `version` in `pyproject.toml` (`voiced.__version__` reads it from
the installed package metadata), set `PUBLISHED = True` in
`voiced.launchd.install` with the first release that goes to PyPI (until
then `voiced install` outside a checkout insists on `--source`), move the Unreleased section of the changelog under the new version with the
date, tag `v<version>`, and build with `uv build`. Then fill in `url` and
`sha256` in `packaging/homebrew/voiced.rb` for the tag and copy it to the tap
([packaging/homebrew/README.md](packaging/homebrew/README.md)).

<!-- TODO(release): verify the publish step once the package is on PyPI -->
