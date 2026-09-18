#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KeyPrism 后端包

模块分层 (各文件头有详细说明):
    keyprism.dsp       纯算法: STFT / 半音聚合 / 降采样 / BPM
    keyprism.audio_io  解码链: sndfile -> PyAV 回退, 浏览器兼容转存, 路径常量
    keyprism.payload   前端契约: data.json 字段的唯一权威定义
    keyprism.server    HTTP 服务: /api/ping /api/spec /api/upload
    keyprism.cli       命令行入口

运行: uv run python -m keyprism [音频] [--serve PORT] ...
"""
