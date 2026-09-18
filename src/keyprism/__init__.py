#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism backend package

Module layering (each file's header has details):
    keyprism.dsp       pure algorithms: STFT / semitone aggregation / downsampling / BPM
    keyprism.audio_io  decode chain: sndfile -> PyAV fallback, browser-safe
                       transcoding, path constants
    keyprism.payload   frontend contract: single source of truth for data.json fields
    keyprism.server    HTTP service: /api/ping /api/spec /api/upload
    keyprism.cli       command-line entry

Run: uv run python -m keyprism [audio] [--serve PORT] ...
"""
