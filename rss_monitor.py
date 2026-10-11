"""RSS 关键词监控：轮询订阅源，命中关键词时推送 webhook。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import signal
import sys
import time
from pathlib import Path

import feedparser
import requests
import yaml

log = logging.getLogger("rss-monitor")

STOP = False
MAX_SEEN_PER_FEED = 1000
MAX_NOTIFY_PER_FEED = 20


def _handle_stop(signum, frame):
    global STOP
    STOP = True
    log.info("收到信号 %s，本轮结束后退出", signum)


def install_signal_handlers() -> None:
    # SIGBREAK 是 Windows 控制台窗口关闭时唯一的信号；POSIX 上没有这个符号
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _handle_stop)


def _as_mapping(value, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SystemExit(f"配置项 {name} 必须是键值映射，实际是 {type(value).__name__}: {value!r}")
    return value


def _num(cfg: dict, key: str, default: float, minimum: float, label: str = "") -> float:
    label = label or key
    value = cfg.get(key, default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        log.warning("配置 %s=%r 不是数字，改用 %s", label, value, default)
        return default
    if number < minimum:
        log.warning("配置 %s=%s 小于下限 %s，按下限处理", label, value, minimum)
        return minimum
    return number


def _keyword_list(value, label: str) -> list[str]:
    if value is None:
        return []
    # keywords: Rust 写成标量时会被逐字符迭代，几乎命中所有条目
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        raise SystemExit(f"配置 {label} 必须是列表或字符串，实际是 {type(value).__name__}: {value!r}")
    return [str(word) for word in value if str(word).strip()]


MATCH_FIELDS = {"title", "summary", "content"}


def _match_in(value, label: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    fields = [str(item) for item in (value or ["title", "summary"])]
    unknown = [item for item in fields if item not in MATCH_FIELDS]
    if unknown:
        log.warning("%s 里的 %s 不是可匹配字段，可选值 %s", label, unknown, sorted(MATCH_FIELDS))
    kept = [item for item in fields if item in MATCH_FIELDS]
    return kept or ["title", "summary"]


def load_config(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except FileNotFoundError:
        raise SystemExit(f"找不到配置文件: {path}")
    except OSError as exc:
        raise SystemExit(f"配置文件无法读取: {path} ({exc})")
    except yaml.YAMLError as exc:
        raise SystemExit(f"配置文件 YAML 语法错误: {path} ({exc})")

    if not isinstance(cfg, dict):
        raise SystemExit(f"配置文件格式错误: {path}")

    webhook = _as_mapping(cfg.get("webhook"), "webhook")
    if not webhook.get("url"):
        raise SystemExit("配置缺少 webhook.url")
    webhook["url"] = str(webhook["url"])
    webhook["headers"] = _as_mapping(webhook.get("headers"), "webhook.headers")
    payload = _as_mapping(webhook.get("payload"), "webhook.payload")
    payload["extra"] = _as_mapping(payload.get("extra"), "webhook.payload.extra")
    webhook["payload"] = payload
    webhook["timeout"] = _num(webhook, "timeout", 10, 1, "webhook.timeout")
    webhook["retries"] = int(_num(webhook, "retries", 3, 1, "webhook.retries"))

    defaults = _as_mapping(cfg.get("defaults"), "defaults")
    if not defaults.get("keywords"):
        log.warning("defaults.keywords 为空，只有 feed 自带 keywords 的源会产生通知")
    defaults["match_in"] = _match_in(defaults.get("match_in"), "defaults.match_in")

    feeds = cfg.get("feeds") or []
    if not isinstance(feeds, list) or not feeds:
        raise SystemExit("配置里没有任何 feeds（应为列表）")

    for index, feed in enumerate(feeds):
        if not isinstance(feed, dict):
            raise SystemExit(f"feeds[{index}] 必须是键值映射: {feed!r}")
        if not feed.get("url") or not feed.get("name"):
            raise SystemExit(f"feed 必须同时有 name 和 url: {feed}")
        feed["url"] = str(feed["url"])
        # 未声明关键词的源回落到全局关键词
        feed["keywords"] = _keyword_list(
            feed.get("keywords", defaults.get("keywords")), f"feeds[{index}].keywords"
        )
        feed["exclude_keywords"] = _keyword_list(
            feed.get("exclude_keywords", defaults.get("exclude_keywords")),
            f"feeds[{index}].exclude_keywords",
        )
        feed["match_in"] = _match_in(
            feed.get("match_in", defaults.get("match_in")), f"feeds[{index}].match_in"
        )
        if not feed["keywords"]:
            log.warning("%s 没有可用关键词，该源不会产生任何通知", feed["name"])

    cfg["defaults"] = defaults
    cfg["webhook"] = webhook
    cfg["poll_interval"] = int(_num(cfg, "poll_interval", 300, 30))
    cfg["request_timeout"] = _num(cfg, "request_timeout", 20, 1)
    cfg["max_notify_per_feed"] = int(_num(cfg, "max_notify_per_feed", MAX_NOTIFY_PER_FEED, 1))
    cfg.setdefault("state_file", "state.json")
    cfg.setdefault("log_level", "INFO")
    cfg.setdefault("notify_on_first_run", False)
    cfg.setdefault("user_agent", "rss-monitor/1.0")
    return cfg


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("状态文件 %s 无法读取（%s），按空状态继续", path, exc)
        return {}


def save_state(path: Path, state: dict) -> bool:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        # 状态写不进去只影响去重（重启后可能重复通知），不值得让常驻进程退出
        log.error("状态文件 %s 写入失败: %s", path, exc)
        return False


def entry_key(entry: dict, feed_url: str) -> str:
    raw = entry.get("id") or entry.get("guid") or entry.get("link")
    if not raw:
        raw = "|".join([str(entry.get("title") or ""), str(entry.get("published") or "")])
    if not raw.strip("|"):
        # 无 id/guid/link/标题的条目必须各自有键，否则会全部塌缩成同一个键
        raw = json.dumps(entry, sort_keys=True, default=str)
    return hashlib.sha256(f"{feed_url}::{raw}".encode("utf-8")).hexdigest()[:24]


def entry_text(entry: dict, where: list[str]) -> str:
    parts = []
    for field in ("title", "summary"):
        if field in where:
            parts.append(str(entry.get(field) or ""))
    if "content" in where:
        content = entry.get("content") or []
        if isinstance(content, (list, tuple)):
            for block in content:
                value = block.get("value") if isinstance(block, dict) else block
                parts.append(str(value or ""))
    return "\n".join(part for part in parts if part)


def match_keywords(text: str, feed: dict, defaults: dict) -> tuple[bool, list[str]]:
    case_sensitive = feed.get("case_sensitive", defaults.get("case_sensitive", False))
    haystack = text if case_sensitive else text.lower()

    for word in feed.get("exclude_keywords") or []:
        needle = str(word) if case_sensitive else str(word).lower()
        if needle in haystack:
            return False, []

    hit = []
    for word in feed.get("keywords") or []:
        needle = str(word) if case_sensitive else str(word).lower()
        if needle in haystack:
            hit.append(str(word))
    return bool(hit), hit


def strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    return " ".join(text.split())


def build_message(feed: dict, entry: dict, hits: list[str]) -> tuple[str, str]:
    title = (entry.get("title") or "(无标题)").strip()
    link = entry.get("link") or feed["url"]
    published = entry.get("published") or entry.get("updated") or ""
    summary = strip_html(entry.get("summary", ""))[:200]

    body = [
        f"关键词: {', '.join(hits)}",
        f"来源: {feed['name']}",
        f"链接: {link}",
    ]
    if published:
        body.append(f"时间: {published}")
    if summary:
        body.append(f"摘要: {summary}")
    return title, "\n".join(body)


def send_webhook(session: requests.Session, webhook: dict, title: str, body: str) -> None:
    payload_cfg = webhook.get("payload") or {}
    title_field = payload_cfg.get("title_field", "title")
    text_field = payload_cfg.get("text_field", "content")

    payload = {title_field: title, text_field: body}
    payload.update(payload_cfg.get("extra") or {})

    url = webhook["url"]
    timeout = webhook.get("timeout", 10)
    retries = webhook.get("retries", 3)
    headers = {"Content-Type": "application/json", **(webhook.get("headers") or {})}

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.request(
                webhook.get("method", "POST"),
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
                # 3xx 说明地址不对（常见是被重定向到登录页），跟随后拿到的 200 是假成功
                allow_redirects=False,
            )
            if resp.status_code < 300:
                log.info("推送成功: %s", title)
                return
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as exc:
            last_error = str(exc)

        if attempt < retries:
            delay = 2 ** (attempt - 1) + random.uniform(0, 0.5)
            log.warning("推送失败（第 %d/%d 次）: %s，%.1fs 后重试", attempt, retries, last_error, delay)
            time.sleep(delay)

    raise RuntimeError(f"webhook 推送最终失败: {last_error}")


def check_feed(session: requests.Session, feed: dict, cfg: dict, state: dict, state_path: Path) -> int:
    url = feed["url"]
    try:
        resp = session.get(
            url,
            timeout=cfg.get("request_timeout", 20),
            headers={"User-Agent": cfg.get("user_agent", "rss-monitor/1.0")},
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except requests.RequestException as exc:
        log.error("抓取失败 %s: %s", feed["name"], exc)
        return 0

    if parsed.bozo and not parsed.entries:
        log.error("解析失败 %s: %s", feed["name"], parsed.bozo_exception)
        return 0

    seen_list = list(state.get(url, []))
    is_new_feed = url not in state
    seen = set(seen_list)
    defaults = cfg["defaults"]
    where = feed["match_in"]

    def persist() -> None:
        del seen_list[:-MAX_SEEN_PER_FEED]
        state[url] = seen_list
        save_state(state_path, state)

    # 同批次内相同键（例如多个条目共用一个 link）只保留第一次出现
    fresh: dict[str, dict] = {}
    for entry in parsed.entries:
        key = entry_key(entry, url)
        if key not in seen and key not in fresh:
            fresh[key] = entry

    if is_new_feed and not cfg.get("notify_on_first_run", False):
        if fresh:
            # 首次接入只建立基线，避免历史条目一次性刷屏
            seen_list.extend(fresh.keys())
            persist()
            log.info("首次抓取 %s：记录 %d 条基线，不推送", feed["name"], len(fresh))
        return 0

    notified = 0
    cap = cfg["max_notify_per_feed"]
    for key, entry in reversed(list(fresh.items())):
        if notified >= cap:
            # 剩余条目不写入状态，下一轮继续判定，避免一次性打爆 webhook
            log.warning("%s：本轮已达 %d 条上限，其余留到下一轮", feed["name"], cap)
            break

        text = entry_text(entry, where)
        matched, hits = match_keywords(text, feed, defaults)
        if not matched:
            seen_list.append(key)
            continue

        title, body = build_message(feed, entry, hits)
        log.info("命中 %s: %s", feed["name"], title)
        try:
            send_webhook(session, cfg["webhook"], title, body)
        except RuntimeError as exc:
            # 只有发送成功才记入状态，失败的条目下一轮仍会重试
            log.error("%s：%s", exc, title)
            continue

        seen_list.append(key)
        notified += 1
        persist()

    persist()
    return notified


def run_cycle(session: requests.Session, cfg: dict, state: dict, state_path: Path) -> int:
    total = 0
    for feed in cfg["feeds"]:
        if STOP:
            break
        try:
            total += check_feed(session, feed, cfg, state, state_path)
        except Exception:
            # 单个源的畸形 XML / 解析异常不能终止常驻进程，只跳过这个源
            log.exception("检查 %s 时出现未预期错误，本轮跳过该源", feed.get("name"))
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="RSS 关键词监控 + webhook 推送")
    parser.add_argument("--config", default=Path(__file__).with_name("config.yaml"), type=Path)
    parser.add_argument(
        "--test", action="store_true", help="发送一条测试通知后退出，用于验证 webhook 连通性"
    )
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    # 先配置日志，load_config 的校验警告才不会被 lastResort handler 打散格式
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )
    install_signal_handlers()

    cfg = load_config(args.config)
    logging.getLogger().setLevel(getattr(logging, str(cfg["log_level"]).upper(), logging.INFO))

    state_path = Path(cfg["state_file"])
    if not state_path.is_absolute():
        state_path = args.config.resolve().parent / state_path
    state = load_state(state_path)

    session = requests.Session()
    if args.test:
        try:
            send_webhook(session, cfg["webhook"], "rss-monitor 测试", "这是一条测试通知，配置正常。")
        except Exception as exc:
            log.error("测试通知发送失败: %s", exc)
            return 1
        log.info("测试通知已发送")
        return 0

    log.info(
        "开始监控 %d 个源，间隔 %ss，状态文件 %s",
        len(cfg["feeds"]),
        cfg["poll_interval"],
        state_path,
    )
    while not STOP:
        notified = run_cycle(session, cfg, state, state_path)
        log.info("本轮完成，推送 %d 条", notified)
        for _ in range(cfg["poll_interval"]):
            if STOP:
                break
            time.sleep(1)

    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
