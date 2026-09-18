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
