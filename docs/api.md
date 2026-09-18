# KeyPrism HTTP API 参考

> 适用范围：后端以 serve 模式运行时（`uv run python -m keyprism --serve 9630`）。
> 跨域已放开（`Access-Control-Allow-Origin: *`），前后端可分开部署。

## 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/ping` | 健康检查，返回 `{"ok": true}` |
| GET | `/api/spec?rate=15&sub=5` | 重算指定分辨率的频谱（三通道 + 包络），带缓存 |
| POST | `/api/upload?name=歌曲.mp3` | 上传本地音频（请求体为原始文件字节），后端解析并切换当前曲目，刷新 `data.json`；返回完整 payload |
| OPTIONS | 任意端点 | CORS 预检，返回 204 |

## 上传接口

请求体直接放音频文件的原始字节（不需要 multipart 表单）：

```bash
curl -X POST --data-binary @歌曲.m4a \
  'http://localhost:9630/api/upload?name=歌曲.m4a'
```

- `name` 查询参数为用户可见的显示名，会做路径剥离（`../` 等被清洗为纯文件名）
- 大小上限 512 MB
- 解码链：libsndfile 直读，失败自动回退 PyAV (ffmpeg)，覆盖主流音频格式
- 成功：`200` + 完整 payload（与 `data.json` 内容一致）
- 失败：`500` + `{"error": "无法解析该音频 (...)"}`，暂存文件自动清理

## 错误码

| 码 | 场景 |
|----|------|
| 404 | 未知路由 |
| 409 | 已有导入任务进行中 |
| 411 | 请求体缺失（Content-Length 为 0） |
| 413 | 文件超过 512 MB 上限 |
| 500 | 解析失败（格式不支持 / 文件损坏 / 无音轨） |

## spec 响应结构

```jsonc
{
  "specs":    { "mix": "<base64 uint8>", "left": "...", "right": "..." },
  "envelopes":{ "mix": "data:image/png;base64,...", "...": "..." },
  "nCols": 960,       // 时间列数
  "hopSec": 0.066,    // 列距 (秒)
  "rate": 15,         // 实际生效的列/秒 (会被钳制到 5..30)
  "sub": 5            // 实际生效的每半音子带数 (非法值回退 1)
}
```

`data.json` 完整字段契约的权威定义在 `src/keyprism/payload.py`，
契约测试 `tests/test_payload.py` 逐字段断言——改契约时两处必须同步。
