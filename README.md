# KeyPrism

<div align="center">

**English** | [简体中文](README_CN.md)

</div>

**KeyPrism** — piano-key spectrogram player.
A spectral prism that resolves audio onto a piano semitone grid: time on the
horizontal axis, 88 keys on the vertical axis (subdividable down to 1/10
semitone), color as energy intensity, with synchronized playback and a full
suite of interactive analysis tools.

```
audio → STFT → semitone aggregation → interactive heatmap + player
```

## Feature Overview

### Spectral Analysis
- **Piano-pitch heatmap**: time on the horizontal axis (adaptive scale, one
  tick per second for windows within 16s), 88 semitones on the vertical axis
- **Stereo, three channels**: mix / left / right spectra, unified peak
  normalization (dB comparable across channels)
- **Switchable resolution**: time-column density 5/10/15/30 columns per second
  × semitone subdivision 1/5/10 subbands, recomputed live by the backend
  (serve mode required) with a progress dialog and staged progress bar
- **Subband widening**: in high-subdivision modes, energy is widened along the
  frequency axis with a triangular kernel to avoid visual fragmentation
- **BPM detection**: spectral-flux autocorrelation estimates BPM and first-beat
  offset, with a measure grid overlay (BPM / offset / time signature can be
  corrected manually)
- **Online track picking**: the "Select music" button in the top bar picks a
  local audio file, uploads it to the backend for analysis and switches the
  whole page (serve mode required)
- **Audio formats**: mp3/wav/ogg/flac read directly; other common containers
  such as m4a/aac/wma/opus/aiff are decoded via PyAV (ffmpeg) as a fallback
  when sndfile cannot handle them, and automatically transcoded to a
  fully browser-compatible PCM WAV for playback; files without an audio track
  produce a clear error

### Interaction & Playback
- **Web Audio playback engine**: the whole PCM track is pre-decoded, seeking
  is instant and stutter-free, with a sample-accurate clock
- **Playback cursor**: rendered as an HTML overlay with `transform` for smooth
  full-frame motion; aligned with "what the ear hears" (output latency
  compensated) and waits for buffering before starting
- **Click anywhere on the spectrogram** to seek playback (no autoplay)
- **Follow playback**: toggleable auto-panning viewport
- **Navigation bar**: full-track energy envelope as backdrop, draggable window
  (constant width) / handles to resize / click to jump
- **Color controls**: 6 palettes (lower anchor pinned to pure black, silence
  sinks into the background) + color floor slider + highlight γ power
  transform (nonlinear mapping emphasizing current leading pitches), all with
  click-to-type values and one-click reset

### Misc
- Pitch range clipping at both ends (keyboard and scale adapt automatically)
- Volume capped at −10 dB (noise protection), volume persistence
- Dark console-style UI

## Repository Layout

```
keyprism/
├── src/                   # backend source (src layout, package name keyprism)
│   └── keyprism/          # backend Python package (all backend code lives here)
│       ├── __main__.py    # python -m keyprism entry point
│       ├── cli.py         # CLI argument parsing and dispatch
│       ├── dsp.py         # pure algorithms: STFT / semitone aggregation / downsampling / BPM
│       ├── audio_io.py    # decode chain: sndfile→PyAV fallback / browser transcoding / path constants
│       ├── payload.py     # frontend contract: single source of truth for data.json fields
│       └── server.py      # HTTP service: /api/ping /api/spec /api/upload
├── pyproject.toml         # uv project definition (deps locked in uv.lock, TUNA index by default)
├── scripts/               # launch & release scripts (config & ports below)
│   ├── start.sh           # one-command start, Linux / macOS
│   ├── start.cmd          # one-command start, Windows
│   └── release.sh         # version cut-off: bump + changelog + release PR
├── tests/                 # pytest regression suite: one layer each (see below)
├── assets/                # static assets such as the demo audio
│   └── demo.m4a
├── docs/                  # developer docs (HTTP API reference etc.)
│   └── api.md
└── frontend/              # Vite frontend (vanilla JS + plotly.js)
    ├── index.html
    ├── vite.config.js
    ├── public/            # backend output: data.json + audio.*
    └── src/
        ├── main.js        # entry: data loading and control wiring
        ├── spectrogram.js # heatmap + keyboard + measure grid + layout
        ├── ticks.js       # adaptive ticks + time-range clamping
        ├── navbar.js      # bottom navigation bar
        ├── player.js      # Web Audio playback engine
        └── style.css
```

## Testing

`tests/` implements no product features; it is an automated regression suite.
After changing code, run `uv run pytest -q`: 30 cases covering DSP algorithms
(including a deterministic 120 BPM click-track case), the decode-chain
fallback (real m4a encoding), the payload contract (field-by-field assertions
the frontend depends on), and all HTTP routes (upload switching / error codes
/ CORS preflight). It is the safety net for refactoring and new features —
keep it.

CI (`.github/workflows/ci.yml`) runs the same flow automatically on every
push / PR: backend `uv sync + pytest` + launch-script syntax check, frontend
`npm ci + build`, about 2 minutes in total. Stale tests are blocked outright —
either fix the code or consciously update the tests.

For architecture intent, maintenance rules and known pitfalls aimed at
developers, see `AGENTS.md` (also read automatically by AI coding agents).

### Releasing (developers)

Merging devel into master **is** a release. The cut is scripted:
`scripts/release.sh --bump minor` bumps the version (`pyproject.toml` +
`uv.lock`), finalizes the CHANGELOG and opens the release PR; after merging,
tag master (`git tag -a vX.Y.Z -m "KeyPrism X.Y.Z" && git push --tags`).
Full rules and hard-learned pitfalls (solo `--admin` merge, exact-string
matching of required CI check names) live in `AGENTS.md`.

## Workspace (`~/.keyprism`)

Logs, caches and upload staging are consolidated into a user workspace so they
never pollute the project directory or system temp directories; it can be
relocated wholesale via the `KEYPRISM_HOME` environment variable (`~` prefix
supported):

```
~/.keyprism/
├── logs/               # backend.log / vite.log (redirected by launch scripts)
├── cache/matplotlib/   # matplotlib font and config cache
├── uploads/            # staging for audio uploaded via "Select music" (only the latest few are kept)
└── config.env          # optional persistent config: KEY=VALUE, lines starting with # are comments
```

Config precedence: **environment variables > `config.env` > built-in
defaults** (`config.env` only recognizes keys prefixed with `KEYPRISM_`, and
never overrides already-exported environment variables):

| Variable | Description | Default |
|------|------|------|
| `KEYPRISM_HOME` | workspace root | `~/.keyprism` |
| `KEYPRISM_LOG_DIR` | log directory | `$KEYPRISM_HOME/logs` |
| `KEYPRISM_API_HOST` | backend listen address (use `0.0.0.0` for LAN access) | `127.0.0.1` |
| `KEYPRISM_API_PORT` | backend API port | `9630` |
| `KEYPRISM_FRONTEND_PORT` | frontend page port | `5270` |

## Requirements

- **Python 3.10+**, environment managed with [uv](https://docs.astral.sh/uv/)
  (creates the venv and locks dependencies automatically)
- **Node.js 20.19+** (for the Vite 7 frontend; `npm run dev` will not start on
  lower versions)

## Quick Start

```bash
# 1) One-command launch (first run does uv sync + npm install automatically;
#    without arguments it loads assets/demo.m4a by default)
#    Windows:
scripts\start.cmd
#    Linux / macOS:
./scripts/start.sh [audio file]

# 2) Open http://localhost:5270 in a browser
#    In the VS Code integrated terminal, both ports appear in the Ports panel
```

The script starts both services (Ctrl+C stops everything; on Windows close
the minimized windows):
- Backend API `http://localhost:9630` (analysis service, supports hot
  resolution switching / audio upload)
- Frontend page `http://localhost:5270`

Ports and the workspace can be changed via environment variables or
`~/.keyprism/config.env` (see the section above).

### Manual Step-by-Step

```bash
# Backend: uv creates the environment and installs deps automatically
# (assets/demo.m4a is the default when no audio is given)
uv run python -m keyprism --serve 9630

# Frontend
cd frontend
npm install
npm run dev -- --port 5270 --strictPort
```

Static mode (no long-running service; resolution fixed at launch options):

```bash
uv run python -m keyprism song.mp3 --rate 15 --sub 1
```

## Backend Options

```
uv run python -m keyprism [audio] [--serve PORT] [--rate R] [--sub S]
                              [--start sec] [--end sec]
                              [--window 8192] [--db-range 70]
```

| Option | Description | Default |
|------|------|------|
| `audio` | input audio file; `assets/demo.m4a` is loaded when omitted | `assets/demo.m4a` |
| `--serve PORT` | long-running API mode; the frontend can hot-switch resolution / upload audio | off |
| `--host` | serve-mode listen address (use `0.0.0.0` for LAN access) | `127.0.0.1` |
| `--rate` | time-column density (columns/second), one of 5/10/15/30 | 15 |
| `--sub` | subbands per semitone, one of 1/5/10 | 1 |
| `--start` / `--end` | analyze only a segment of the audio | whole track |
| `--window` | STFT window size (samples, power of two) | 8192 |
| `--db-range` | dynamic range in dB | 70 |

## HTTP API (serve mode)

| Endpoint | Description |
|------|------|
| `GET /api/ping` | health check |
| `GET /api/spec?rate=15&sub=5` | recompute the spectrum at the given resolution (three channels + envelopes), cached |
| `POST /api/upload?name=song.mp3` | upload a local audio file (request body is raw file bytes); the backend analyzes it, switches the current track and refreshes `data.json`; returns the full payload |

For the full reference (error codes / response structure / preflight) see
`docs/api.md`.
Upload example: `curl -X POST --data-binary @song.m4a 'http://localhost:9630/api/upload?name=song.m4a'`;
size cap 512 MB; the decode chain is libsndfile → PyAV (ffmpeg) fallback,
covering mainstream audio formats.

## Frontend Controls Cheat Sheet

| Action | Effect |
|------|------|
| ♪ Select music | opens a file picker; a local audio file is uploaded, analyzed by the backend and the page refreshes automatically (serve mode required) |
| Scroll wheel | zoom the time axis (pitch axis locked) |
| Drag the spectrogram | pan (clamped at both ends; cannot drag past the track) |
| Double click | restore the full-track view |
| Click the spectrogram | seek playback (no autoplay) |
| Bottom navigation bar | drag the window to pan / drag handles to resize / click empty space to jump |
| Top bar | resolution, channel, pitch range, BPM, offset, time signature, palette, color floor, highlight γ |
| Click a numeric label | type a value directly (Enter commits / Esc cancels), ↺ restores the default |

## Technical Notes

- **Time mapping**: after downsampling each column maps to the center of its
  time block (`hop × block`); the frontend calibrates against the full-track
  duration evenly, eliminating drift from inconsistent columns/second
- **Playback sync**: `AudioContext` scheduling + `outputLatency` compensation
  + buffer wait at start; the cursor is a DOM overlay and never triggers
  plotly reflows
- **Performance**: heatmap restyle throttling (leading+trailing 120ms);
  high-resolution decoding is chunked and asynchronous, keeping only the
  current channel's float matrix resident
- **Color floor anchor pinned to pure black**: only the first anchor color
  changes; transitions between anchors remain linear, orthogonal to the γ
  transform
- **Online track-picking pipeline**: the frontend XHR-uploads raw file bytes
  (with an upload progress bar); the backend streams to a staging directory →
  decodes and analyzes → atomically switches the server-side track state and
  clears the resolution cache; the audio URL carries a cache-busting
  parameter so a stale browser-cached file is never read after switching
  tracks

## License

MIT
