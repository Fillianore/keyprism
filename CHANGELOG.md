# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-09-21

### Added

- Phase 3 deep-learning separation behind a strictly optional `[dl]`
  extra (`uv sync --extra dl`: onnxruntime, basic-pitch, huggingface-hub;
  no PyTorch in our inference path): `keyprism.dlsep` runs htdemucs
  (4-stem) / htdemucs_6s (6-stem) ONNX exports chunked (10 s windows,
  1 s overlap) with a strictly-positive periodic-Hann overlap-add
  cross-fade normalized by the accumulated window sum — seam-free stems,
  exact edge reconstruction, RAM bounded by one chunk plus a singleton
  per-model ONNX session (never re-loaded per request). Results cache
  under the analysis entry as `stems/<DL_STEMS_VERSION>/<method>/`
  (atomic tmp-swap; model weights in `~/.keyprism/models/demucs/`,
  auto-downloaded, env-overridable repo/file)
- `keyprism.poly_transcribe`: Basic Pitch polyphonic transcription of a
  DL stem with mandatory same-pitch fragment merging (gaps < 50 ms
  collapse — frame predictors shred sustained chords into 50 ms bits),
  cached as `notes/<POLY_VERSION>/notes_poly_<stem>.json`
- Background-task HTTP API for long DL runs: `POST
  /api/stems?method=demucs_4|demucs_6` starts a single-worker-executor
  task and returns `{"task_id", "status_url"}`; `GET /api/task/{id}`
  polls status/progress/result (done payload matches the classic
  `/api/stems` JSON); `GET /api/stems?method=demucs_*` serves the cache
  (404 until computed); `/api/stem` serves DL stems too; `GET /api/notes
  ?track=piano|guitar|other&method=poly[&source=]` returns merged
  polyphonic notes; `GET /api/ping` now carries `capabilities`
  (`dl`/`poly`/`dl_methods`). Without the `[dl]` extra every DL endpoint
  answers 501 Not Implemented and nothing crashes (graceful degradation,
  locked by tests running green with AND without the extra)
- Frontend multi-lane workspace (`lanes.js`): stacked horizontal lanes
  (Mix + vocals/drums/bass/piano/guitar/other) with per-lane waveform
  canvases, volume/Mute/Solo GainNodes and PX toggles for polyphonic
  note overlays. All lanes ride the shared AudioContext transport (one
  absolute `source.start(when, offset)` timestamp — Phase 2 sync
  contract), notes are drawn on dedicated overlay canvases (never
  `layout.shapes`) synced to the main spectrogram's `xaxis.range` on
  every `plotly_relayout`, with a spatial index (sorted starts + binary
  search bounded by the longest note) so zoom/pan render only the
  visible window

### Changed

- `tests/test_server.py::test_ping` asserts the extended ping contract
  (`capabilities` flags)

## [0.4.1] - 2026-09-21

### Added

- Phase 2 classic source separation (training-free) over the Phase 0
  cached complex STFT: `keyprism.hpss` (median-filter HPSS with Wiener
  soft masks), `keyprism.rpca` (Robust PCA via inexact ALM/ADMM with a
  two-stage mu schedule, residual early stopping and scale-invariant
  solve, full-track decomposition built from 400-frame chunks with
  80-frame cross-faded overlap-add — never a whole-track SVD) and
  `keyprism.stems` (real masks applied to the complex STFT so the
  original phase is preserved, streaming ISTFT that emits only sample
  ranges with complete overlap-add contributors, mono PCM16 WAVs cached
  per entry as `stems/<version>/<method>/` with atomic swap and
  byte-deterministic recompute)
- New HTTP endpoints `GET /api/stems?method=hpss|rpca|combined`
  (`&progress=1` polls live computation progress) and
  `GET /api/stem?method=..&name=..` (WAV attachment download); `data.json`
  keeps `"stems": null` by contract — stem audio is served on demand only
- Frontend stem player: a Stems On/Off toggle in the top bar plus a panel
  (per-stem volume, Mute, Solo and a Mix row for the original track,
  method switch, live progress); all stems and the mix are scheduled on
  ONE shared AudioContext at ONE absolute timestamp
  (`source.start(when, offset)`) for sample-accurate multi-track sync,
  and gain changes ramp via `setTargetAtTime` so mute/solo are click-free
- player.js now schedules playback with a small absolute lead time and
  emits transport events (`play`/`pause`/`mixgain`) so companion engines
  can join the same clock

## [0.4.0] - 2026-09-21

### Added

- Phase 1 monophonic transcription (bass + lead presets): a preset-driven
  pipeline (`tracks` registry → loudness-weighted harmonic salience with
  subharmonic suppression → band-limited adaptive onsets → Viterbi
  single-pitch decoding → note events → MIDI export) reading the Phase 0
  cached complex STFT in memmap row chunks — the full complex matrix is
  never in RAM. Two cold runs are byte-identical (determinism locked by
  tests); adding an instrument requires only a new `MONO_TRACKS` entry
- New HTTP endpoints `GET /api/notes?track=bass|lead|both` and
  `GET /api/midi?track=...` (SMF type 0/1 via `mido`, the only new
  runtime dependency): per-track results cache under the analysis entry
  as `notes/<MONO_VERSION>/notes_<track>.json` (hit = file bytes served
  verbatim; version bump auto-invalidates); `data.json` keeps
  `"notes": null` by contract — note data is served on demand only
- Frontend note overlay: Off/Bass/Lead/Both segmented control (EN+zh),
  detected notes drawn as culled `layout.shapes` rectangles on the
  existing heatmap (re-culled on relayout through the shared 120 ms
  throttle, top-1500-by-confidence cap with a hint), and an Export MIDI
  download button
- `scripts/eval_trans.py`: manual transcription evaluation against
  reference MIDI (single pair or dataset directory), optional `mir_eval`
  via the new `eval` extra (never required by tests or runtime)
- Phase 0 analysis foundation: the complex-valued STFT now lives in a new
  `keyprism.transform` module (byte-identical numerics to the previous
  scipy-based path, locked by round-trip / naive-reference-equivalence /
  Parseval / byte-determinism tests) together with an ISTFT overlap-add
  inverse, legacy-semantics dB magnitude helper and a sub-bin parabolic
  peak refiner; `dsp.py` keeps its full public API as a thin façade
- Content-addressed analysis cache under
  `~/.keyprism/cache/analysis/<pcm16>/<params16>/`: repeated analysis of
  the same track and parameters reuses the stored full-resolution
  mix-channel complex STFT (`stft.npy`, complex64 memmap artifact
  chunk-filled in ≤2048-frame slices with per-chunk flush, plus
  `meta.json`) instead of recomputing it; LRU eviction capped by
  `KEYPRISM_CACHE_MAX_ENTRIES` (default 8)
- Staged analysis orchestration shared by CLI static mode and serve mode
  (decode → stft → aggregate → payload) with stage weights (stft 0.6 /
  aggregate 0.3 / payload 0.1) surfaced through a progress callback;
  console progress output is unchanged
- Payload contract reservations: `notes` and `stems` fields are always
  present and `null` in this release (reserved for note-level
  transcription and separated source stems in later phases)

## [0.3.3] - 2026-09-21

### Added

- Frontend interface preview screenshot in both READMEs (`assets/preview.png`,
  shown centered under the intro pipeline)

### Changed

- Resolution options trimmed to 15/30/60 columns per second (5 and 10
  removed); 15 remains the default

### Fixed

- Spectrogram scrolling at 60 cols/s × 10 subbands reworked: the trace now
  holds the complete pooled matrix — subband rows max-pooled to ~one row
  per screen pixel (peaks preserved) and columns capped for very long
  tracks (`frontend/src/specfeed.js`) — rendered once per data change with
  no viewport slicing or dynamic loading. Pan/zoom during a wheel gesture
  slides the trace layer via a transient SVG transform (the same trick as
  plotly's own drag pan), pixel-perfect seamless, committed with a single
  relayout after the gesture goes idle or on pointerdown; the playhead
  follows the same affine so it stays glued to the content. While that
  commit replot blocks the main thread on large matrices, a modal-style
  overlay in the progress-dialog visual language (lighter backdrop, no
  input blocking) shows a composited sliding bar that keeps moving through
  the block

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
