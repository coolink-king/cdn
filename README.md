# RSS 关键词监控

轮询 RSS/Atom 订阅源，出现配置的关键词时向 webhook 推送通知。

## 安装

需要 Python 3.10 或更高版本。

```bash
python -m pip install -r requirements.txt
```

## 配置

全部在 `config.yaml` 里，改完直接重启程序即可。

| 字段 | 作用 |
| --- | --- |
| `feeds[].url` / `name` | 订阅地址和显示名称，两者必填 |
| `feeds[].keywords` | 命中任意一个就推送；省略则用 `defaults.keywords` |
| `feeds[].exclude_keywords` | 命中任意一个直接跳过，优先级高于 `keywords` |
| `feeds[].match_in` | 在哪些字段里找关键词，可选 `title` / `summary` / `content` |
| `feeds[].case_sensitive` | 默认 `false`，中英文都按子串匹配 |
| `webhook.url` | 接收通知的地址 |
| `webhook.method` | 默认 `POST` |
| `webhook.headers` | 需要鉴权时加请求头，例如 `Authorization: "Bearer xxx"` |
| `webhook.timeout` / `retries` | 单次请求超时秒数（默认 10）与重试次数（默认 3，最小 1） |
| `webhook.payload` | JSON 载荷的字段名映射，见下 |
| `poll_interval` | 轮询间隔秒数，默认 300，下限 30（配更小会按下限跑，避免空转打满 CPU） |
| `max_notify_per_feed` | 每个源每轮最多**成功**推送条数，默认 20；超出的留到下一轮，避免触发机器人限流 |
| `notify_on_first_run` | 默认 `false`：新接入的源只记录历史条目做基线，不一次性刷屏 |
| `request_timeout` / `user_agent` | 抓取 RSS 的超时秒数（默认 20）与 User-Agent |
| `state_file` / `log_level` | 状态文件路径（相对配置文件目录）与日志级别，默认 `INFO` |

`keywords` 写成单个字符串（`keywords: Rust`）也可以，程序会自动当成一项；写成列表更符合直觉。类型不对的配置（`webhook.headers` 写成字符串、`poll_interval` 写成 `"20s"` 等）会在启动时报明确错误或直接回落到默认值，不会跑到一半才崩。

`webhook.payload` 决定发出的 JSON 长什么样。默认配置下：

```json
{
  "title": "条目标题",
  "content": "关键词: Rust\n来源: Hacker News 首页\n链接: https://...\n时间: ...\n摘要: ...",
  "source": "rss-monitor",
  "level": "info"
}
```

改 `title_field` / `text_field` 就能适配不同的字段名约定，`extra` 里的键值会原样合并进每条消息。

## 运行

```bash
python rss_monitor.py                    # 常驻轮询，Ctrl+C 退出
python rss_monitor.py --test             # 只发一条测试通知，验证 webhook 连通性
python rss_monitor.py --config prod.yaml # 指定配置文件
```

建议先跑 `--test` 确认地址和字段名没问题，再正式启动。

## 去重与状态

已推送条目的标识记在 `state.json`（相对配置文件目录），重启后不会重复通知，每个源最多保留 1000 条记录。删除该文件等于全部重新判定。

条目只有在 webhook **返回成功后**才写入状态，所以推送失败的条目下一轮会自动重试。反过来，进程被强杀时可能重试已发过的通知 —— 这是「至少送达一次」的取舍。`state.json` 写入失败（Windows 上文件被其他程序占用）只记录错误、不会让进程退出，代价同样是重启后可能重复通知。

## 退出与可靠性

`Ctrl+C` 在 Windows 和 Linux 上都能优雅退出（本轮跑完再停）；关闭窗口在 Windows 上会走 `SIGBREAK`，同样优雅；Linux 下 `kill` 发 `SIGTERM` 也走同一处理。单个源返回畸形 XML 或解析异常时只跳过该源，不会终止常驻进程。

webhook 返回 3xx 一律按失败处理（不跟随重定向）。地址被重定向到登录页时，跟随后拿到的 200 是假成功，通知其实没送到，所以宁可报错也不要静默吞掉。

## 换成别的推送渠道

程序按通用 JSON POST 发送，不对应具体平台模板。若接收端要求特定字段名或需要包一层嵌套结构，先试着用 `title_field` / `text_field` / `extra` 对齐；对不齐的话改 `send_webhook()` 里的 `payload` 构造即可。钉钉、飞书机器人的加签（`timestamp` + `sign`）未内置，需要自行在 `send_webhook()` 里计算后拼到 URL 上。
