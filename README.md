# KeyPrism

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
- **音频格式**：mp3/wav/ogg/flac 直接读取；m4a 等容器经 PyAV (ffmpeg) 解码，
  并自动转存浏览器全兼容的 PCM WAV 供播放

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
├── backend.py        # Python 后端: 分析 + (--serve) 常驻 API
├── dsp.py            # DSP 核心: STFT / 半音聚合 / 降采样
├── pyproject.toml    # uv 项目定义 (依赖锁定见 uv.lock)
├── start.cmd         # Windows 一键启动
├── start.sh          # Linux / macOS 一键启动
├── demo.m4a          # 演示音频
└── frontend/         # Vite 前端 (vanilla JS + plotly.js)
    ├── index.html
    ├── vite.config.js
    ├── public/       # 后端输出: data.json + audio.*
    └── src/
        ├── main.js         # 入口: 数据加载与控件装配
        ├── spectrogram.js  # 热图 + 键盘 + 小节网格 + 布局
        ├── ticks.js        # 刻度自适应 + 时间范围钳制
        ├── navbar.js       # 底部导航条
        ├── player.js       # Web Audio 播放引擎
        └── style.css
```

## 环境要求

- **Python 3.10+**，通过 [uv](https://docs.astral.sh/uv/) 管理环境（自动创建 venv 并锁定依赖）
- **Node.js 18+**（用于 Vite 前端）

## 快速开始

```bash
# 1) 一键启动 (首次自动 uv sync + npm install)
#    Windows:
start.cmd demo.m4a
#    Linux / macOS:
./start.sh demo.m4a

# 2) 浏览器打开 http://localhost:5180
```

脚本会同时拉起：
- 后端 API `http://localhost:8800`（分析服务，支持分辨率热切换）
- 前端页面 `http://localhost:5180`

### 手动分步运行

```bash
# 后端: uv 自动创建环境并安装依赖
uv run python backend.py demo.m4a --serve 8800

# 前端
cd frontend
npm install
npm run dev -- --port 5180 --strictPort
```

静态模式（无需常驻服务，分辨率固定为启动参数）：

```bash
uv run python backend.py 歌曲.mp3 --rate 15 --sub 1
```

## 后端参数

```
uv run python backend.py <音频> [--serve PORT] [--rate R] [--sub S]
                              [--start 秒] [--end 秒]
                              [--window 8192] [--db-range 70]
```

| 参数 | 说明 | 默认 |
|------|------|------|
| `--serve PORT` | 常驻 API 模式，前端可热切换分辨率 | 关闭 |
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

跨域已放开（`Access-Control-Allow-Origin: *`），前后端可分开部署。

## 前端操作速查

| 操作 | 效果 |
|------|------|
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

## License

MIT
