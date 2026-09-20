# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Automated release finalization: a `release.yml` workflow (triggered by a
  push to master) tags `v<version>` from `pyproject.toml`, publishes the
  GitHub Release from the matching CHANGELOG section and syncs master back
  into devel, keeping "devel behind master" at zero

### Fixed

- The release-finalize workflow now always syncs master back into devel;
  previously the "tag already exists" early exit skipped the sync entirely

## [0.3.0] - 2026-09-19

### Added

- Release process documentation: a dedicated section in `AGENTS.md`
  (`scripts/release.sh` usage, cut-off timing, solo-maintainer admin-merge
  conventions, CI check-name pitfalls) plus a developer "Releasing" note and
  `scripts/` tree entry in both `README.md` and `README_CN.md`

## [0.2.0] - 2026-09-18

### Added

- Audio upload endpoint `POST /api/upload?name=<file>`: browsers can pick a
  local music file, upload it as raw bytes, and the backend decodes, analyzes
  and hot-switches the current track (512 MB cap, libsndfile → PyAV/ffmpeg
  fallback covering mp3/wav/ogg/flac/m4a/aac/wma/opus/aiff and more).
- "Select music" button in the frontend top bar with upload progress dialog.
- Default audio input: running the backend without arguments loads
  `assets/demo.m4a`.
- One-command launch scripts under `scripts/` (`start.sh` for Linux/macOS,
  `start.cmd` for Windows) with graceful shutdown, readiness wait and log
  redirection to the workspace.
- User workspace `~/.keyprism` consolidating logs, matplotlib cache and upload
  staging; relocatable via `KEYPRISM_HOME` or `~/.keyprism/config.env`
  (env var > config file > built-in defaults).
- Regression test suite (pytest, 30 cases across DSP / audio IO / payload
  contract / HTTP server layers, self-generated fixtures) and GitHub Actions
  CI running backend tests plus frontend build on every push and pull request.
- Developer documentation: `AGENTS.md` (architecture, design rationale,
  maintenance rules, known pitfalls) and `docs/api.md` (HTTP API reference).

### Changed

- Backend restructured from a single `backend.py` into a `src/keyprism/`
  package with five single-responsibility modules (`cli` / `server` /
  `payload` / `audio_io` / `dsp`); behavior and CLI unchanged
  (`python -m keyprism`).
- Default ports changed to 9630 (API) and 5270 (frontend), replacing the
  legacy 8800/5180.
- Non-browser-safe audio formats are transcoded to PCM WAV inside the
  workspace instead of the system temp directory.
- TUNA PyPI mirror configured as the default uv index for constrained
  networks.

### Fixed

- Stale cached audio playback after switching tracks (cache-busting query on
  the audio URL).
- Files without an audio track now fail with an explicit error instead of an
  opaque decoder traceback.

## [0.1.0] - 2026-09-18

Initial public baseline: piano-key spectrogram player with analysis backend,
Vite frontend, three-channel semitone heatmap, Web Audio playback engine,
BPM estimation and measure grid.
