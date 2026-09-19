# AGENTS.md — KeyPrism Development & Long-Term Maintenance Guide

> Aimed at future developers and AI coding agents. End-user documentation lives
> in `README.md`; this file carries no usage instructions.
> Purpose: record **why things are designed this way**, **what must be done
> when changing them**, and **where the pitfalls are**, so intent doesn't get
> lost over time.

## Architecture Layers (read before changing code)

```
src/keyprism/cli.py       CLI entry (thin shell, only argparse and dispatch)
src/keyprism/server.py    HTTP service: ping / spec / upload   ← the only module coupled to stdlib httpd
src/keyprism/payload.py   frontend contract: single source of truth for data.json fields (incl. matplotlib envelope rendering)
src/keyprism/audio_io.py  decode chain (sndfile→PyAV fallback) + path constants (PUBLIC_DIR / KEYPRISM_HOME)
src/keyprism/dsp.py       pure algorithms: STFT / semitone aggregation / downsampling / BPM   ← zero IO, numpy arrays in and out
```

> src layout note: source lives in `src/keyprism/`, but the package name is
> still `keyprism` —
> imports are always `from keyprism.dsp import ...`, runs are always
> `python -m keyprism`,
> never write `from src.keyprism import ...`.

Dependencies flow one way: `cli → server → payload → dsp / audio_io`.
Reverse imports and circular dependencies are forbidden.

**Layering iron rules**:
- `dsp.py` never does IO (no file reads, no print, no network) — that is the
  prerequisite for testing it in isolation
- `data.json` fields may only be defined in one place, `payload.py`; the
  frontend `frontend/src/main.js` is a consumer
- If the web framework is ever swapped (FastAPI/WebSocket etc.), only
  `server.py` should be thrown away and rewritten; the other four layers
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
git switch master && git pull               # after merging: tag and push
git tag -a vX.Y.Z -m "KeyPrism X.Y.Z" && git push origin master --tags
```

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
