# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.0] - 2026-09-23

Phase 3 feature-complete release. The Phase 3 batch was cut incrementally
as 0.5.0–0.6.4 — those sections below keep the granular records; this
section consolidates the capability set as it ships in 0.7.0, on top of
the final compliance and documentation pass of this release.

### Features

- 6-stem Demucs separation: `demucs_6` auto-downloads the real 6-stem
  ONNX export (`StemSplitio/htdemucs-6s-onnx`, MIT; registry order
  drums/bass/other/vocals/guitar/piano) — guitar/piano lanes work end
  to end, with poly Notes on the poly-capable ones; `demucs_4` stays
  the 4-stem default; a zero-probe still validates the graph contract
  (fixed segment + stem count) at model load
- Inference quality tiers fast / balanced / best via demucs shift
  averaging around the untouched ONNX graph (1 / 2 / 3 passes;
  `shifts=0` byte-equivalent to the single pass), tier-aware stem
  cache, per-pass progress denominator, persisted dropdown
- Device selector Auto / GPU / CPU (`&device=` routing; the GPU option
  disables itself on GPU-less builds; provider list is part of the
  session-singleton key) and explicit Run / Stop / Restart task
  control: cooperative cancel at chunk/pass boundaries (partial
  downloads discarded, no cache written), Restart = cancel + re-run
  with `force=1` cache bypass; parameter changes never auto-analyze
- Layer compositor: drag a lane's ⧉ handle onto the master spectrogram
  to overlay that stem's semitone spectrogram exactly on the master
  plot — time/pitch alignment via the shared plot geometry and pitch
  mapping, intensity alignment via master-mix-peak normalization
  (`peak_ref` + `basis` tag), CSS screen/normal blending with per-layer
  opacity, ±24 dB gain and highlight-γ reshaping, visibility /
  drag-to-reorder / remove manager; overlays repaint only on view
  changes

### Audio Engine

- DAW-grade mixer (`frontend/src/mixer.js`): destructive solo
  materialized as real mute states (M and S can never both light up),
  per-stem make-up gain defaults (lone stems audition at
  mix-comparable loudness), a brickwall limiter (−1 dB / 20:1 / 1 ms)
  on the master bus, strips born muted, anti-clipping mix ducking —
  locked by the `npm run test:mixer` transition-table suite
- Per-lane playheads driven by the master cursor's frame loop (one
  `ctx.currentTime` time source, zero drift) and LOD hi-res lane
  waveforms: per-pixel-column min/max computed in a Web Worker,
  cached per [lane, view window, columns], overview envelope keeps
  painting while a compute is in flight

### UI-UX

- Lane visualization engine: shared pixel-exact plot geometry between
  the master heatmap and every lane canvas (`geometry.js` + CSS custom
  properties, alignment locked by `npm run test:geometry`), the
  keyboard strip / pitch labels rebuilt into the left margin, [Wave|
  Spec] per-lane mini-spectrograms from `GET /api/stem_spec`
  (Phase 0 row space, disk-cached, basis-checked)
- Compact control strip inside the Mix row (method / quality / device
  selects, GPU/CPU badge, Run/Stop/Restart, status) — the empty lane
  placeholder is gone and the controls stay reachable mid-task; the
  full separation registry (classic + Demucs) is exposed in one
  dropdown with a hand-off to the stems panel
- Capability gating everywhere: Notes (扒谱) availability checked
  lazily on click against `/api/ping` with precise-reason toasts,
  dead-dropdown fixes, i18n key-completeness enforced by
  `npm run check:i18n` in CI

### GPU & Deployment

- ORT execution-provider auto-detection (CUDA → DirectML → CoreML,
  CPU always last) with `KEYPRISM_ORT_PROVIDERS` override; new extras
  `[dl-cuda]` (Linux) and `[dl-directml]` (Windows) — a missing or
  broken GPU extra is a silent CPU fallback, and a GPU EP failing at
  first inference evicts the session and retries pure CPU
- CUDA 13 runtime shipped as pip wheels with a gated preload
  (`ort.preload_dlls` only fills gaps: a complete system CUDA install
  wins and is never shadowed; without a CUDA-13-capable driver the
  wheels are skipped) — fixes the
  `libcublasLt.so.13: cannot open shared object file` silent
  CPU degrade on WSL/驱动less hosts
- Model provenance metadata: every resolved model file records repo
  id, revision, commit, etag, license URL and source URL into
  `<model_dir>/provenance.json` (weights are never redistributed
  in-repo); auto-download streams to `.part` + atomic rename,
  honors `HF_ENDPOINT`, and is per-byte-chunk cancellable

### Docs

- NOTICE final audit (v2): NVIDIA CUDA Toolkit / cuDNN wheels
  characterized (proprietary, non-OSI, optional `[dl-cuda]`-only,
  Linux-only, never redistributed by KeyPrism), onnxruntime-gpu /
  onnxruntime-directml MIT, the MIT 6-stem export and the 4-stem
  export's research-only MUSDB18-HQ weight lineage, LGPL
  (libsndfile/FFmpeg) redistribution story resolved, Slakh2100 /
  MUSDB18 dataset re-check — a "Non-OSI components" section keeps the
  core MIT product clean
- README / README_CN feature refresh for the stable Phase 3 set:
  6-stem + quality tiers + device selector + CUDA-13 preload, layer
  compositor, DAW mixer, lane workspace and the extended HTTP API
  table (EN/zh kept in lockstep)
- AGENTS.md carries the Phase 3.9/3.10 architecture decisions
  (geometry contract, LOD worker, provider chains, quality tiers,
  cancel chain)

## [0.6.4] - 2026-09-23

### Added

- 3.10.4 — CUDA 13 pip wheels + GPU activation fix:
  - `[dl-cuda]` now ships the CUDA 13 runtime libraries as pip wheels
    (`nvidia-cublas` / `nvidia-cuda-runtime` / `nvidia-cuda-nvrtc` /
    `nvidia-cudnn-cu13` / `nvidia-curand` / `nvidia-cufft`, Linux) —
    since ORT 1.23 the GPU build targets CUDA 13 and NVIDIA stopped
    publishing a system toolkit path for WSL users
  - `dlsep._preload_pip_cuda_libs()` calls `ort.preload_dlls()` before
    every session build — with environment pre-checks: a COMPLETE
    system CUDA install (every required soname already resolvable)
    wins and is never shadowed (dlopen dedupes by soname, so preloads
    can only fill gaps, never override), and without a CUDA-13-capable
    NVIDIA driver (`cuDriverGetVersion` probe, WSL shim included) the
    wheels are skipped as dead weight; the CUDA EP used to fail with
    `libcublasLt.so.13: cannot open shared object file` and silently
    degrade to CPU even on a working GPU (further guarded: no ORT /
    pre-1.21 / probe or preload error → unchanged CPU fallback)
- 3.10.3 — compact control strip + stop/restart:
  - The AI Separation panel's controls (method / quality / device
    selects, GPU badge, status) collapsed from stacked full-width rows
    into ONE horizontal wrapped control strip living in the Mix row's
    right cell (the empty dashed lane placeholder is gone); the Mix row
    renders in every state so the controls stay reachable mid-task
  - Stop (⏹) & Restart (↻) buttons: `POST /api/task/{id}/cancel` flips
    a per-task cooperative cancel flag — the model download checks it
    per byte-chunk and inference per chunk / per shift pass (ort
    `run()` is uninterruptible; the stop lands at the next boundary,
    ≤ one ~7.8 s model segment later) — the task ends `cancelled`,
    partial `.part` downloads are removed and no stem cache is written;
    Restart cancels any running task and re-submits with `force=1`
    (`POST /api/stems&force=1` bypasses the stems cache and recomputes)
- Phase 3.10 — 6-stem auto-download, GPU providers, quality tiers:
  - `demucs_6` now auto-downloads the REAL 6-stem ONNX export
    (`StemSplitio/htdemucs-6s-onnx`, MIT) — the Phase 3.5 conclusion
    that no auto-downloadable 6-stem export existed was wrong; the
    registry mirrors the export's source order
    drums/bass/other/vocals/guitar/piano (guitar BEFORE piano), so the
    piano/guitar lanes finally work end to end (six lanes, poly Notes
    on the poly-capable ones). The zero-probe contract check (segment
    length + stem count) still fails fast on mislabelled exports
  - Model provenance metadata: every resolved model file (auto-download,
    cache re-resolve, env override, model-dir glob) is recorded into
    `<model_dir>/provenance.json` — repo id, revision, commit, etag,
    license URL, source URL — for the license audit; weights are never
    redistributed in-repo
  - ORT execution-provider auto-detection: CUDA → DirectML → CoreML →
    CPU probed against `ort.get_available_providers()`; overridable via
    `KEYPRISM_ORT_PROVIDERS` (comma list, order = preference, short or
    raw names); new extras `[dl-cuda]` (onnxruntime-gpu) and
    `[dl-directml]` (onnxruntime-directml) — plain `[dl]` stays
    CPU-only, and a missing GPU extra is a silent CPU fallback; a
    compiled-in but unusable GPU EP retries CPU-only at session load.
    `/api/ping` exposes the ACTIVE provider chain (session
    `get_providers()` readback) as `capabilities.ort_providers`, and
    the lanes panel shows a GPU/CPU badge with the chain as tooltip
  - Inference quality tiers via demucs `shifts`, applied externally
    around the ONNX graph (circular-shift → infer → shift back →
    average; chunking/OLA untouched): fast = 1 pass, balanced = 2
    (default), best = 3; `POST /api/stems&quality=` with a tier-aware
    cache (tier switch recomputes; `status.json` records the tier) and
    a per-pass progress denominator (chunks × passes); the lanes panel
    gains a persisted quality dropdown beside the method select

### Changed

- `dlsep.STEM_SPECS["demucs_6"]` order changed to match the reference
  export (`guitar` now before `piano`); no valid demucs_6 cache can
  exist (the previous variant always failed the zero-probe), so
  `DL_STEMS_VERSION` was NOT bumped — 4-stem caches stay valid

### Added

- 3.10.2 hotfix — ORT CUDA fallback + Device selector:
  - Device dropdown (Auto / GPU / CPU) in the AI Separation panel,
    persisted in localStorage and POSTed as `&device=`; the GPU option
    disables itself (and a stale persisted `gpu` coerces back to Auto)
    when `/api/ping` reports no GPU EP in the build
    (`capabilities.ort_providers_available`)
  - `/api/stems&device=auto|gpu|cpu` routing: auto = probe chain,
    cpu = forced pure CPU, gpu = forced first available GPU EP + CPU
    (400 "GPU requested but no GPU provider available." on a GPU-less
    build); a session-singleton key now includes the provider list so a
    device switch never reuses a session built for another chain

### Fixed

- ORT CUDA fallback (D1): `provider_chain` ALWAYS ends with
  `CPUExecutionProvider` (ops without a GPU kernel run on CPU instead
  of failing the session); a GPU EP that fails at FIRST INFERENCE
  (CUDA error 9 / NOT_IMPLEMENTED on e.g. a Conv node — surfaces at
  `session.run()`, not at session creation) is now caught: warning
  logged, cached session evicted, pure-CPU session rebuilt, the call
  retried once — verified live on a CUDA host with a missing cuDNN
  (`libcudnn.so`): the task completed on CPU instead of crashing
- 3.10.1 hotfix — dead dropdowns + 6-stem UI plumbing:
  - The method/quality dropdowns in BOTH separation panels came up
    permanently disabled: the post-load re-render ran while the loading
    flag was still set (cleared only in `finally`, after the render),
    and the selects are built with `disabled = state.loading` — so
    demucs_6 was unreachable and every click died. Latent since 3.8;
    the loading flag is now cleared BEFORE the final render on the
    success and error paths of both panels (the 3.9 layer compositor
    was NOT the cause: `.layer-stack` is z-auto + `pointer-events:
    none` by design)
  - The AI Separation panel's method dropdown exposes the FULL
    separation registry in one place (classic hpss/rpca/combined
    grouped, then demucs_4/demucs_6); a classic selection hands off to
    the stems panel via the new `stems.openWithMethod()` entry point
  - The DL progress status names the tier's pass count
    ("{pct}% ({passes} passes)") so the chunks × passes scaling is
    visible while a tier runs
- DL model resolution order: the anonymous model-dir glob
  (`*.onnx`) is now consulted only AFTER the variant's auto-download —
  previously a foreign export already in the dir (e.g. a 4-stem
  `htdemucs.onnx` from an earlier demucs_4 install) satisfied a
  demucs_6 resolve first and was rejected by the zero-probe with a
  confusing stem-count error instead of fetching the right model; the
  glob stays as the manual/offline escape hatch

## [0.6.3] - 2026-09-23

### Added

- Phase 3.9 — lane visualization engine + layer compositor (M1/M2):
  - Shared plot geometry (`PLOT_LEFT_PX` / `PLOT_RIGHT_PX`, JS consts in
    `frontend/src/geometry.js` mirrored into `--plot-left` /
    `--plot-right` CSS custom properties): the master spectrogram plot
    area and every lane canvas span the identical pixel boundaries, so a
    drum hit lands on the same vertical line in both; the keyboard strip
    is pinned to the fixed pixel geometry and re-pinned on resize
  - Per-lane playhead hairlines driven by the master cursor's own
    repaint path (new `player.onFrame`) — one time source, zero drift,
    and playback never repaints a canvas
  - LOD high-res waveforms: at view windows ≤ 10 s the lane waveform is
    painted from per-pixel-column min/max computed on demand in a Web
    Worker (`wavelod.js` + `wave-worker.js`), cached per
    [lane, view window, column count]; the worker owns one transferred
    mono PCM copy per lane
  - Per-stem spectrogram endpoint `GET /api/stem_spec?method=&stem=`
    (Phase 0 semitone aggregate of the stem, sub=1 → 88 rows, dB-
    compressed against the stem's own peak, base64 uint8) disk-cached as
    `spec_<stem>.json` beside the WAV in the version-tagged stems dir;
  - Per-lane [Wave|Spec] toggle: the lane canvas flips to a mini-
    spectrogram rendered from the shared `stemspec.js` fetch cache;
    lazily fetched on first click, failure toasts and degrades to Wave
  - Layer compositor: drag a lane's ⧉ handle onto the master spectrogram
    to create an overlay layer of that stem's spectrogram — exactly
    covering the master plot area, y-mapped onto the master heatmap's
    key range; blending via CSS `mix-blend-mode` (叠加 = `screen` +
    per-layer opacity slider, 覆盖 = `normal` + opacity 1); a top-right
    manager lists layers topmost-first with blend / opacity /
    visibility / drag-to-reorder / remove; overlays redraw only on
    view-range change (throttled) — playback never repaints them

### Changed

- Master figure x axis now uses fixed pixel margins + full-domain range
  (`margin.autoexpand` off) so the plot-area boundaries are exact; lane
  grid columns are derived from the same CSS variables (the lane scope's
  1px border became an inset box-shadow to keep the canvas box exact)

### Fixed

- 3.9.1 hotfix (three defects):
  - Keyboard strip relocated INTO the left margin: plotly 'paper' shape
    coordinates span the plotting area inside the margins, so 3.9's
    fractions drew the keyboard INSIDE the plot area (~280–480px,
    occluding the spectrum) while the left margin sat empty. The pitch
    labels (C-note names) are now left-anchored annotations at ~10px —
    leftmost — and the keyboard spans [40, PLOT_LEFT_PX−4] hugging the
    plot edge; the plot area contains only spectrum. Resize re-pinning
    (applyPlotShapes) now re-pins shapes AND annotations
  - Layer INTENSITY controls: opacity is alpha-mixing, so per-layer
    gain_dB (−24…+24 dB, default 0) and γ (0.3…3.0, default 1) now act
    on the layer's dB matrix BEFORE the tint (dB' = dB + gain, then
    v' = v^(1/γ) — the master's highlight-γ direction: the master warps
    colorscale anchors by p^γ, so a BIGGER γ reads BRIGHTER; the naive
    v^γ would invert the slider), on a second line of each layer-manager
    row; gain shifts WHICH energies light up, opacity only fades the
    whole layer. Direction locked by `npm run test:intensity` (Node,
    wired into CI)
  - Stem-spec normalization rebased onto the MASTER's mix joint peak
    (`peak_ref` + `basis: "mix_joint_peak"` in the payload; frontend
    asserts the master's dbRange): at gain 0 / opacity 1 / screen an
    overlaid stem's brightness now equals its true share of the mix
    instead of being inflated to its own full scale (pre-3.9.1 caches
    fail the basis check and recompute). Pitch→pixel mapping extracted
    into ONE shared definition (geometry.js `keyRangeUnits` /
    `rowCenterUnit` / `unitToPlotFraction`) used by both the master
    heatmap axis and the overlay canvases; a new `npm run test:geometry`
    (Node, wired into CI) locks overlay-vs-master row alignment to
    ≤ 1 px (measured worst |Δ| = 0.000000 px over 108 cases)

## [0.6.2] - 2026-09-23

### Added

- Two-phase DL progress (first-run UX): when the Demucs ONNX weights
  are absent, the separation task now enters a `downloading` phase —
  `/api/task/{id}` reports `{status: "downloading", bytes_done,
  bytes_total, speed_mbps}` alongside the classic `{status: "running",
  progress}` inference phase — and the lanes panel shows 下载模型
  x.x / y.y MB (z MB/s) → 分离中 p% in one continuous run (download
  auto-continues into inference, no extra clicks). Downloads stream to
  `<model>.part` with an atomic rename, so an interrupted download can
  never leave a corrupt cache entry; the downloader is dependency-free
  (stdlib urllib) and honors `HF_ENDPOINT` for mirror networks
- NOTICE v1: hand-maintained third-party license inventory of the
  current runtime/optional/frontend dependencies, with the LGPL
  (libsndfile, FFmpeg-in-PyAV) and research-only weights provenance
  flagged as TODOs for 3.10

### Changed

- AI-Separation click is never dead (capability gating): the On button
  awaits the (cached) `/api/ping` capabilities on click; a missing
  `[dl]` extra toasts 需要 uv sync --extra dl (onnxruntime), disables
  the button and uses the same text as its tooltip — no task is ever
  started — while a poly-only unavailability keeps the distinct
  basic-pitch message; with DL present the button spins immediately
  while the separation task spins up

### Fixed

- Lane envelopes were invisible (black lanes, only the peak-dB label
  rendered): `drawWave` indexed the cached envelope with
  `lane.bucketSec`, but the field lives on the envelope itself
  (`lane.peaks.bucketSec`) — the column index was `NaN`, every column
  was treated as silence and skipped, ever since the lane canvases were
  introduced. Muting now also dims the envelope (35 % alpha) instead of
  ever hiding it: silence ≠ invisible; unmuted lanes draw at full stem
  color and the redraw follows mute/solo changes.

## [0.6.1] - 2026-09-23

### Added

- Mixer transition-table test (`npm run test:mixer`,
  `frontend/scripts/test-mixer.mjs`): executes the FULL destructive-solo
  transition table (T1–T5, invariant I1, snapshot restore, solo move,
  the mix-duck rule, make-up math and limiter wiring) against a fake
  AudioContext on pure Node; wired into the CI frontend job
- `/api/ping` capabilities carry `poly_reason` exactly when `poly` is
  false — the precise reason (install remedy vs the Python ≥ 3.12
  basic-pitch limitation) that drives the Notes-button tooltip and toast

### Changed

- Per-stem make-up gain and dB faders (Phase 3.7 gain staging): at stem
  decode each strip computes `makeup_dB = clamp(20*log10(p_mix/p_stem),
  0, +12)` and its fader defaults to it, so an unmuted stem auditions at
  mix-comparable loudness (stems were ~11 dB quieter than the mix at
  fader max); faders are −40…+18 dB with a live dB readout, a 0 dB
  detent and per-lane waveform envelopes that auto-scale to each lane's
  own peak (plus a peak-dB label) so quiet stems render visibly
- A brickwall limiter (`DynamicsCompressorNode`: threshold −1 dB, knee
  0, ratio 20, attack 1 ms, release 100 ms) now sits between the master
  bus and the destination — make-up defaults plus open faders can sum N
  stems past full scale, and the −1 dB threshold leaves headroom for
  the 1 ms attack-time overshoot on transients
- Compact mixer grid on both panels: `[label 96px][controls 152px]
  [lane 1fr]`, 8 px column gap, 80 px rows — 16 px icon + 12 px no-wrap
  label, fixed 84 px fader, 26 px square M/S keys, and the Notes (扒谱)
  button as a compact ♫ chip overlaid on its lane scope (was
  `[label 140px][controls 200px]` with a wide dead gap)

### Fixed

- Mixer mute/solo semantics (Phase 3.7): solo is DESTRUCTIVE — it
  materializes as real mute states (T1: soloist unmutes, every other
  row force-mutes from a frozen pre-solo snapshot; T3: unmuting any row
  releases the solo; T4/T5: self-mute releases, solo-unmute) — so
  effective audibility is simply NOT muted and buttons render strictly
  from state: the M and S buttons can never both light up on one row,
  in any click sequence
- `/api/notes?method=poly` answered the "uv sync --extra dl" install
  remedy even when only basic-pitch was missing (unfixable on
  Python ≥ 3.12); the two 501s now carry distinct codes and prose
  (`dl_not_installed` vs `poly_unavailable`) matching the new
  `capabilities.poly_reason`

## [0.6.0] - 2026-09-22

### Fixed

- Stem/lane panel UI polish (Phase 3.6 visual QA): every mixer row now
  renders an `<icon><text>` label from the fixed stem registries (per
  track inline SVG glyphs, EN/中文 live-switchable, with the zh labels
  混音/人声/鼓/贝斯/其他/谐波/打击乐/低秩伴奏/稀疏主音); lane waveforms
  draw from a cached ~1024-column peak envelope computed once at decode
  — visible immediately after separation, before any playback — and a
  per-lane decode failure renders an in-lane placeholder instead of
  failing the panel; both panels build every row (mix included) from
  ONE shared factory (`controls.js`), so the volume slider, M/S keys
  and layout can no longer drift between rows; the stray cryptic "PX"
  button becomes a uniform Notes (扒谱) button on the poly-eligible
  lanes with strictly LAZY availability: capabilities are checked on
  click and unavailability surfaces as a dismissible toast with the
  precise reason (`需要 uv sync --extra dl` when onnxruntime is
  missing; `多音转录在 Python ≥3.12 不可用；分离功能不受影响` when
  basic-pitch is unavailable on Python ≥ 3.12) — never auto-triggered
  on panel load; born-muted stems and the ducked mix row are `.dimmed`
  WITH explanatory tooltips; lanes use a
  `[label 140px][controls 200px][lane 1fr]` grid with 88 px rows
- New i18n key-completeness guard (`npm run check:i18n`,
  `frontend/scripts/check-i18n.mjs`): scans every `t('…')` /
  `` t(`prefix${…}`) `` usage and every `data-i18n*` attribute and
  fails when a key is missing from either the EN or the ZH dict (and
  when the two dicts drift apart); enforced in the CI frontend job

- `/api/stems` now derives its `stems` list from the FIXED per-method
  registry (`stems.STEM_SPECS` / `dlsep.STEM_SPECS`) instead of the
  cached `status.json`, so the answer can never depend on file length or
  chunking: hpss → exactly `['harmonic', 'percussive']`,
  rpca → `['lowrank', 'sparse']`, combined → `['harmonic', 'percussive']`,
  demucs_4 → exactly `['drums', 'bass', 'other', 'vocals']`,
  demucs_6 → the 6-stem registry. `method=demucs` is accepted as an
  alias of the canonical `demucs_4` everywhere (`/api/stems` GET/POST,
  `/api/stem`)
- Demucs model auto-download was silently broken twice over: the
  pinned Hub repo (`Xenova/htdemucs-onnx`) no longer exists, and the
  network cannot reach huggingface.co anyway. `_VARIANTS` now points at
  `smank/htdemucs-onnx` (verified graph contract: `mix [1,2,T]` →
  `sources [1,S,2,T]`, STFT embedded), and the documented remedy for
  the offline network is `HF_ENDPOINT=https://hf-mirror.com` (or
  dropping any compatible ONNX into `~/.keyprism/models/demucs/`)
- Demucs separation crashed on every real model: the reference ONNX
  exports embed the STFT/iSTFT and only accept their fixed segment
  (343980 samples = 7.8 s @ 44.1 kHz), while the separator chunked at
  generic 10 s windows (and a pulled-back short tail). Chunking now
  defaults to the model's fixed segment with a 50% COLA-exact overlap
  and zero-pads the sub-segment tail chunk (`_pad_segment`); verified
  end-to-end on an 89 s track (~40 s CPU, 4 stems, no clipping)
- A session zero-probe now validates the ONNX graph contract at model
  load (fixed segment accepted, output stem count matches the variant
  registry), so a bad export fails immediately with an actionable
  message instead of a cryptic `'stem4'` KeyError ~40 s into a
  separation. Known caveat: no usable auto-downloadable 6-stem export
  exists on the Hub right now (mislabelled or STFT-input-requiring
  files) — `demucs_6` needs a compatible ONNX via
  `KEYPRISM_DEMUCS6_FILE`; `demucs_4` works out of the box
- Frontend mixing architecture: separated stems/lanes no longer connect
  straight to `ctx.destination` at unity gain (louder than the −10 dB
  mix ceiling; mix + stems summed into clipping). A shared `MixerState`
  (new `frontend/src/mixer.js`) feeds every strip through a MASTER
  GainNode capped at the same −10 dB ceiling and computes the DAW
  mute/solo matrix in one place: solo anywhere silences every
  non-soloed strip at gain level; a strip silenced by others' solo or
  by the anti-clipping rule is shown `.dimmed` while its own M/S buttons
  keep the user's state
- Stem/lane defaults are DAW-standard and anti-clipping: loading stems
  no longer mutes the Mix and blasts every stem at unity — the Mix keeps
  playing at its current volume and every separated strip starts MUTED
  (gain 0) until unmuted/soloed; unmuting any stem silences the Mix
  (its content is inside the stems — summing both would double the
  waveform)
- Multi-track (AI Separation) toggle unresponsiveness: the Demucs
  toggle flipped to "On" even when the `[dl]` extra was missing and
  then silently did nothing; it now refuses to flip, shows the reason
  in the panel status, and the remedy (`uv sync --extra dl`) is set as
  a tooltip on the On button when capabilities report DL missing
- A failed/partial stem decode no longer leaves half-wired GainNodes
  connected to the output bus (gain nodes route into the master bus
  only after a fully successful load; failures disconnect everything)
- The frontend validates the `/api/stems` response against the fixed
  per-method stem contract and fails loudly instead of rendering
  chunk-count-dependent mystery rows

### Changed

- `[dl]` extra: `basic-pitch` is now marked `python_version < '3.12'`
  (it pins `tensorflow<2.15`, which ships no CPython 3.12 wheels, so
  `uv sync --extra dl` failed outright on 3.12). On 3.12 the Demucs
  stack (onnxruntime + huggingface-hub) still installs and Basic Pitch
  poly transcription degrades to 501 — the designed graceful
  degradation; Python 3.10/3.11 keep full DL functionality
- The top-bar multi-track control is now visibly branded
  "AI Separation (Demucs)" (i18n EN/ZH) instead of the opaque "Lanes"

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
