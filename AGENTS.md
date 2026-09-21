# AGENTS.md — KeyPrism Development & Long-Term Maintenance Guide

> Aimed at future developers and AI coding agents. End-user documentation lives
> in `README.md`; this file carries no usage instructions.
> Purpose: record **why things are designed this way**, **what must be done
> when changing them**, and **where the pitfalls are**, so intent doesn't get
> lost over time.

## Architecture Layers (read before changing code)

```
src/keyprism/cli.py       CLI entry (thin shell, only argparse and dispatch)
src/keyprism/server.py    HTTP service: ping / spec / notes / midi / upload
                          ← the only module coupled to stdlib httpd
src/keyprism/analyze.py   staged analysis orchestrator (decode→stft→aggregate→payload)
                          + content-addressed analysis disk cache
src/keyprism/transcribe.py notes orchestration: cached STFT → salience → onsets
                          → decode, per-track notes disk cache (+ MIDI export glue)
src/keyprism/tracks.py    monophonic preset registry (bass / lead) — pure data
src/keyprism/salience.py  loudness weighting + harmonic salience   ← zero IO
src/keyprism/onset.py     band-limited adaptive onset detection    ← zero IO
src/keyprism/decode.py    Viterbi single-pitch decoder + NoteEvent ← zero IO
src/keyprism/midi_io.py   note events → Standard MIDI File bytes   ← zero IO
src/keyprism/payload.py   frontend contract: single source of truth for data.json fields (incl. matplotlib envelope rendering)
src/keyprism/audio_io.py  decode chain (sndfile→PyAV fallback) + path constants (PUBLIC_DIR / KEYPRISM_HOME)
src/keyprism/transform.py complex STFT/ISTFT + dB magnitude + peak refine  ← zero IO, numpy arrays in and out
src/keyprism/dsp.py       pure algorithms: semitone aggregation / downsampling / BPM (+ stft_power façade)  ← zero IO, numpy arrays in and out
```

> src layout note: source lives in `src/keyprism/`, but the package name is
> still `keyprism` —
> imports are always `from keyprism.dsp import ...`, runs are always
> `python -m keyprism`,
> never write `from src.keyprism import ...`.

Dependencies flow one way: `cli → server → {analyze, transcribe} → payload →
dsp / transform / audio_io` and `transcribe → {tracks, salience, onset,
decode, midi_io, analyze}`. Reverse imports and circular dependencies are
forbidden.

**Layering iron rules**:
- `dsp.py` and `transform.py` never do IO (no file reads, no print, no
  network) — that is the prerequisite for testing them in isolation
- `data.json` fields may only be defined in one place, `payload.py`; the
  frontend `frontend/src/main.js` is a consumer
- If the web framework is ever swapped (FastAPI/WebSocket etc.), only
  `server.py` should be thrown away and rewritten; the other layers
  survive untouched — that is the core reason for the split

## Design Intent Archive (why it is the way it is)

| Decision | Rationale |
|------|------|
| Five-module split (rather than one file) | The seams are framework-agnostic; a closure-style single-file Handler cannot be tested without a live process |
| `server.py` exposes `make_server()` (non-blocking start) | Tests can start a real service on an ephemeral port and then shut it down; `run_server()` is the blocking entry |
| `server.py` references `audio_io.PUBLIC_DIR` at runtime (module attribute, not a from-import value binding) | Tests can fully isolate the output directory by monkeypatching one spot |
| Decode chain sndfile → PyAV fallback | libsndfile covers wav/flac/ogg/mp3; PyAV (ffmpeg) backstops m4a/aac/wma/opus/aiff etc. |
| Anything not mp3/wav/ogg/flac is always transcoded to PCM WAV | Guarantees the browser's `decodeAudioData` can decode it 100% of the time; compatibility beats disk usage |
| Workspace `~/.keyprism` (logs/cache/uploads) | Logs and caches never pollute the project directory or system temp dirs; the `KEYPRISM_HOME` env var relocates the whole thing |
| Existence of `tests/` | Implements no product features; it is a regression safety net. A red contract test means **the mechanism is working**, not that tests are outdated |
| CI enforces pytest + build | Prevents "red and nobody cares" from decaying into a skip dump — test rot is intercepted before merge |
| pyproject defaults to the TUNA mirror | The official PyPI is unreachable from the dev network; GitHub runners reach TUNA just fine |
| Ports 9630/5270 (old 8800/5180 deprecated) | Explicitly switched to new ports to avoid silent confusion with old processes/bookmarks |

## Modification Rules (pre-merge checklist)

1. **Changed any code under `src/keyprism/`** → `uv run pytest -q` must be
   fully green (about 6 seconds; no excuse to skip)
2. **Changed the data.json contract** (added/removed/changed fields) → sync
   three places: `payload.py`, `tests/test_payload.py`,
   `frontend/src/main.js`. The contract test's field-by-field assertions
   exist exactly for this
3. **Changed the HTTP API** (routes/params/error codes) → sync
   `tests/test_server.py`
4. **Tests are outdated** → deleting them in bulk and rewriting against the
   new contract is allowed; **forbidden** to skip, xfail, or comment out to
   "make CI pass"
5. **Principle for tests accompanying new features**: few but critical —
   invariants (shape/energy/field presence), contracts, error paths; do not
   chase coverage numbers
6. **During big refactors**: get a green baseline first → move in small steps
   → stay green at every step. `tests/helpers.py` self-generates all fixtures
   (wav/m4a/120 BPM click track) and depends on neither demo.m4a nor the
   external environment

## Release Process (devel → master is a release)

Merging devel into master **is** the release. The version cut-off is scripted:
`scripts/release.sh` bumps the version (`pyproject.toml` + `uv.lock` via
`uv version`), finalizes the CHANGELOG (`[Unreleased]` → `[X.Y.Z] - date`,
with a fresh empty `[Unreleased]` prepended), commits, pushes devel and opens
the release PR. Full usage lives in the script header.

```bash
scripts/release.sh --bump minor --dry-run   # preview, zero side effects
scripts/release.sh --bump minor             # real cut + release PR
gh pr merge <N> --merge --admin             # solo maintainer: admin merge
```

Everything after the merge is automated by
`.github/workflows/release.yml` (fires on push to master): it tags
`v<version>` from `pyproject.toml`, creates the GitHub Release from the
matching CHANGELOG section, and merges master back into devel so the
"devel behind master" counter stays at zero. The job is idempotent — if the
tag already exists it exits without doing anything.

Ground rules:

- Keep `[Unreleased]` growing as features land; the version number is decided
  once per cut (SemVer over the whole batch), never per feature
- Cut during a quiet window (no in-flight PRs on devel) and merge the release
  PR promptly so the batch cannot grow underneath it
- The script refuses: wrong branch, dirty tree, out-of-sync devel, offline
  (real mode), version equal to the current one, or an empty `[Unreleased]`
- Solo-maintainer note: master requires 1 approval and you cannot approve
  your own PR — merging with `--admin` (bypass) is the designed path until
  collaborators arrive
- Pitfall learned the hard way: required status checks in the `protect-master`
  ruleset match check names **by exact string**. Renaming CI jobs orphans the
  old entries (PR stuck on "waiting for status" forever) — update the ruleset
  in the same change
- Pitfall learned the hard way: the devel → master release PR must be merged
  with **"Create a merge commit"**, never squash. A squash drops devel's
  commit lineage from master, the merge base never advances and every
  subsequent release PR re-conflicts on the same doc/CHANGELOG regions
  (squash is fine for feature → devel PRs)

## Analysis Cache & Memmap Discipline (Phase 0+)

The complex STFT is the internal source of truth for future analysis
features (source separation, transcription). Since Phase 0 it is cached
under `$KEYPRISM_HOME/cache/analysis/<pcm16>/<params16>/`:

- **Key scheme**: `pcm16` = sha256[:16] of the decoded segment's canonical
  PCM16 bytes (row-interleaved int16 of clipped floats); `params16` =
  sha256[:16] of canonical JSON of `{sr, win, hop, window, center, rate,
  sub, db_range, start, end}`. Note `rate`/`sub` do not influence the STFT
  itself but are part of the key — each resolution therefore gets its own
  entry. That duplication is deliberate (cheap disk, simple invalidation);
  do not "deduplicate" it without revisiting the contract.
- **Artifacts**: `stft.npy` — complex64, shape `(n_frames, win//2+1)`,
  time-major so one frame = one contiguous row (memmap friendly) — plus
  `meta.json` (params, sr/win/hop, n_frames/n_bins, duration, and the
  derived `bpm`/`beat_offset_sec`; Python float → JSON → float round-trips
  exactly, which is what makes cache hits bit-reproducible).
- **Never hold the full complex STFT in RAM.** Writes go through
  `np.lib.format.open_memmap` in row chunks of at most 2048 frames
  (`STORE_CHUNK_FRAMES = 512`), flushed per chunk, atomic rename into
  place; reads go through `np.load(..., mmap_mode="r")` row slices
  (`analyze.iter_chunks`). Float64 *power* matrices of legacy size are
  fine — the ban targets complex matrices (2–4× the bytes).
- **complex64 is lossy vs the float64 core.** Anything feeding the payload
  quantization must derive from the float64 core (the orchestrator squares
  the pre-cast float64 chunks). The cached complex64 artifact exists for
  future phases, not for byte-identical payload replay.
- **Eviction**: LRU by `stft.npy` mtime, cap `KEYPRISM_CACHE_MAX_ENTRIES`
  (default 8; usual env > config.env > default precedence — config.env is
  materialized into the environment by the launch scripts).
- **Test seam**: `analyze.cache_root()` reads `audio_io.KEYPRISM_HOME` at
  call time (module attribute, never a from-import value) — same pattern
  the server tests rely on for `PUBLIC_DIR`.
- **Numerics contract**: `transform.py` reproduces the legacy
  `scipy.signal.stft` conventions bit for bit (periodic hann, center
  `win//2` zero padding on both sides, tail padding, divide by
  `win.sum()`, hop = `win - win*3//4`); `tests/test_transform.py` locks
  this against a naive reference STFT written in the test file. Do not
  "optimize" the framing math without re-locking those tests.

## Monophonic Transcription: Presets & Extension Rules (Phase 1+)

`tracks.py` is the single registry driving the whole transcription
pipeline (`salience` / `onset` / `decode` / `transcribe` / `midi_io` are
parameterized exclusively by a `TrackPreset`). **Adding an instrument MUST
require only a new registry entry** — if you catch yourself writing an
`if track == ...` branch inside an algorithm module, extend the preset
schema instead. Frontend exception: `frontend/src/notes.js` mirrors preset
colors (`TRACK_COLORS`) because the frontend never reads Python data —
keep the two in sync when a color changes.

- **Cache invalidation**: per-track results live at
  `<analysis entry>/notes/<MONO_VERSION>/notes_<track>.json`. Bump
  `MONO_VERSION` in `tracks.py` whenever preset values, salience / onset /
  decode numerics or the note JSON schema change in a way that must
  invalidate previously cached files — stale results are then never
  served and recompute automatically. Do not "just delete the cache"
  instead; other installs have no you to do it.
- **Chunk discipline**: the full complex STFT is never in RAM (Phase 0
  rule). `transcribe` streams `stft.npy` row chunks (≤512 frames) through
  `np.load(..., mmap_mode="r")` views via `analyze.load_entry`, carrying
  only the previous chunk's last magnitude row into the onset flux. The
  intermediate float32 salience matrix (`n_frames × n_pitches`) is fine —
  it is not the complex STFT and is orders of magnitude smaller.
- **Determinism contract**: salience is a pure per-frame-slice function
  (chunk boundaries cannot change any value — locked by a chunk-invariance
  test); the two-pass global min-max normalization uses exact min/max
  (commutative) and element-wise arithmetic, so results are
  chunk-order-independent; Viterbi ties resolve to the lowest state index.
  Two cold runs must produce byte-identical notes JSON (locked by test).
- **Frame-rate invariance**: Viterbi transition weights are per-second
  rates multiplied by the physical frame spacing `hop_sec` — do not
  "simplify" them back to per-frame constants or behavior drifts when
  sr/win changes.
- **On-demand contract**: `data.json` keeps `"notes": null` (owned by
  `payload.py`, unchanged); note data is served only through
  `/api/notes` and `/api/midi`, so the heavy payload and its contract
  test stay untouched.
- **Loudness curve**: full A-weighting spans ~18 dB over a preset's four
  octaves, which one global linear salience normalization plus a fixed
  REST emission cannot cover — `salience.py` compresses the dB response
  to 1/4 (documented there). Revisit only together with the REST balance,
  not as a lone tweak.

## Known Boundaries & Pitfalls (must read before changing)

- `PUBLIC_DIR`/`DEMO_AUDIO` locate the repo root via `_repo_root()` (walks up
  looking for `pyproject.toml`). This holds under uv's default editable
  install; a regular install deployment would break the paths
- The `MPLCONFIGDIR` setting at the top of `payload.py` **must** come before
  `import matplotlib` — order-sensitive, do not reorder those imports
- The frontend audio URL carries a `?t=` cache-busting parameter (`main.js`):
  after an upload switches tracks, a same-named `audio.wav` in the browser
  cache is untrustworthy — do not "clean it up"
- Vite 7 requires **Node ≥ 20.19**; this repo's Windows script
  `scripts/start.cmd` has never been executed on real Windows (syntax review
  only), so verify it on first real use
- `uv.lock` and `frontend/package-lock.json` are both committed; install deps
  with `uv sync` / `npm ci`, never let versions drift with `npm install`

## Common Commands

```bash
uv sync                        # install/sync Python deps (incl. dev test group)
uv run pytest -q               # regression tests (30 cases, ~6s)
uv run python -m keyprism --serve 9630   # start the backend manually (loads assets/demo.m4a by default)
bash scripts/start.sh          # one-command start of both ends (ports/workspace see ~/.keyprism/config.env)
scripts/release.sh --bump minor --dry-run  # preview the next release cut
cd frontend && npm ci && npm run build   # frontend deps and build
```


## One Sentence for Future Maintainers

Tests are living documentation that evolves with the code, not an artifact
written once. When features change substantially the contract tests will turn
red — that is them forcing you to consciously sync both the frontend and
backend sides and update them (about 5-15 minutes per iteration). Do not
route around them.
