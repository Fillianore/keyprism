# KeyPrism HTTP API Reference

> Scope: the backend running in serve mode
> (`uv run python -m keyprism --serve 9630`).
> CORS is wide open (`Access-Control-Allow-Origin: *`), so frontend and
> backend can be deployed separately.

## Endpoints

| Method | Endpoint | Description |
|------|------|------|
| GET | `/api/ping` | health check, returns `{"ok": true, "capabilities": {"dl": bool, "poly": bool, "dl_methods": [...]}}` |
| GET | `/api/spec?rate=15&sub=5` | recompute the spectrum at the given resolution (three channels + envelopes), cached |
| GET | `/api/notes?track=bass\|lead\|both` | monophonic transcription notes of the current track, cached per track under the analysis entry |
| GET | `/api/midi?track=bass\|lead\|both` | the same notes exported as a Standard MIDI File (attachment download) |
| GET | `/api/stems?method=hpss\|rpca\|combined` | separated stems of the current track (Phase 2); `&progress=1` polls a running computation |
| GET | `/api/stems?method=demucs_4\|demucs_6` | computed DL stems from the entry cache (Phase 3, optional); compute is started via POST |
| POST | `/api/stems?method=demucs_4\|demucs_6` | start DL separation as a background task; returns `{"task_id", "status_url"}` (501 without the `[dl]` extra) |
| GET | `/api/task/{task_id}` | background-task status/progress/result poll |
| GET | `/api/notes?track=piano\|guitar\|other&method=poly[&source=demucs_6]` | polyphonic notes of a DL stem (Phase 3, optional) |
| GET | `/api/stem?method=..&name=..` | one computed stem as a PCM16 WAV attachment download (classic and DL methods) |
| POST | `/api/upload?name=song.mp3` | upload a local audio file (request body is raw file bytes); the backend analyzes it, switches the current track and refreshes `data.json`; returns the full payload |
| OPTIONS | any endpoint | CORS preflight, returns 204 |

## Upload Endpoint

Put the raw bytes of the audio file directly in the request body (no
multipart form needed):

```bash
curl -X POST --data-binary @song.m4a \
  'http://localhost:9630/api/upload?name=song.m4a'
```

- The `name` query parameter is the user-visible display name; path components
  are stripped (`../` etc. are sanitized to a bare file name)
- Size cap: 512 MB
- Decode chain: libsndfile reads directly, falling back automatically to
  PyAV (ffmpeg), covering mainstream audio formats
- Success: `200` + the full payload (identical to the `data.json` contents)
- Failure: `500` + `{"error": "..."}`; the staged file is cleaned up
  automatically

## Notes & MIDI Endpoints (Phase 1)

Both endpoints run the monophonic transcription (bass / lead presets,
`src/keyprism/tracks.py`) on demand against the cached complex STFT of the
currently loaded track — `data.json` deliberately keeps `"notes": null`
(the heavy payload contract is untouched; notes are never inlined into
it).

### GET /api/notes?track=bass|lead|both

Response `200` (only the requested tracks appear in `notes`):

```jsonc
{
  "track": "bass",
  "notes": {
    "bass": [
      // times in seconds from segment start; conf in [0, 1]
      {"pitch": 45, "start": 0.5573, "end": 0.9752, "conf": 0.8044}
    ]
  }
}
```

`track=both` returns `{"track": "both", "notes": {"bass": [...],
"lead": [...]}}`.

- First call computes the full pipeline (salience → onsets → Viterbi
  decode) and caches the JSON at
  `<analysis entry>/notes/<MONO_VERSION>/notes_<track>.json`; subsequent
  calls serve the cached bytes verbatim (warm path is a file read).
  `MONO_VERSION` lives in `tracks.py`; bumping it invalidates old note
  caches automatically. Evicted STFT entries are transparently recomputed
  through the analysis cache.

### GET /api/midi?track=bass|lead|both

Response `200`: `audio/midi` attachment `keyprism_<slug>_<track>.mid`
(slug from the display file name). Single requested track → SMF type 0;
`both` → type 1 (tempo-only conductor track + one named note track per
preset). 480 ticks/quarter, tempo from the detected BPM (fallback 120),
note times shifted by the detected first-beat offset, velocity =
64 + round(63 × conf).

### Error Codes (notes/midi)

| Code | Scenario |
|----|------|
| 400 | unknown `track` value (anything but `bass` / `lead` / `both`) |
| 409 | no track loaded (cannot happen in normal serve mode) |
| 500 | transcription failed (unreadable analysis artifact etc.) |

## Stems Endpoints (Phase 2)

Classic (training-free) source separation of the currently loaded track,
computed on demand against the cached complex STFT — `data.json` keeps
`"stems": null` by contract, stem audio is served only here.

Methods (`src/keyprism/hpss.py`, `rpca.py`, `stems.py`):

- `hpss` — median-filter HPSS (Fitzgerald): harmonic vs percussive Wiener
  masks
- `rpca` — Robust PCA via chunked inexact ALM/ADMM: low-rank (`lowrank`)
  vs sparse (`sparse`) masks
- `combined` (default) — agreement-weighted fusion of both: Stem 1 =
  Harmonic+LowRank, Stem 2 = Percussive+Sparse

Stems are real masks applied to the COMPLEX STFT (original phase
preserved) and rendered through the Phase 0 ISTFT as mono PCM16 WAVs,
cached under the analysis entry as
`<entry>/stems/<STEMS_VERSION>/<method>/<stem>.wav` (+ `status.json`).
Bumping `STEMS_VERSION` in `stems.py` invalidates old caches; evicted
STFT entries are transparently recomputed.

### GET /api/stems?method=hpss|rpca|combined

Blocking (first call computes, subsequent calls are cache hits). Response
`200`:

```jsonc
{
  "method": "combined",
  "cached": false,
  "elapsed_sec": 17.7,       // compute time of the last computation
  "duration": 89.118,        // stem duration in seconds
  "sample_rate": 44100,
  "stems": [
    {"key": "harmonic",
     "url": "http://localhost:9630/api/stem?method=combined&name=harmonic"}
    // stem keys per method: hpss/combined -> harmonic+percussive,
    //                        rpca -> lowrank+sparse
  ]
}
```

Progress reporting: because the computation can take tens of seconds, the
frontend (or any client) may poll

    GET /api/stems?method=combined&progress=1

which returns immediately with `{"method": "combined", "done": 3,
"total": 6}` (work blocks processed; zeros before the first call). The
blocking call should be issued in parallel; progress is per method.

### GET /api/stem?method=..&name=..

Response `200`: `audio/wav` attachment
`keyprism_<slug>_<method>_<name>.wav` (slug from the display file name),
streamed verbatim from the entry cache. Mono PCM16, same sample rate and
duration as the analysis segment; amplitude is clipped to [-1, 1].

### Error Codes (stems)

| Code | Scenario |
|----|------|
| 400 | unknown `method` or `name` (not in the method's stem list) |
| 404 | stem not computed yet (request `/api/stems` first) |
| 409 | no track loaded |
| 500 | separation failed (unreadable analysis artifact etc.) |

## DL Workspace Endpoints (Phase 3, optional)

Deep-learning separation (Demucs via ONNX Runtime) and polyphonic
transcription (Basic Pitch) are OPTIONAL: they require the `[dl]` extra
(`uv sync --extra dl`) and, for Demucs, the model weights (auto-downloaded
into `~/.keyprism/models/demucs/` on first use; explicit files via
`KEYPRISM_DEMUCS4_FILE` / `KEYPRISM_DEMUCS6_FILE`, alternate repos via
`KEYPRISM_DEMUCS4_REPO` / `KEYPRISM_DEMUCS6_REPO`). Without the extra the
DL endpoints answer **501 Not Implemented** with a human-readable error and
`GET /api/ping` reports `"capabilities": {"dl": false, "poly": false,
"dl_methods": []}` — the app itself keeps working (Phase 2 pipeline).

Demucs runs CHUNKED: the track is separated in 10 s windows with 1 s
overlap, cross-faded by a strictly-positive periodic Hann window divided
by the accumulated window sum (perfect edge reconstruction, no seam
clicks, RAM bounded by one chunk + the singleton ONNX session). Stems
cache under the analysis entry as
`<entry>/stems/<DL_STEMS_VERSION>/<method>/<stem>.wav` (+ `status.json`);
bump `DL_STEMS_VERSION` in `dlsep.py` to invalidate. Polyphonic notes
cache as `<entry>/notes/<POLY_VERSION>/notes_poly_<stem>.json` (bump
`POLY_VERSION` in `poly_transcribe.py`).

### POST /api/stems?method=demucs_4|demucs_6

Starts separation of the current track as a BACKGROUND task (single-worker
executor: one DL job at a time). Response `200`:

```jsonc
{ "task_id": "9f1c…", "status_url": "/api/task/9f1c…", "status": "started" }
```

If the method's stems are already cached, the response is the same JSON as
`GET /api/stems?method=…` with `"cached": true` and no task is started.
Errors: `400` unknown method, `409` no track loaded, `501` `[dl]` extra
missing.

### GET /api/task/{task_id}

Non-blocking poll of a background task:

```jsonc
// running
{ "id": "9f1c…", "status": "running", "progress": 0.45 }
// done — same shape as the classic /api/stems response
{ "id": "9f1c…", "status": "done", "progress": 1.0,
  "method": "demucs_6", "duration": 89.118, "sample_rate": 44100,
  "stems": [{"key": "vocals", "url": "http://…/api/stem?method=demucs_6&name=vocals"},
            … ] }   // demucs_4: drums/bass/other/vocals; demucs_6: + piano/guitar
// failed
{ "id": "9f1c…", "status": "error", "progress": 0.2, "error": "…" }
```

`404` for unknown ids; finished tasks are pruned to the newest 32.

### GET /api/stems?method=demucs_4|demucs_6

Cache-only: serves the stem list when computed (`200`, same JSON shape as
the classic endpoint), `404` with a hint otherwise — computation is only
ever started via `POST`. `GET /api/stem?method=demucs_4&name=vocals`
downloads one DL stem (mono PCM16 WAV at 44.1 kHz).

### GET /api/notes?track=piano|guitar|other&method=poly[&source=demucs_6]

Basic Pitch polyphonic transcription of a DL stem. Same-pitch notes with
gaps below 50 ms are merged (frame predictors fragment sustained notes —
the merge runs on every compute path). Response `200`:

```jsonc
{
  "track": "piano", "method": "poly", "cached": false,
  "notes": {
    "piano": [ {"pitch": 64, "start": 0.55, "end": 2.41, "conf": 0.83}, … ]
  }
}
```

Errors: `400` unknown track/source combination (`track` must be one of the
`source` variant's stems; piano/guitar exist only in `demucs_6`), `409`
the stem WAV has not been computed yet (start `POST /api/stems` first),
`501` `[dl]` extra missing.

## Error Codes

| Code | Scenario |
|----|------|
| 404 | unknown route |
| 409 | an import task is already in progress |
| 411 | request body missing (Content-Length is 0) |
| 413 | file exceeds the 512 MB cap |
| 500 | analysis failed (unsupported format / corrupted file / no audio track) |

## spec Response Structure

```jsonc
{
  "specs":    { "mix": "<base64 uint8>", "left": "...", "right": "..." },
  "envelopes":{ "mix": "data:image/png;base64,...", "...": "..." },
  "nCols": 960,       // number of time columns
  "hopSec": 0.066,    // column spacing (seconds)
  "rate": 15,         // effective columns/second (clamped to 5..30)
  "sub": 5            // effective subbands per semitone (invalid values fall back to 1)
}
```

The authoritative definition of the full `data.json` field contract lives in
`src/keyprism/payload.py`; the contract test `tests/test_payload.py` asserts
it field by field — when the contract changes, both places must be synced.

Reserved contract fields: the payload always contains `"notes": null` and
`"stems": null`. They are on-demand feature reservations — note-level
transcription is served through `/api/notes` (Phase 1) and separated
stems through `/api/stems` (Phase 2); the heavy payload itself stays
untouched. Consumers should treat them as optional and ignore `null`.
