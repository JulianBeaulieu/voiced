# Changelog

All notable changes to voiced. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- Project skeleton: `src/voiced` package, `uv` based development setup,
  frozen engine interfaces (`STTEngine`, `STTStreamSession`, `TTSEngine`),
  entry-point engine registry, `Settings` with TOML plus `VOICED_*`
  environment overrides, bounded `InferenceQueue`, `HealthState` and the
  `/health` schema, FastAPI app factories with bearer authentication,
  `voiced serve` and `voiced config`, fake engines for tests, CI on
  `macos-14`.
- Wyoming listeners for Home Assistant: STT on 10300 and TTS on 10200 in the
  same process as the HTTP API, streaming synthesis, zeroconf discovery when
  bound to the network, `[server] zeroconf` config key.
- Documentation: README with quickstart, architecture diagram, benchmarks
  and troubleshooting; API reference; Home Assistant guide; architecture and
  operations guides; ADR index; contributing guide; issue and pull request
  templates; Dependabot; manual `model-tests` CI job.

### Changed

- CI uses `actions/checkout@v7` and `astral-sh/setup-uv@v10.3.0` and cancels
  superseded runs of the same branch.
