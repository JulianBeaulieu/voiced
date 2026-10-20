## What

One or two sentences on what this changes and why. Link the issue if there is one.

## How to test it by hand

Commands or click-paths a reviewer can follow.

## Interface questions

Anything that touches a frozen interface, the config schema, the `/health` schema or a port. Write "none" otherwise.

## Checks run

Paste the exact commands and their results.

```
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

## Checklist

- [ ] Tests pass without any model installed (the fake engines cover the new code)
- [ ] Tests that need models carry `@pytest.mark.model`
- [ ] No personal data: no hostnames, IPs, usernames, tokens
- [ ] No emoji; conventional commit messages and a conventional pull request title (it becomes the squash commit); no AI attribution trailers
- [ ] Docs updated where behaviour changed (`README.md`, `docs/`, `config.example.toml`)
- [ ] `CHANGELOG.md` has a line under Unreleased if a user would notice
- [ ] New non-obvious decisions have an ADR under `docs/decisions/`
