"""Payload reservation contract: notes/stems are present but null in
Phase 0 (reserved for note-level transcription / source separation).

This file is additive by design: the pre-existing contract test
(tests/test_payload.py) must stay untouched, so the new fields are
asserted here instead of being folded into the old field list.
"""

import pytest

from keyprism import audio_io
from helpers import make_wav
from keyprism.payload import analyze


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    pub = tmp_path / "pub"
    pub.mkdir()
    monkeypatch.setattr(audio_io, "PUBLIC_DIR", pub)
    return pub


@pytest.mark.parametrize("field", ["notes", "stems"])
def test_reserved_fields_present_and_null(tmp_path, field):
    p = tmp_path / "t.wav"
    make_wav(p, seconds=1.5)
    d = analyze(p, 0.0, None, 2048, 70.0, 5, 1)
    assert field in d          # presence: the contract reserves the key
    assert d[field] is None    # null until its phase lands


def test_reserved_fields_flow_into_staged_payload(tmp_path):
    from keyprism.analyze import run_analysis
    p = tmp_path / "t.wav"
    make_wav(p, seconds=1.5)
    payload, _ = run_analysis(p, window=2048, rate=5, sub=1)
    assert payload["notes"] is None and payload["stems"] is None
