# AGENTS.md — KeyPrism Development & Long-Term Maintenance Guide

> Aimed at future developers and AI coding agents. End-user documentation lives
> in `README.md`; this file carries no usage instructions.
> Purpose: record **why things are designed this way**, **what must be done
> when changing them**, and **where the pitfalls are**, so intent doesn't get
> lost over time.

## Architecture Layers (read before changing code)

```
src/keyprism/cli.py       CLI entry (thin shell, only argparse and dispatch)
src/keyprism/server.py    HTTP service: ping / spec / notes / midi / stems /
                          stem_spec / upload
                          ← the only module coupled to stdlib httpd
src/keyprism/analyze.py   staged analysis orchestrator (decode→stft→aggregate→payload)
                          + content-addressed analysis disk cache
src/keyprism/transcribe.py notes orchestration: cached STFT → salience → onsets
                          → decode, per-track notes disk cache (+ MIDI export glue)
src/keyprism/stems.py     stem orchestration: cached STFT → HPSS/RPCA masks →
                          masked complex STFT → streaming ISTFT, per-entry stem
                          disk cache (Phase 2)
src/keyprism/dlsep.py     Demucs ONNX chunked separation + DL stem disk cache
                          (optional [dl] extra, guarded imports) (Phase 3)
src/keyprism/poly_transcribe.py  Basic Pitch polyphonic notes + fragment
                          merging, per-stem notes cache (optional [dl]) (Phase 3)
src/keyprism/hpss.py      median-filter HPSS Wiener masks          ← zero IO
src/keyprism/rpca.py      chunked inexact-ALM (ADMM) RPCA          ← zero IO
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

Dependencies flow one way: `cli → server → {analyze, transcribe, stems,
dlsep, poly_transcribe} → payload → dsp / transform / audio_io` and
`transcribe → {tracks, salience, onset, decode, midi_io, analyze}`,
`stems → {hpss, rpca, transform, analyze}`, `poly_transcribe → dlsep`,
`dlsep → analyze/audio_io`. Reverse imports and circular dependencies are
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
   fully green (about 12 seconds; no excuse to skip)
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

## Classic Source Separation: Stems (Phase 2+)

`stems.py` orchestrates separation over the Phase 0 cached complex STFT;
`hpss.py` / `rpca.py` are pure mask factories (zero IO, same discipline
as `dsp` / `transform`). Decisions that are easy to "simplify" into
regressions:

- **Masks multiply the COMPLEX STFT.** `X_stem = mask · X_complex` keeps
  the original phase — masking the magnitude and inventing a phase would
  smear transients and destroy stereo feel. The mask is real-valued Wiener
  (Fitzgerald) or low-rank/sparse ratio; never re-render phase.
- **RPCA chunking is not optional.** A whole-track SVD is OOM-prone and
  pointless: `rpca.iter_rpca_blocks` processes 400-frame chunks with
  80-frame overlap and linear cross-fade (complementary ramps summing to
  1) between chunks. Hard `np.concatenate` of chunk solutions leaves
  energy steps at seams → clicks after ISTFT. `rpca_full_track` exists
  only as the tests'/small-inputs reference.
- **Two-stage mu schedule (ADMM).** Blind geometric mu growth shrinks the
  SVT/soft thresholds towards zero, silently cancelling BOTH regularizers:
  the split then satisfies `L+S=X` exactly while `L` absorbs `S`
  (measured: sparse error 0.54 vs 1e-14 with the schedule). `mu` grows
  only while `||X-L-S||/||X|| > freeze_tol`, then freezes so the
  regularizers keep forcing rank/sparsity. The solve is scale-invariant by
  construction (input normalized by its spectral norm) because raw STFT
  magnitudes are ~1e-4..1e-1 and absolute thresholds would zero them.
  Default `tol=5e-4` is deliberate: derived masks shift by ~1e-2
  (≈0.1 dB, inaudible) while the SVD count drops ~3x — tighten it only if
  the split itself (not its masks) becomes the product.
- **Streaming ISTFT emission is sample-exact.** Per work block the masked
  complex rows of the previous `win//hop` frames are kept as a tail; a
  block emits exactly the samples `[a·hop − win//2, b·hop − win//2)`
  whose overlap-add contributors are all inside the buffer — the
  concatenation equals a hypothetical full-matrix ISTFT bit for bit
  (locked by test against the full reference, tolerance = PCM16
  quantization). Do not replace this with per-chunk ISTFT + concatenate.
- **Chunk-boundary exactness for HPSS.** `medfilt2d` reaches
  `Kt//2` frames across boundaries, so each block loads real magnitude
  rows of context from the memmap and keeps only the interior — streamed
  masks are bit-equal to a full-track call (locked by test). The block
  edges use zero padding exactly like a full-track medfilt would.
- **Cache invalidation**: stems live at
  `<analysis entry>/stems/<STEMS_VERSION>/<method>/<stem>.wav` +
  `status.json`. Bump `STEMS_VERSION` in `stems.py` when masks, the
  fusion, the ISTFT emission or the WAV layout change in a way that must
  invalidate old files (same rule as `MONO_VERSION`). Writes go to a
  `.tmp-<pid>` directory with an atomic `os.replace` so a crashed compute
  never leaves half-written stems.
- **On-demand contract**: `data.json` keeps `"stems": null` (owned by
  `payload.py`, unchanged); audio is served only through `/api/stems` +
  `/api/stem` — the endpoint streams WAV bytes directly from the entry
  cache (no copy into `frontend/public`, so static-mode builds and cache
  eviction stay consistent). `/api/stems?...&progress=1` is a
  non-blocking poll of the in-memory progress registry; the blocking call
  must be issued in parallel (ThreadingHTTPServer handles it).
- **Frontend sync contract** (`frontend/src/stems.js` + `player.js`):
  everything plays through the player's ONE shared AudioContext, and the
   transport emits `('play', {offset, when})` with a single absolute
   timestamp `when = ctx.currentTime + START_LEAD`; every
   AudioBufferSourceNode — mix and stems — calls `source.start(when,
   offset)` with that same `when`. Never serialize starts, never use
   setTimeout for scheduling. If you change `START_LEAD` in one file,
   change it in both. Mute/solo/volume only ramp GainNodes
   (`setTargetAtTime`), which is click-free; stopping sources is allowed
   to be abrupt (matches the mix transport).

## DL Separation & Multi-Track Workspace (Phase 3+)

`dlsep.py` (Demucs ONNX) and `poly_transcribe.py` (Basic Pitch) are
STRICTLY OPTIONAL `[dl]` features: both heavy imports are try/except
guarded, availability is exposed as module-level flags
(`dlsep.ORT_AVAILABLE`, `poly_transcribe.BP_AVAILABLE`), and `server.py`
maps them to module flags `DL_AVAILABLE`/`POLY_AVAILABLE` that DL
endpoints check before anything else — a missing extra answers **501
Not Implemented**, never a 500 or a crash. The tests stub the models
(`infer=` injection in `dlsep`, `predict_fn=` in `poly_transcribe`) so
`uv run pytest -q` is green in BOTH environments; keep it that way.

Decisions that are easy to "simplify" into regressions:

- **Hann OLA blending is exact, not decorative.** Chunks of exactly
  `chunk_sec` (final one pulled back to the track end, prior chunk kept
  so every seam keeps the nominal overlap) are blended with the PERIODIC
  Hann at sample centers (`0.5 - 0.5*cos(2π(i+0.5)/L)`,
  `dlsep.hann_cola`): strictly positive (single-coverage regions at the
  track head/tail divide out to exactly the covering chunk — a zero-
  endpoint window would produce 0/0 at sample 0) and 50%-overlap COLA
  (`w[i] + w[i+L/2] == 1`). Output is `Σ w·y_k / Σ w` — a convex
  combination, so `out = c1*fade_out + c2*fade_in` with
  `fade_out + fade_in == 1` at every sample. `plan_chunks` /
  `ola_separate` are pure and locked by reconstruction tests.
- **The ONNX session is a singleton** (`_SESSIONS` keyed by path+mtime,
  lock-guarded). Re-creating `InferenceSession` per request spikes RAM
  and leaks; don't "make it fresh".
- **DL jobs run on a single-worker ThreadPoolExecutor** (one heavy
  inference at a time = the RAM ceiling) and report progress into the
  in-memory task registry polled via `/api/task/{id}` — long runs never
  hold an HTTP request open. Finished tasks prune to the newest 32.
- **The reference Demucs ONNX exports fix the segment length.** Their
  STFT/iSTFT lives INSIDE the graph with reflect pads sized for exactly
  `SEGMENT_SAMPLES` (343980 = 7.8 s @ 44.1 kHz) — any other input
  length fails with a Pad/Reshape error. `DemucsSeparator.separate`
  therefore defaults its chunks from `self._segment` (exact segment,
  50% overlap = COLA-exact Hann) and zero-pads the sub-segment tail
  chunk (`_pad_segment`); injected-infer backends and
  `KEYPRISM_DL_SEGMENT` overrides flow through the same seam. Do not
  "simplify" back to arbitrary chunk sizes.
- **Model discovery defaults live in `dlsep._VARIANTS`** — demucs_4 is
  `smank/htdemucs-onnx`, demucs_6 is `StemSplitio/htdemucs-6s-onnx`
  (graph contract of both: input `mix [1,2,T]`, single output
  `sources|stems [1,S,2,T]`, STFT embedded, opset 17). Phase 3.5's
  claim that no auto-downloadable 6-stem export existed was WRONG
  (its smank default shipped 4 sources and always failed the
  zero-probe); 3.10 points demucs_6 at the real 6-stem export, whose
  source order is drums/bass/other/vocals/**guitar/piano** —
  `STEM_SPECS` mirrors that order POSITIONALLY (rows are zipped, never
  name-matched), so do not "alphabetize" it. huggingface.co is
  unreachable from the dev network: set `HF_ENDPOINT=https://hf-mirror.com`
  for the download, or drop any compatible ONNX into
  `~/.keyprism/models/demucs/` (the glob fallback picks it up). Every
  resolved model file is recorded into
  `<model_dir>/provenance.json` (repo id, revision, commit, etag,
  license URL) for the license audit — weights are NEVER redistributed
  in-repo, and a repo swap must keep that metadata flowing.
- **ORT execution providers are probed, not hardcoded**
  (3.10 `dlsep.provider_chain`): CUDA → DirectML → CoreML → CPU,
  intersected with `ort.get_available_providers()`;
  `KEYPRISM_ORT_PROVIDERS="CUDA,CPU"` (comma list, order = preference)
  overrides. Plain `[dl]` stays CPU-only; `[dl-cuda]` /
  `[dl-directml]` add the GPU builds, and a missing/broken GPU extra
  is a SILENT CPU fallback (session load even retries CPU-only when a
  compiled-in EP fails at creation) — never a crash, never a 500.
  What `/api/ping` reports as `capabilities.ort_providers` is the
  ACTIVE chain (the session's `get_providers()` readback — demucs'
  embedded STFT ops may partially fall back to CPU), not the requested
  one; the frontend GPU/CPU badge renders from it.
- **Quality tiers are external shifts, not graph options** (3.10
  `dlsep.shift_passes`): for each extra pass the CHUNK is circularly
  shifted, inferred, shifted back, and the passes averaged — the ONNX
  graph, chunking and OLA math stay untouched (`shifts=0` must remain
  byte-equivalent to the pre-3.10 single pass). fast/balanced/best →
  0/1/2 extra passes. The `/api/stems` cache is QUALITY-AWARE: the
  POST serves the cache only when `status.json`'s `quality` matches
  the request (a tier switch recomputes instead of serving a
  mismatched render); progress denominators are chunks × passes.
- **Frontend gain architecture is `frontend/src/mixer.js`.**
  `MixerState` is the single source of truth: strips feed a MASTER
  GainNode capped at `MASTER_CEILING` (mirrors `player.js` `VOL_MAX`,
  change both together), strips are born MUTED (loading stems/lanes
  never changes what the user hears), solo anywhere force-silences
  every non-soloed strip, and any open stem silences the Mix (its
  content is inside the stems — summing both clips). Suppressed rows
  are shown `.dimmed`; their own M/S buttons keep the user's state.
  Both `stems.js` and `lanes.js` validate the `/api/stems` response
  against the fixed per-method stem list before rendering.
- **Panel row DOM comes from ONE factory** (`frontend/src/controls.js`,
  used by BOTH `stems.js` and `lanes.js`) — never fork per-panel row
  markup, or the slider/label inconsistency class of bugs returns.
  Notes (扒谱) availability is STRICTLY lazy: checked on click against
  `/api/ping` capabilities and surfaced as a dismissible toast
  (`toast.js`) with the precise reason — never auto-fetched on panel
  load. i18n key usage is CI-enforced by `npm run check:i18n`
  (`frontend/scripts/check-i18n.mjs`): a `t()` key must exist in BOTH
  dicts.
- **DL stem cache invalidation** mirrors Phase 2: files under
  `<entry>/stems/<DL_STEMS_VERSION>/<method>/` (+ `status.json`,
  atomic tmp-swap) and `<entry>/notes/<POLY_VERSION>/notes_poly_<stem>.json`;
  bump `DL_STEMS_VERSION` / `POLY_VERSION` when the numerics or JSON
  schema change in a way that must invalidate (never "just delete the
  cache"). Demucs stems are mono PCM16 at 44.1 kHz regardless of the
  entry's rate — the frontend schedules by seconds, so mixed rates stay
  sample-locked.
- **Note merging is not optional.** Frame predictors shred sustained
  notes into ~50 ms fragments; `merge_fragmented_notes` (same pitch,
  gap < 50 ms → extend end, fold conf) runs on EVERY compute path in
  `transcribe_polyphonic`. Bypassing it floods MIDI and the canvas.
- **Frontend notes are canvas, never `layout.shapes`.** Six polyphonic
  lanes mean tens of thousands of rects — SVG dies. `lanes.js` keeps a
  per-lane notes overlay `<canvas>`, redraws from the applied plotly
  `xaxis.range` on `plotly_relayout`, and renders only the visible
  window via the spatial index (sorted starts + binary search bounded by
  the longest note). The frontend source contract is asserted by
  `tests/test_dl_workspace.py` — do not reintroduce shapes for notes.
- **Capabilities drive the UI.** `/api/ping` returns
  `capabilities.dl/poly/dl_methods`; `lanes.js` hides/disables the Demucs
  options from it. Keep the flag → UI path intact when touching ping.

## Lane Visualization Engine & Layer Compositor (Phase 3.9)

Decisions that are easy to "simplify" into regressions:

- **Shared plot geometry is a hard contract** (`frontend/src/geometry.js`).
  `PLOT_LEFT_PX`/`PLOT_RIGHT_PX` are the single source: the master figure
  runs fixed pixel margins + full-domain x axis (`margin.autoexpand`
  off — nothing may grow the margins), and the lane grid columns are
  DERIVED in style.css from the same `--plot-left`/`--plot-right` custom
  properties, so every lane canvas spans exactly the master plot area's
  [left, right] pixels. That is what makes a drum hit land on the same
  vertical line in master + lanes + overlay layers. Consequences: do not
  add plot margins/domain fractions anywhere, do not give `.lane-scope` a
  real border (it is an inset box-shadow precisely so the canvas box
  stays exact), and the keyboard strip paper fractions must be re-pinned
  on resize (`applyPlotShapes` in the main.js resize observer).
- **plotly 'paper' coordinates span the PLOTTING AREA INSIDE the margins**
  (3.9.1 root cause): the margin zone [0, PLOT_LEFT_PX) is NEGATIVE paper
  territory. The keyboard strip ([40, PLOT_LEFT_PX−4], hugging the plot
  edge) and the pitch labels (left-anchored ANNOTATIONS at ~10px — plotly
  tick labels cannot leave the axis edge) both live there; the plot area
  contains only spectrum. Rebuild both together wherever
  `currentShapes()` is relayouted (buildFigure / applyPitchRange /
  applyPlotShapes); annotation y stays in data coords so setSub moves it
  automatically.
- **Pitch→pixel mapping has ONE definition** (`geometry.js`
  `keyRangeUnits` / `rowCenterUnit` / `unitToPlotFraction`, 3.9.1 D).
  The master yaxis range and the overlay band mapping both derive from
  it — do not re-derive `m*sub + (sub-1)/2` in another module. The
  ≤1 px overlay-vs-master row alignment is locked by
  `npm run test:geometry` (pure-Node, geometry.js is DOM-import-safe by
  a guard, wired into CI).
- **Lane playheads ride the master cursor's frame loop**
  (`player.onFrame`, fired from `reposition()` — the same function the
  playback rAF loop and every seek/relayout call repaint through). One
  time source (`ctx.currentTime`), zero drift, and playback never
  repaints a lane canvas: the playhead is a DOM hairline with a
  composited transform, like the master cursor.
- **LOD waveforms never scan samples on the main thread** (`wavelod.js` +
  `wave-worker.js`). At view windows ≤ `LOD_SPAN_SEC` (10 s) the lane
  paints per-pixel-column min/max computed in the worker; the worker owns
  ONE transferred mono PCM copy per lane (the AudioBuffer keeps its own
  data for playback) so pan/zoom requests ship only the view window.
  Results cache per [lane, exact view window, column count] — any pan,
  zoom or resize allocates a new key (invalidation by construction), FIFO
  bounded, and the cache dies with the lane object on method/track
  switches (`lod.forgetAll()` drops the worker-side PCM). While a compute
  is in flight the overview envelope keeps painting — never blank.
- **`/api/stem_spec` reuses the Phase 0 row space** (`payload.
  stem_spec_payload` → `spec_matrix`, sub=1 → 88 semitone rows). That row
  identity is what makes lane mini-spectrograms and overlay layers line
  up with the master heatmap's y axis — do not "fix" it to linear Hz
  bands. Normalization basis (3.9.1 C): the MASTER's mix joint peak
  (`payload.joint_spec_peak` — the exact value data.json is quantized
  against, computed once per track in `server._master_peak`), NOT the
  stem's own peak; the response carries `peak_ref` + `basis` and the
  frontend asserts the master's `dbRange`. Disk cache is
  `spec_<stem>.json` inside the version-tagged stems dir — cached bodies
  must carry the `basis` tag, so pre-3.9.1 own-peak caches recompute
  without touching the WAVs (bumping `STEMS_VERSION`/`DL_STEMS_VERSION`
  would needlessly invalidate the stems themselves); compute needs only
  the cached WAV (no onnxruntime) and 404s with the compute hint when
  separation has not run.
- **Overlay layers blend with CSS, not pixels** (`frontend/src/layers.js`).
  叠加 = `mix-blend-mode: screen` + per-layer opacity (dark spec pixels
  are no-ops under screen); 覆盖 = `normal` + opacity 1 (the spec image
  is opaque by construction, so it occludes the base). The `.layer-stack`
  container must stay z-auto: a z-indexed container would form a stacking
  context and then the canvases could only blend against the stack, never
  against the heatmap painted beneath it (the canvases carry the
  z-index themselves). Layer y-mapping follows the master's LIVE
  sub/pitch-range state (`getSub`/`getPitchLoHi` getters in main.js)
  through the shared `unitToPlotFraction` — semitone m spans row units
  [m·sub−0.5, (m+1)·sub−0.5] in [lo·sub−0.5, (hi+1)·sub−0.5]. Per-layer
  INTENSITY (3.9.1 B) is separate from opacity: `gain_dB` shifts the dB
  matrix and `gamma` re-shapes the colormap input (`stemspec.js
  intensityValue`, master color-floor/γ semantics) — opacity only
  alpha-mixes the result. γ DIRECTION (3.9.1 fix): the master warps
  colorscale ANCHORS by p^γ, which equals warping the data by v^(1/γ) —
  so the layer applies v^(1/γ) and a BIGGER γ reads BRIGHTER, matching
  the master's slider; the naive v^γ inverts it (locked by
  `npm run test:intensity`, wired into CI). Overlays redraw ONLY on
  view-range/geometry change (throttled) — never per frame; the playhead
  lives on its own DOM layer above the stack so playback is free.

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
uv run pytest -q               # regression tests (95 cases, ~12s)
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
