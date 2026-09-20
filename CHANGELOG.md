# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.2] - 2026-09-20

### Added

- Bilingual UI (English / 中文) with an EN/中文 toggle in the top bar:
  English is the default and the choice is remembered across sessions via
  `localStorage` (live switch, no reload)
- Hover tooltip now shows the hovered cell's note name, annotated with the
  subband index when sub > 1 (e.g. `C4 (2/5)`, low → high frequency)
- Seek and volume sliders fill the played/applied portion left of the thumb
  in the theme's champagne gold

### Changed

- Wheel over the spectrogram now scrubs playback and pans the visible
  window along with it (wheel down = forward, wheel up = backward);
  horizontal time-axis zoom requires holding Ctrl while scrolling
- Player seek bar no longer sits mid-track on first load: the thumb now
  starts (and stays, while paused) at the actual playback position

### Removed

- Bottom overview navigation bar (redundant with plot pan/zoom and the
  player seek bar)

### Fixed

- Resolution/subband switching crashed with "sub is not defined" before the
  request was sent (i18n refactor renamed a template variable); the message
  placeholder now receives the actual subband value
- Keyboard strip pattern was rotated three semitones (row 0 is A0, not C):
  C/F/G were drawn as black keys while C#/F#/G# looked white, and the C
  axis ticks landed on D#-position keys. Pitch class is now derived from
  the A0 base; white-key group separators sit under the black keys'
  centers (E|F and B|C stay on the direct row boundary)
- Black keys span their full semitone row (same width as white keys) while
  keeping the real-piano 62% horizontal length
- Hover placeholder (`%{text}`) now resolves: the per-cell label array must
  match the heatmap's full z shape ([row][col]), not one label per row

## [0.3.1] - 2026-09-20

### Added

- Automated release finalization: a `release.yml` workflow (triggered by a
  push to master) tags `v<version>` from `pyproject.toml`, publishes the
  GitHub Release from the matching CHANGELOG section and syncs master back
  into devel, keeping "devel behind master" at zero

### Fixed

- The release-finalize workflow now always syncs master back into devel;
  previously the "tag already exists" early exit skipped the sync entirely
- The release-finalize workflow now sets a git identity on the runner
  (annotated tag creation failed without one)
- Release PRs must merge with a merge commit: squashing devel → master drops
  devel's commit lineage and re-creates the same doc conflicts on every
  release (now stated in the PR body and AGENTS.md)

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
