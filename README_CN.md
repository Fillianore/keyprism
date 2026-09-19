# KeyPrism

<div align="center">

[English](README.md) | **简体中文**

</div>

**KeyPrism** — piano-key spectrogram player.
把音频解析到钢琴半音网格的频谱棱镜：横轴时间、纵轴 88 键（可细分到 1/10 半音）、
颜色为能量强度，配合同步播放与一整套交互分析工具。

```
音频 → STFT → 半音聚合 → 交互式热图 + 播放器
```

## 功能一览

### 频谱分析
- **钢琴音高热图**：横轴时间（自适应刻度，放大到 16s 内每 1s 一档），纵轴 88 个半音
- **立体声三通道**：混合 / 左 / 右 三套频谱，统一峰值归一化（dB 跨通道可比）
- **可切换分辨率**：时间列密度 5/10/15/30 列每秒 × 半音细分 1/5/10 子带，
  切换由后端实时重算（需 serve 模式），进度弹窗 + 分阶段进度条
- **子带展宽**：高细分模式下沿频率轴做三角核能量展宽，避免信息碎片化
- **BPM 识别**：频谱通量自相关估计 BPM 与首拍偏移，小节网格叠加（可手动修正 BPM/偏移/拍号）
- **在线选曲**：顶栏"选择音乐"按钮选择本地音频，上传后端解析并整页切换（需 serve 模式）
- **音频格式**：mp3/wav/ogg/flac 直接读取；m4a/aac/wma/opus/aiff 等其他主流容器
  经 PyAV (ffmpeg) 解码（sndfile 不支持时自动回退），并自动转存浏览器全兼容的
  PCM WAV 供播放；无音轨文件给出明确报错

### 交互与播放
- **Web Audio 播放引擎**：整段 PCM 预解码，seek 瞬时无卡顿，采样级精确时钟
- **播放光标**：HTML 覆盖层 + `transform` 渲染，满帧平滑；与"耳朵听到的声音"对齐
  （扣除输出延迟），起播自动等待缓冲
- **点击频谱任意位置**定位播放进度（不自动播放）
- **跟随播放**：可开关的视窗自动平移
- **导航条**：全曲能量包络背景，窗口拖动（宽度恒定）/ 手柄调宽 / 点击跳转
- **色彩控制**：6 套配色（下限锚点纯黑，静默区沉入背景）+ 色彩下限滑块 +
  高光 γ 幂变换（非线性映射凸显当前主音），均可点击输入数值、一键还原

### 其他
- 音域上下限裁剪（键盘与刻度自动适配）
- 音量上限 −10 dB（防嘈杂），音量记忆
- 深色控制台质感 UI

## 目录结构

```
keyprism/
├── src/                   # 后端源码 (src layout, 包名 keyprism)
│   └── keyprism/          # 后端 Python 包 (所有后端实现集中于此)
│       ├── __main__.py    # python -m keyprism 入口
│       ├── cli.py         # CLI 参数解析与调度
│       ├── dsp.py         # 纯算法: STFT / 半音聚合 / 降采样 / BPM 估计
│       ├── audio_io.py    # 解码链: sndfile→PyAV 回退 / 浏览器转存 / 路径常量
│       ├── payload.py     # 前端契约: data.json 字段的唯一权威定义
│       └── server.py      # HTTP 服务: /api/ping /api/spec /api/upload
├── pyproject.toml        # uv 项目定义 (依赖锁定见 uv.lock, 默认走清华源)
├── scripts/              # 启动与发版脚本 (配置与端口见下)
│   ├── start.sh          # 一键启动 Linux / macOS
│   ├── start.cmd         # 一键启动 Windows
│   └── release.sh        # 版本扎口: 改版本号 + 收口 CHANGELOG + 开发版 PR
├── tests/                # pytest 自动化回归: 四层各司其职 (见下节)
├── assets/               # 演示音频等静态资产
│   └── demo.m4a
├── docs/                 # 开发者文档 (HTTP API 参考等)
│   └── api.md
└── frontend/             # Vite 前端 (vanilla JS + plotly.js)
    ├── index.html
    ├── vite.config.js
    ├── public/           # 后端输出: data.json + audio.*
    └── src/
        ├── main.js         # 入口: 数据加载与控件装配
        ├── spectrogram.js  # 热图 + 键盘 + 小节网格 + 布局
        ├── ticks.js        # 刻度自适应 + 时间范围钳制
        ├── navbar.js       # 底部导航条
        ├── player.js       # Web Audio 播放引擎
        └── style.css
```

## 测试

`tests/` 不实现产品功能, 是自动化回归测试: 修改代码后运行
`uv run pytest -q`, 30 个用例覆盖 DSP 算法 (含 120 BPM 节拍器确定性用例)、
解码链回退 (真实 m4a 编码)、payload 契约 (前端依赖字段逐一断言)、
HTTP 全路由 (上传切换 / 错误码 / CORS 预检)。它是重构与加功能时的安全网,
建议保留。

CI (`.github/workflows/ci.yml`) 在每次 push / PR 时自动执行同样流程:
后端 `uv sync + pytest` + 启动脚本语法检查, 前端 `npm ci + build`,
全部约 2 分钟。测试过时会被强制拦截——要么修代码, 要么有意识地更新测试。

面向开发者的架构意图、修改守则与已知陷阱见 `AGENTS.md`（AI 编程智能体亦会自动读取该文件）。

### 发版（面向开发者）

devel 合并到 master **即为发版**。扎口已脚本化：`scripts/release.sh
--bump minor` 自动改版本号（`pyproject.toml` + `uv.lock`）、收口
CHANGELOG 并开发版 PR；合并后在 master 打 tag
（`git tag -a vX.Y.Z -m "KeyPrism X.Y.Z" && git push --tags`）。
完整规则与踩过的坑（单人 `--admin` 合并、CI 检查名精确匹配）见 `AGENTS.md`。

## 工作区（`~/.keyprism`）

日志、缓存与上传暂存统一收敛到用户工作区，不污染项目目录与系统临时目录；
可用环境变量 `KEYPRISM_HOME` 整体重定向（支持 `~` 前缀）：

```
~/.keyprism/
├── logs/               # backend.log / vite.log (启动脚本重定向)
├── cache/matplotlib/   # matplotlib 字体与配置缓存
├── uploads/            # "选择音乐" 上传的音频暂存 (自动只保留最近几个)
└── config.env          # 可选持久配置: KEY=VALUE, # 开头为注释
```

配置优先级：**环境变量 > `config.env` > 内置默认**（`config.env` 只识别
`KEYPRISM_` 前缀的键，且不会覆盖已导出的环境变量）：

| 变量 | 说明 | 默认 |
|------|------|------|
| `KEYPRISM_HOME` | 工作区根目录 | `~/.keyprism` |
| `KEYPRISM_LOG_DIR` | 日志目录 | `$KEYPRISM_HOME/logs` |
| `KEYPRISM_API_HOST` | 后端监听地址（局域网访问用 `0.0.0.0`） | `127.0.0.1` |
| `KEYPRISM_API_PORT` | 后端 API 端口 | `9630` |
| `KEYPRISM_FRONTEND_PORT` | 前端页面端口 | `5270` |

## 环境要求

- **Python 3.10+**，通过 [uv](https://docs.astral.sh/uv/) 管理环境（自动创建 venv 并锁定依赖）
- **Node.js 20.19+**（用于 Vite 7 前端，`npm run dev` 在更低版本无法启动）

## 快速开始

```bash
# 1) 一键启动 (首次自动 uv sync + npm install; 不带参数时默认加载 assets/demo.m4a)
#    Windows:
scripts\start.cmd
#    Linux / macOS:
./scripts/start.sh [音频文件]

# 2) 浏览器打开 http://localhost:5270
#    在 VS Code 集成终端运行时, API 与前端端口会自动出现在 Ports 面板
```

脚本会同时拉起（Ctrl+C 全部停止，Windows 下关闭最小化窗口）：
- 后端 API `http://localhost:9630`（分析服务，支持分辨率热切换 / 上传音频）
- 前端页面 `http://localhost:5270`

端口与工作区可用环境变量或 `~/.keyprism/config.env` 修改（见上节）。

### 手动分步运行

```bash
# 后端: uv 自动创建环境并安装依赖 (未指定音频时默认 assets/demo.m4a)
uv run python -m keyprism --serve 9630

# 前端
cd frontend
npm install
npm run dev -- --port 5270 --strictPort
```

静态模式（无需常驻服务，分辨率固定为启动参数）：

```bash
uv run python -m keyprism 歌曲.mp3 --rate 15 --sub 1
```

## 后端参数

```
uv run python -m keyprism [音频] [--serve PORT] [--rate R] [--sub S]
                              [--start 秒] [--end 秒]
                              [--window 8192] [--db-range 70]
```

| 参数 | 说明 | 默认 |
|------|------|------|
| `音频` | 输入音频文件，省略时加载 `assets/demo.m4a` | `assets/demo.m4a` |
| `--serve PORT` | 常驻 API 模式，前端可热切换分辨率 / 上传音频 | 关闭 |
| `--host` | serve 模式监听地址（局域网访问用 `0.0.0.0`） | `127.0.0.1` |
| `--rate` | 时间列密度（列/秒），可选 5/10/15/30 | 15 |
| `--sub` | 每半音子带数，可选 1/5/10 | 1 |
| `--start` / `--end` | 只分析音频片段 | 全曲 |
| `--window` | STFT 窗长（采样点，2 的幂） | 8192 |
| `--db-range` | 动态范围 dB | 70 |

## HTTP API（serve 模式）

| 端点 | 说明 |
|------|------|
| `GET /api/ping` | 健康检查 |
| `GET /api/spec?rate=15&sub=5` | 重算指定分辨率的频谱（三通道 + 包络），带缓存 |
| `POST /api/upload?name=歌曲.mp3` | 上传本地音频（请求体为原始文件字节），后端解析并切换当前曲目，刷新 `data.json`；返回完整 payload |

完整参考（错误码 / 响应结构 / 预检）见 `docs/api.md`。
上传示例：`curl -X POST --data-binary @歌曲.m4a 'http://localhost:9630/api/upload?name=歌曲.m4a'`，
大小上限 512MB，解码链为 libsndfile → PyAV (ffmpeg) 回退，覆盖主流音频格式。

## 前端操作速查

| 操作 | 效果 |
|------|------|
| ♪ 选择音乐 | 弹出文件选择器，本地音频上传后端解析后自动刷新整页（需 serve 模式） |
| 滚轮 | 缩放时间轴（音高轴锁定） |
| 拖动频谱区 | 平移（两端截止，不可拖出全曲） |
| 双击 | 恢复全曲视图 |
| 点击频谱 | 定位播放进度（不自动播放） |
| 底部导航条 | 拖窗口平移 / 拖手柄调宽 / 点击空白跳转 |
| 顶栏 | 分辨率、通道、音域、BPM、偏移、拍号、配色、色彩下限、高光 γ |
| 点击数值标签 | 直接输入（Enter 提交 / Esc 取消），↺ 还原默认 |

## 技术说明

- **时间映射**：降采样后每列映射到所属时间块中心（`帧距 × 块长`），
  前端以全曲时长均分校准，杜绝列/秒不一致造成的漂移
- **播放同步**：`AudioContext` 调度 + `outputLatency` 补偿 + 起播缓冲等待；
  光标为 DOM 覆盖层，不触发 plotly 重排
- **性能**：热图 restyle 节流（先导+尾随 120ms）；高分辨率解码分块异步，
  只驻留当前通道的浮点矩阵
- **配色下限锚点强制纯黑**：只改首锚点颜色，锚点间仍线性过渡，与 γ 变换正交
- **在线选曲链路**：前端 XHR 直传原始文件字节（带上传进度条），后端流式落盘系统
  临时目录 → 解码分析 → 原子切换服务端曲目状态并清空分辨率缓存；音频 URL 带缓存
  破坏参数，切换曲目后不会读到浏览器缓存的旧文件

## License

MIT
