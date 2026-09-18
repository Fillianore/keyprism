# AGENTS.md — KeyPrism 开发与长期维护指南

> 面向后续开发者与 AI 编程智能体。最终用户文档见 `README.md`，本文不承载使用说明。
> 目的：记录**为什么这样设计**、**改动时必须做什么**、**哪里有坑**，避免意图随时间失传。

## 架构分层（改代码前先看）

```
src/keyprism/cli.py       CLI 入口 (薄壳, 只做 argparse 与调度)
src/keyprism/server.py    HTTP 服务: ping / spec / upload   ← 唯一耦合 stdlib httpd 的模块
src/keyprism/payload.py   前端契约: data.json 字段唯一权威定义 (含 matplotlib 包络渲染)
src/keyprism/audio_io.py  解码链 (sndfile→PyAV 回退) + 路径常量 (PUBLIC_DIR / KEYPRISM_HOME)
src/keyprism/dsp.py       纯算法: STFT / 半音聚合 / 降采样 / BPM   ← 零 IO, 只进出 numpy 数组
```

> src layout 说明: 源码在 `src/keyprism/`, 但包名仍是 `keyprism`——
> 导入永远是 `from keyprism.dsp import ...`, 运行永远是 `python -m keyprism`,
> 不要写成 `from src.keyprism import ...`。

依赖方向单向：`cli → server → payload → dsp / audio_io`。禁止反向导入与循环依赖。

**分层铁律**：
- `dsp.py` 永远不做 IO（不读文件、不 print、不联网）——这是它可独立测试的前提
- `data.json` 的字段只允许在 `payload.py` 一处定义；前端 `frontend/src/main.js` 是消费方
- 未来若换 Web 框架（FastAPI/WebSocket 等），只应扔掉 `server.py` 重写，其余四层原样存活——这就是当初拆分的核心理由

## 设计意图存档（为什么是现在这样）

| 决策 | 理由 |
|------|------|
| 五模块拆分（而非单文件） | 接缝是框架无关的；闭包式单文件 Handler 无法脱离活进程测试 |
| `server.py` 暴露 `make_server()`（不阻塞启动） | 测试可用临时端口起真实服务再 shutdown；`run_server()` 才是阻塞入口 |
| `server.py` 运行时引用 `audio_io.PUBLIC_DIR`（模块属性而非 from-import 值绑定） | 测试 monkeypatch 一处即可完全隔离产物目录 |
| 解码链 sndfile → PyAV 回退 | libsndfile 覆盖 wav/flac/ogg/mp3，PyAV(ffmpeg) 兜底 m4a/aac/wma/opus/aiff 等 |
| 非 mp3/wav/ogg/flac 一律转存 PCM WAV | 保证浏览器 `decodeAudioData` 百分之百可解，兼容性优先于磁盘占用 |
| 工作区 `~/.keyprism`（logs/cache/uploads） | 日志与缓存不污染项目目录与系统临时目录；`KEYPRISM_HOME` 环境变量整体重定向 |
| `tests/` 的存在 | 不实现产品功能，是回归安全网；契约测试变红是**机制在正常工作**，不是测试落后 |
| CI 强制跑 pytest + build | 防止"红了没人管"演变成 skip 堆场——测试腐烂在合并前被拦截 |
| pyproject 默认清华源 | 开发网络环境官方 pypi 不可达；GitHub runner 访问清华源同样可达 |
| 端口 9630/5270（弃用旧 8800/5180） | 显式换新，避免旧进程/旧书签静默混淆 |

## 修改守则（合并前自查清单）

1. **改了任何 `src/keyprism/` 下的代码** → `uv run pytest -q` 必须全绿（约 6 秒，无理由跳过）
2. **改了 data.json 契约**（加/删/改字段）→ 同步三处：`payload.py`、`tests/test_payload.py`、`frontend/src/main.js`。契约测试逐字段断言就是为此设计的
3. **改了 HTTP API**（路由/参数/错误码）→ 同步 `tests/test_server.py`
4. **测试过时了** → 允许成批删除并按新契约重写；**禁止** skip、xfail、注释掉来"让 CI 过"
5. **新增功能配测试的原则**：少而关键——不变量（形状/能量/字段存在性）、契约、错误路径；不追求覆盖率数字
6. **大重构时**：先跑绿基线 → 小步搬移 → 每步跑绿。`tests/helpers.py` 自生成全部夹具（wav/m4a/120BPM 节拍器），不依赖 demo.m4a 与外部环境

## 已知边界与陷阱（改前必读）

- `PUBLIC_DIR`/`DEMO_AUDIO` 通过 `_repo_root()`（向上找 `pyproject.toml`）定位仓库根。uv 默认 editable 安装成立；若改为普通安装部署会路径失效
- `payload.py` 顶部的 `MPLCONFIGDIR` 设置**必须**在 `import matplotlib` 之前——顺序敏感，勿调整 import 顺序
- 前端音频 URL 带 `?t=` 缓存破坏参数（`main.js`）：上传切歌后浏览器缓存里同名 `audio.wav` 不可信，勿"清理"掉
- Vite 7 要求 **Node ≥ 20.19**；本仓 Windows 脚本 `scripts/start.cmd` 未在真实 Windows 上执行过（仅语法审校），首次在 Windows 使用时注意验证
- `uv.lock` 与 `frontend/package-lock.json` 均已提交，装依赖用 `uv sync` / `npm ci`，不要 `npm install` 漂移版本

## 常用命令

```bash
uv sync                        # 安装/同步 Python 依赖 (含 dev 测试组)
uv run pytest -q               # 回归测试 (30 用例, ~6s)
uv run python -m keyprism --serve 9630   # 手动起后端 (默认加载 assets/demo.m4a)
bash scripts/start.sh          # 一键起前后端 (端口/工作区见 ~/.keyprism/config.env)
cd frontend && npm ci && npm run build   # 前端依赖与构建
```

## 给未来维护者的一句话

测试是随代码演进的活文档，不是一次写成的文物。功能大改时契约测试必然变红——那是它在逼你有意识地同步前后端两侧，更新它（约每次迭代 5-15 分钟），不要绕过它。
