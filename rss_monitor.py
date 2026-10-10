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


def _handle_stop(signum, frame):
    global STOP
    STOP = True
    log.info("收到信号 %s，本轮结束后退出", signum)


signal.signal(signal.SIGINT, _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise SystemExit(f"配置文件格式错误: {path}")

    webhook = cfg.get("webhook") or {}
    if not webhook.get("url"):
        raise SystemExit("配置缺少 webhook.url")

    defaults = cfg.get("defaults") or {}
    if not defaults.get("keywords"):
        log.warning("defaults.keywords 为空，只有 feed 自带 keywords 的源会产生通知")

    feeds = cfg.get("feeds") or []
    if not feeds:
        raise SystemExit("配置里没有任何 feeds")

    for feed in feeds:
        if not feed.get("url") or not feed.get("name"):
            raise SystemExit(f"feed 必须同时有 name 和 url: {feed}")
        # 未声明关键词的源回落到全局关键词
        feed.setdefault("keywords", defaults.get("keywords", []))
        feed.setdefault("exclude_keywords", defaults.get("exclude_keywords", []))

    cfg["defaults"] = defaults
    cfg["webhook"] = webhook
    cfg.setdefault("poll_interval", 300)
    cfg.setdefault("state_file", "state.json")
    cfg.setdefault("log_level", "INFO")
    cfg.setdefault("notify_on_first_run", False)
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


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def entry_key(entry: dict, feed_url: str) -> str:
    raw = entry.get("id") or entry.get("guid") or entry.get("link")
    if not raw:
        raw = "|".join([entry.get("title", ""), entry.get("published", "")])
    if not raw:
        raw = json.dumps(entry, sort_keys=True, default=str)
    return hashlib.sha256(f"{feed_url}::{raw}".encode("utf-8")).hexdigest()[:24]


def entry_text(entry: dict, where: list[str]) -> str:
    parts = []
    if "title" in where:
        parts.append(entry.get("title", ""))
    if "summary" in where:
        parts.append(entry.get("summary", ""))
    if "content" in where:
        for block in entry.get("content", []) or []:
            parts.append(block.get("value", ""))
    return "\n".join(part for part in parts if part)


def match_keywords(text: str, feed: dict, defaults: dict) -> tuple[bool, list[str]]:
    case_sensitive = feed.get("case_sensitive", defaults.get("case_sensitive", False))
    haystack = text if case_sensitive else text.lower()

    for word in feed.get("exclude_keywords") or []:
        needle = word if case_sensitive else word.lower()
        if needle in haystack:
            return False, []

    hit = []
    for word in feed.get("keywords") or []:
        needle = word if case_sensitive else word.lower()
        if needle in haystack:
            hit.append(word)
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
    where = feed.get("match_in", defaults.get("match_in", ["title", "summary"]))

    def persist() -> None:
        del seen_list[:-MAX_SEEN_PER_FEED]
        state[url] = seen_list
        save_state(state_path, state)

    fresh = [entry for entry in parsed.entries if entry_key(entry, url) not in seen]
    if is_new_feed and not cfg.get("notify_on_first_run", False):
        # 首次接入只建立基线，避免历史条目一次性刷屏
        for entry in fresh:
            seen_list.append(entry_key(entry, url))
        persist()
        log.info("首次抓取 %s：记录 %d 条基线，不推送", feed["name"], len(fresh))
        return 0

    notified = 0
    for entry in reversed(fresh):
        key = entry_key(entry, url)
        seen_list.append(key)
        seen.add(key)
        persist()

        text = entry_text(entry, where)
        matched, hits = match_keywords(text, feed, defaults)
        if not matched:
            continue

        title, body = build_message(feed, entry, hits)
        log.info("命中 %s: %s", feed["name"], title)
        try:
            send_webhook(session, cfg["webhook"], title, body)
            notified += 1
        except RuntimeError as exc:
            log.error("%s（该条目下次仍会重试）", exc)
            seen_list.remove(key)
            seen.discard(key)
            persist()

    persist()
    return notified


def run_cycle(session: requests.Session, cfg: dict, state: dict, state_path: Path) -> int:
    total = 0
    for feed in cfg["feeds"]:
        if STOP:
            break
        total += check_feed(session, feed, cfg, state, state_path)
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

    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, str(cfg["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    state_path = Path(cfg["state_file"])
    if not state_path.is_absolute():
        state_path = args.config.resolve().parent / state_path
    state = load_state(state_path)

    session = requests.Session()
    if args.test:
        try:
            send_webhook(session, cfg["webhook"], "rss-monitor 测试", "这是一条测试通知，配置正常。")
        except RuntimeError as exc:
            log.error("%s", exc)
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
        for _ in range(int(cfg["poll_interval"])):
            if STOP:
                break
            time.sleep(1)

    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
