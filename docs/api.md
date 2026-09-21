# KeyPrism HTTP API Reference

> Scope: the backend running in serve mode
> (`uv run python -m keyprism --serve 9630`).
> CORS is wide open (`Access-Control-Allow-Origin: *`), so frontend and
> backend can be deployed separately.

## Endpoints

| Method | Endpoint | Description |
|------|------|------|
| GET | `/api/ping` | health check, returns `{"ok": true}` |
| GET | `/api/spec?rate=15&sub=5` | recompute the spectrum at the given resolution (three channels + envelopes), cached |
| GET | `/api/notes?track=bass\|lead\|both` | monophonic transcription notes of the current track, cached per track under the analysis entry |
| GET | `/api/midi?track=bass\|lead\|both` | the same notes exported as a Standard MIDI File (attachment download) |
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
`"stems": null`. They are placeholders for upcoming features (note-level
transcription and separated source stems) and carry no data yet; consumers
should treat them as optional and ignore `null`.
