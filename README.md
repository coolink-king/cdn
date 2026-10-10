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
| `webhook.headers` | 需要鉴权时加请求头，例如 `Authorization: "Bearer xxx"` |
| `webhook.payload` | JSON 载荷的字段名映射，见下 |
| `poll_interval` | 轮询间隔秒数，默认 300 |
| `max_notify_per_feed` | 每个源每轮最多成功推送条数，默认 20；超出的留到下一轮，避免触发机器人限流 |
| `notify_on_first_run` | 默认 `false`：新接入的源只记录历史条目做基线，不一次性刷屏 |

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

已推送条目的标识记在 `state.json`（与配置文件同目录），重启后不会重复通知，每个源最多保留 1000 条记录。删除该文件等于全部重新判定。推送失败的条目不会写入状态，下一轮自动重试。

## 换成别的推送渠道

程序按通用 JSON POST 发送，不对应具体平台模板。若接收端要求特定字段名或需要包一层嵌套结构，先试着用 `title_field` / `text_field` / `extra` 对齐；对不齐的话改 `send_webhook()` 里的 `payload` 构造即可。钉钉、飞书机器人的加签（`timestamp` + `sign`）未内置，需要自行在 `send_webhook()` 里计算后拼到 URL 上。
