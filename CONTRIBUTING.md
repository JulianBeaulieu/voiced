# Contributing

Thanks for your interest. Bug reports, fixes, docs and new engines are all
welcome. Everyone taking part follows the
[code of conduct](CODE_OF_CONDUCT.md).

## How to contribute

1. Find an [issue](https://github.com/JulianBeaulieu/voiced/issues), or
   open one. Questions go in
   [Discussions](https://github.com/JulianBeaulieu/voiced/discussions). A
   small fix, such as a typo or an obvious bug, needs no issue.
2. Say in the issue that you are taking it, so nobody does the same work
   twice.
3. Fork the repository and branch from `main`.
4. Make one change per pull request. Split refactors from features.
5. Open the pull request and fill in the template: what was built, how to
   try it by hand, any interface questions, and the exact commands you ran
   with their results.

CI must be green before a pull request can merge; it runs the
[checks](#checks) below. The maintainer reviews every pull request and may
ask for changes. The review looks for tests that run without models, no
personal data, no emoji, nothing stable changed without an issue, and docs
updated where behaviour changed (`README.md`, `docs/`,
`config.example.toml`, `CHANGELOG.md`). Pull requests are squash merged,
and the title becomes the commit message, so it follows the
[commit format](#conventions).

voiced has one maintainer, who works on it part time. Expect a first reply
within a week, and longer at times. A friendly ping on a pull request that
has waited two weeks is welcome.

## What is stable

These are stable, and other projects may build on them:

- the engine interfaces in `src/voiced/engines/base.py`
- the [audio conventions](docs/architecture.md#audio-conventions)
- the config schema
- the HTTP and Wyoming API, including the `/health` schema
- the [conventions](#conventions) below

A change to one of them starts as an issue, then follows the conventions
(an ADR when the choice is not obvious, a changelog line). While the
version starts with 0, a minor release can still change them; the changelog
then says what changed and what to do.

## Development setup

Requirements: [uv](https://docs.astral.sh/uv/), Python 3.11 to 3.13 (uv
installs 3.12 from `.python-version` if needed). The MLX engines need an
Apple Silicon Mac; Kokoro uses the espeak-ng that the `tts` extra
installs (Homebrew's `espeak-ng` is an optional fallback). Everything else
runs anywhere.

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

To install your checkout as the running service, see
[Installing from a checkout](docs/operations.md#installing-from-a-checkout).
To try a change to the Homebrew formula from a local tap, see
[packaging/homebrew/README.md](packaging/homebrew/README.md).

## Checks

CI runs these four on a GitHub `macos-14` runner. Run them before you
open a pull request.

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

CI runs two more jobs on every pull request. `menubar` builds and tests
the Swift menu bar app on `macos-15` (`swift build -c release`,
`swift test`, `Scripts/bundle.sh`); for a Swift change, run
`cd menubar && swift test` first. `workflows` lints the workflows with
[actionlint](https://github.com/rhysd/actionlint) and
[zizmor](https://docs.zizmor.sh/) (`actionlint` and `uvx zizmor .github/`
locally). Every `uses:` is pinned to a full commit SHA with its version as a
trailing comment, and checkouts set `persist-credentials: false`. Dependabot
proposes updates to the pins weekly (`.github/dependabot.yml`); do not
replace a pin with a tag.

## Tests

The default `pytest` run must pass on a machine with no model, no GPU and no
MLX packages installed, because that is what CI is. ffmpeg comes with the
`imageio-ffmpeg` core dependency, so the ffmpeg tests run everywhere. Use `FakeSTTEngine` and
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
- Non-obvious design choices get an ADR under `docs/decisions/` with the
  next free number, and a line in `docs/decisions/README.md`.
- Anything a user would notice gets a line under **Unreleased** in
  [CHANGELOG.md](CHANGELOG.md).
- Engines run every blocking model call through
  `voiced.threads.run_blocking`, or a `voiced.threads.ModelThread` when the
  engine keeps MLX streams, which belong to one thread, and transports call engines only inside
  `voiced.inference.inference()`; see
  [ADR 0005](docs/decisions/0005-shared-inference-and-audio-primitives.md).
- Runtime dependencies go in `[project] dependencies`; MLX-only packages in
  the `stt` or `tts` extras; test and lint tools in the `dev` group. Then
  run `uv run python scripts/refresh_locks.py` (see
  [Updating a dependency](#updating-a-dependency)).
- Documentation that describes behaviour which has not landed yet carries an
  HTML comment `<!-- TODO(release): ... -->` on its own line. The canonical
  list of open items is
  `grep -rn 'TODO(release)' README.md CONTRIBUTING.md CHANGELOG.md docs`;
  resolve the item and delete the comment in the same change.

## Updating a dependency

`uv.lock` is the one source of the dependency versions. Three files are
generated from it and committed: the two locks every install path uses,
`src/voiced/requirements.lock` and `src/voiced/build-requirements.lock`
(ADR 0017, "One lock for all paths"), and `NOTICE`. Tests fail while any of
them says something other than `uv.lock`, and name this command, which
runs `uv lock` and rewrites all three (NOTICE asks PyPI for licences):

```sh
uv run python scripts/refresh_locks.py
```

Dependabot's pull requests for the `uv` ecosystem change only
`pyproject.toml` and `uv.lock`, so their CI fails until someone runs it.
To land one, check out its branch, run the command, commit and push:

```sh
gh pr checkout <number>
uv run python scripts/refresh_locks.py
git commit -am "chore(deps): regenerate the shipped locks and NOTICE"
git push
```

To update a package by hand instead, `uv lock --upgrade-package <name>`
(or edit its pin in `pyproject.toml`), then the same command. A new
package whose licence PyPI does not name as an SPDX expression stops
`scripts/notice.py`; read its source and add it to `LICENCE_OVERRIDES`
there.

## Adding an engine

Engines live in their own packages and are selected by name in the config.
The walkthrough, with a complete example and the entry-point declaration, is
in [docs/architecture.md](docs/architecture.md#adding-an-engine). Engines
bundled with voiced follow the same path; the only difference is that their
entry points are declared in this repository's `pyproject.toml`.

## Cutting a release

Maintainers only. `.github/workflows/release.yml` builds everything from the
tag. Signing needs "Setting it up, once" in
[SIGNING.md](menubar/Scripts/SIGNING.md) done first. Two of its steps are
required, not optional: the `release` environment with a required reviewer,
and the tag ruleset that lets only the owner create `v*` tags. Without them
anyone with write access could tag a commit and have it signed with the
owner's Developer ID.

1. Bump `version` in `pyproject.toml` (`voiced.__version__` reads it from
   the installed package metadata) and run `uv lock`.
2. In `CHANGELOG.md`, move the Unreleased entries under
   `## [<version>] - <date>` and leave an empty Unreleased section. That
   section is the release notes, and the app's "What's new".
3. Open a pull request with the two changes and merge it.
4. Tag the merge commit and push the tag:

   ```sh
   git switch main && git pull
   git tag -a v<version> -m "voiced <version>"
   git push origin v<version>
   ```

   The workflow fails early if the tag is not `v` plus the version in
   `pyproject.toml`, or the CHANGELOG has no section for it. Approve the
   `release` deployment when the run asks (Actions > the run > Review
   deployments).
5. Check the draft release it creates: the notes, the wheel, the sdist,
   `voiced-<version>-macos.zip` and `.dmg`, and `SHA256SUMS`. Assets named
   `-adhoc` mean the signing secrets were missing; the job summary says so.
   Download the zip and open the app once: Gatekeeper should not complain.
   To rebuild, delete the draft first; the workflow refuses a tag that
   already has a release.
6. Publish the draft.
7. Update the tap from the run's `homebrew` artifact, as
   [packaging/homebrew/README.md](packaging/homebrew/README.md) describes.

To try the workflow without releasing, start it by hand (Actions > release >
Run workflow). On a branch it builds the same files ad hoc; on a tag ref it
signs and notarizes them. Either way they are the run's `dist` and
`homebrew` artifacts, and no release is created.

<!-- TODO(release): verify the publish step once the package is on PyPI -->
