#!/usr/bin/env python3
"""Summarize a Doubao video capture JSONL into upload/completion findings.

Example:
  uv run python scripts/analyze_video_capture.py /tmp/doupool-i2v-network-....jsonl
  uv run python scripts/analyze_video_capture.py /tmp/doupool-i2v-network-....jsonl \\
      --markdown docs/doubao-i2v-ref2v-capture-notes.md
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def walk(obj: Any, path: str = ""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, value
            yield from walk(value, child)
    elif isinstance(obj, list):
        for index, value in enumerate(obj[:50]):
            child = f"{path}[{index}]"
            yield child, value
            yield from walk(value, child)


def find_keys(obj: Any, names: set[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for path, value in walk(obj):
        leaf = path.split(".")[-1].split("[")[0]
        if leaf in names and leaf not in found:
            found[leaf] = value
    return found


def compact(value: Any, limit: int = 1200) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) > limit:
        return text[:limit] + "\n...[TRUNCATED]"
    return text


def looks_like_i2v(body: Any) -> bool:
    blob = json.dumps(body or {}, ensure_ascii=False).lower()
    markers = (
        "image_block",
        "file_block",
        "attachments",
        "image_uri",
        "image_url",
        "tos-cn-",
        "img_uri",
        "upload_id",
        "block_type\":10005",
        "block_type\": 10005",
        "block_type\":10006",
        "block_type\": 10006",
        "block_type\":10010",
        "block_type\": 10010",
    )
    return any(marker in blob for marker in markers)


def looks_like_ref(body: Any) -> bool:
    blob = json.dumps(body or {}, ensure_ascii=False).lower()
    markers = (
        "references",
        "reference",
        "ref_image",
        "ref_list",
        "user_context",
        "skill_type",
        "replica",
        "参考",
    )
    return any(marker in blob for marker in markers)


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    url_counter: Counter[str] = Counter()
    class_counter: Counter[str] = Counter()
    completions: list[dict[str, Any]] = []
    uploads: list[dict[str, Any]] = []
    configs: list[dict[str, Any]] = []
    multimodal: list[dict[str, Any]] = []

    for event in events:
        if event.get("event") not in {"request", "response"}:
            continue
        url = str(event.get("url") or "")
        url_counter[url] += 1
        event_class = str(event.get("class") or "other")
        class_counter[event_class] += 1
        body = event.get("body")

        item = {
            "event": event.get("event"),
            "method": event.get("method"),
            "status": event.get("status"),
            "url": url,
            "class": event_class,
            "content_type": event.get("content_type"),
            "body": body,
            "timestamp": event.get("timestamp"),
        }

        if event_class == "completion" and event.get("event") == "request":
            keys = find_keys(
                body,
                {
                    "chat_ability",
                    "ability_type",
                    "ability_param",
                    "content_block",
                    "attachments",
                    "references",
                    "user_context",
                    "messages",
                    "client_meta",
                    "option",
                    "ext",
                },
            )
            item["extracted"] = keys
            item["guess_i2v"] = looks_like_i2v(body)
            item["guess_ref"] = looks_like_ref(body)
            completions.append(item)
        elif event_class == "upload":
            uploads.append(item)
        elif event_class == "config" and event.get("event") == "response":
            configs.append(item)
        elif event_class in {"multimodal", "skill"}:
            multimodal.append(item)
        elif event.get("event") == "request" and looks_like_i2v(body):
            multimodal.append(item)

    return {
        "total_events": len(events),
        "url_top": url_counter.most_common(40),
        "class_counts": class_counter.most_common(),
        "completions": completions,
        "uploads": uploads,
        "configs": configs[:10],
        "multimodal": multimodal,
        "i2v_completions": [c for c in completions if c.get("guess_i2v")],
        "ref_completions": [c for c in completions if c.get("guess_ref")],
    }


def to_markdown(path: Path, summary: dict[str, Any]) -> str:
    lines: list[str] = [
        f"# Doubao 视频抓包分析（自动生成）",
        "",
        f"来源：`{path}`",
        f"总事件：{summary['total_events']}",
        "",
        "## 事件分类",
        "",
    ]
    for name, count in summary["class_counts"]:
        lines.append(f"- `{name}`: {count}")

    lines += ["", "## 高频 URL", ""]
    for url, count in summary["url_top"][:25]:
        lines.append(f"- `{count}` {url}")

    lines += ["", f"## Completion 请求（{len(summary['completions'])}）", ""]
    if not summary["completions"]:
        lines.append("_未捕获到 /chat/completion 类请求。_")
    for index, item in enumerate(summary["completions"], 1):
        flags = []
        if item.get("guess_i2v"):
            flags.append("疑似图生")
        if item.get("guess_ref"):
            flags.append("疑似参考")
        flag_text = f" ({', '.join(flags)})" if flags else ""
        lines += [
            f"### Completion #{index}{flag_text}",
            "",
            f"- method: `{item.get('method')}`",
            f"- url: `{item.get('url')}`",
            "",
            "```json",
            compact(item.get("body")),
            "```",
            "",
        ]

    lines += ["", f"## Upload 请求（{len(summary['uploads'])}）", ""]
    if not summary["uploads"]:
        lines.append("_未捕获到明确的 upload/imagex 请求。可能图片走了其他资源域名，或上传发生在本次会话之前。_")
    for index, item in enumerate(summary["uploads"], 1):
        lines += [
            f"### Upload #{index}",
            "",
            f"- event: `{item.get('event')}` status: `{item.get('status')}`",
            f"- url: `{item.get('url')}`",
            f"- content-type: `{item.get('content_type')}`",
            "",
            "```json",
            compact(item.get("body")),
            "```",
            "",
        ]

    lines += ["", f"## 其他多模态 / skill 事件（{len(summary['multimodal'])}）", ""]
    if not summary["multimodal"]:
        lines.append("_无额外 multimodal/skill 事件。_")
    for index, item in enumerate(summary["multimodal"][:20], 1):
        lines += [
            f"### Multimodal #{index}",
            "",
            f"- `{item.get('event')}` `{item.get('method')}` `{item.get('url')}`",
            "",
            "```json",
            compact(item.get("body"), limit=800),
            "```",
            "",
        ]

    lines += [
        "",
        "## 实现提示",
        "",
        "1. 对比文生视频 payload，重点看 `messages[].content_block` 是否出现 image/file block。",
        "2. 查看 `chat_ability.ability_type/ability_param` 是否变化。",
        "3. 还原图床上传：apply/commit/upload 的字段名、返回 uri、后续如何写回 completion。",
        "4. 参考生视频可能把素材放在 `attachments`、`references` 或 `user_context`。",
        "5. 敏感字段已脱敏；实现时仍应在 Playwright 页面上下文发请求，不要硬编码风控参数。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Doubao video capture JSONL.")
    parser.add_argument("capture", type=Path, help="Path to capture .jsonl")
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="Optional markdown report output path.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Optional machine-readable summary JSON path.",
    )
    args = parser.parse_args()

    events = load_events(args.capture)
    summary = summarize(events)

    print(f"file: {args.capture}")
    print(f"total_events: {summary['total_events']}")
    print("classes:", dict(summary["class_counts"]))
    print(f"completions: {len(summary['completions'])}")
    print(f"  guessed i2v: {len(summary['i2v_completions'])}")
    print(f"  guessed ref: {len(summary['ref_completions'])}")
    print(f"uploads: {len(summary['uploads'])}")
    print(f"multimodal/skill: {len(summary['multimodal'])}")
    print("top urls:")
    for url, count in summary["url_top"][:15]:
        print(f"  {count:4d} {url}")

    if summary["completions"]:
        print("\n--- completion ability snapshot ---")
        for item in summary["completions"]:
            extracted = item.get("extracted") or {}
            ability = extracted.get("chat_ability") or {
                "ability_type": extracted.get("ability_type"),
                "ability_param": extracted.get("ability_param"),
            }
            print(json.dumps({
                "url": item.get("url"),
                "guess_i2v": item.get("guess_i2v"),
                "guess_ref": item.get("guess_ref"),
                "chat_ability": ability,
            }, ensure_ascii=False))

    md = to_markdown(args.capture, summary)
    md_path = args.markdown or args.capture.with_suffix(".md")
    md_path.write_text(md, encoding="utf-8")
    print(f"\nmarkdown report: {md_path}")

    if args.json:
        # Avoid dumping huge bodies twice if not needed; keep full summary.
        args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json summary: {args.json}")


if __name__ == "__main__":
    main()
