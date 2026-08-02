#!/usr/bin/env python3
"""Capture sanitized Doubao network traffic for text / image / reference video flows.

Usage examples:

  # image-to-video capture (default output tagged by mode)
  uv run python scripts/capture_doubao_video.py --mode i2v

  # reference-to-video
  uv run python scripts/capture_doubao_video.py --mode ref2v

  # reuse an existing logged-in Chromium profile
  uv run python scripts/capture_doubao_video.py \\
    --mode i2v \\
    --profile /tmp/doupool-login-capture-profile

In the opened Chromium window:
  1. Confirm you are logged into Doubao.
  2. Open video generation UI.
  3. For i2v: upload 1 image and generate.
  4. For ref2v: add reference image(s)/assets and generate.
  5. Ctrl+C when finished.

The script redacts cookies/tokens/fp/a_bogus. Binary upload bodies are summarized
by field name / filename / size only.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Request, Response, sync_playwright


SENSITIVE_PARTS = (
    "token",
    "cookie",
    "authorization",
    "signature",
    "a_bogus",
    "ms_token",
    "mstoken",
    "verify",
    "captcha",
    "password",
    "phone",
    "mobile",
    "user_id",
    "sec_user",
    "web_id",
    "ttwid",
    "sessionid",
    "sid_tt",
    "sid_guard",
)
STATIC_SUFFIXES = (
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".map",
    ".png",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
)

# Paths/hosts that matter for video + multimodal upload analysis.
HIGHLIGHT_PATTERNS = (
    r"/chat/completion",
    r"/samantha/chat/completion",
    r"/im/chain/single",
    r"/im/conversation/info",
    r"get_item_conf",
    r"/upload",
    r"/imagex",
    r"/imagex",
    r"apply_upload",
    r"commit_upload",
    r"/resource/",
    r"/file/",
    r"/asset",
    r"/media",
    r"/attachment",
    r"skill/pack",
    r"action_bar",
    r"creation",
    r"seedance",
)

MODE_LABELS = {
    "t2v": "文生视频",
    "i2v": "图生视频",
    "ref2v": "参考生视频",
    "all": "全部视频模式",
}


def is_sensitive(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return lowered == "fp" or any(part in lowered for part in SENSITIVE_PARTS)


def sanitize(value: object, key: str = "", depth: int = 0) -> object:
    if key and is_sensitive(key):
        return "[REDACTED]"
    if depth > 10:
        return "[MAX_DEPTH]"
    if isinstance(value, dict):
        return {
            str(item_key): sanitize(item, str(item_key), depth + 1)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item, key, depth + 1) for item in value[:200]]
    if isinstance(value, str):
        if value.startswith("data:image"):
            return f"[DATA_URL image; len={len(value)}]"
        if len(value) > 12_000:
            return value[:12_000] + "...[TRUNCATED]"
        # Redact long base64-looking blobs that often carry image bytes.
        if len(value) > 400 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value or ""):
            return f"[BASE64_BLOB; len={len(value)}]"
    return value


def sanitize_query(url: str) -> tuple[str, dict[str, object]]:
    parsed = urlparse(url)
    query: dict[str, object] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        clean_value = "[REDACTED]" if is_sensitive(key) else value
        if key in query:
            current = query[key]
            query[key] = [*current, clean_value] if isinstance(current, list) else [current, clean_value]
        else:
            query[key] = clean_value
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}", query


def summarize_multipart(raw: str, content_type: str) -> dict[str, object]:
    boundary_match = re.search(r"boundary=(.+)", content_type, flags=re.I)
    if not boundary_match:
        return {"type": "multipart", "note": "missing boundary", "size": len(raw)}
    boundary = boundary_match.group(1).strip().strip('"')
    parts = raw.split(f"--{boundary}")
    fields: list[dict[str, object]] = []
    for part in parts:
        if not part or part in ("--", "--\r\n", "--\n"):
            continue
        header_blob, _, body = part.partition("\r\n\r\n")
        if not body and "\n\n" in part:
            header_blob, _, body = part.partition("\n\n")
        disposition = ""
        part_type = ""
        for line in header_blob.splitlines():
            lower = line.lower()
            if lower.startswith("content-disposition:"):
                disposition = line.split(":", 1)[1].strip()
            elif lower.startswith("content-type:"):
                part_type = line.split(":", 1)[1].strip()
        name_match = re.search(r'name="([^"]+)"', disposition)
        file_match = re.search(r'filename="([^"]*)"', disposition)
        body_clean = body.rstrip("\r\n-")
        field: dict[str, object] = {
            "name": name_match.group(1) if name_match else "",
            "content_type": part_type,
            "size": len(body_clean.encode("utf-8", errors="ignore")),
        }
        if file_match is not None:
            field["filename"] = file_match.group(1)
            field["kind"] = "file"
        else:
            field["kind"] = "field"
            # Keep small text fields; redact large ones.
            if len(body_clean) <= 500 and not is_sensitive(field["name"]):
                field["value"] = body_clean
            elif is_sensitive(str(field["name"])):
                field["value"] = "[REDACTED]"
            else:
                field["value"] = f"[OMITTED; len={len(body_clean)}]"
        fields.append(field)
    return {"type": "multipart", "field_count": len(fields), "fields": fields}


def sanitize_post_data(request: Request) -> object | None:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        raw = request.post_data or ""
        return summarize_multipart(raw, content_type)

    try:
        payload = request.post_data_json
    except Exception:
        payload = None
    if payload is not None:
        return sanitize(payload)

    raw = request.post_data
    if not raw:
        return None
    if "application/x-www-form-urlencoded" in content_type:
        return sanitize(dict(parse_qsl(raw, keep_blank_values=True)))
    if raw.startswith("{") or raw.startswith("["):
        try:
            return sanitize(json.loads(raw))
        except Exception:
            pass
    if len(raw) > 4_000:
        return {"type": "raw", "size": len(raw), "preview": sanitize(raw[:1_000])}
    return sanitize(raw)


def should_capture(request: Request) -> bool:
    if request.resource_type not in {"fetch", "xhr"}:
        return False
    parsed = urlparse(request.url)
    path = parsed.path.lower()
    if path.endswith(STATIC_SUFFIXES):
        return False
    hostname = (parsed.hostname or "").lower()
    if hostname in {"mcs.doubao.com", "opt.doubao.com"}:
        return False
    if any(marker in path for marker in ("/monitor_", "/slardar/", "/collect/", "/log/", "/tea/")):
        return False
    interesting_host = any(
        marker in hostname
        for marker in (
            "doubao",
            "byte",
            "volc",
            "byted",
            "tos",
            "imagex",
            "byteimg",
            "jianying",
            "capcut",
            "faceu",
        )
    )
    return interesting_host


def is_highlight(url: str, body: object | None = None) -> bool:
    target = url.lower()
    if any(re.search(pattern, target) for pattern in HIGHLIGHT_PATTERNS):
        return True
    if body is None:
        return False
    try:
        blob = json.dumps(body, ensure_ascii=False).lower()
    except Exception:
        blob = str(body).lower()
    markers = (
        "chat_ability",
        "ability_type",
        "ability_param",
        "image_block",
        "file_block",
        "content_block",
        "attachments",
        "references",
        "upload",
        "seedance",
        "block_type",
        "creation_block",
    )
    return any(marker in blob for marker in markers)


def classify_event(url: str, body: object | None = None) -> str:
    lower = url.lower()
    if "completion" in lower:
        return "completion"
    if any(k in lower for k in ("upload", "imagex", "imagex", "apply_upload", "commit_upload")):
        return "upload"
    if "get_item_conf" in lower or "action_bar" in lower:
        return "config"
    if "/im/chain" in lower or "/im/conversation" in lower:
        return "im"
    if "skill" in lower:
        return "skill"
    if body is not None:
        try:
            blob = json.dumps(body, ensure_ascii=False).lower()
        except Exception:
            blob = ""
        if "chat_ability" in blob or "ability_type" in blob:
            return "completion"
        if "image_block" in blob or "file_block" in blob or "attachments" in blob:
            return "multimodal"
    return "other"


def response_payload(response: Response) -> object | None:
    content_type = response.headers.get("content-type", "")
    # SSE / text event streams: keep a short sanitized preview.
    if "text/event-stream" in content_type or "text/plain" in content_type:
        try:
            text = response.text()
        except Exception:
            return None
        preview = text[:8_000]
        # Strip obvious tokens if present.
        preview = re.sub(r"(msToken|a_bogus|fp)=[^&\s]+", r"\1=[REDACTED]", preview)
        return {
            "type": "text",
            "content_type": content_type,
            "size": len(text),
            "preview": preview + ("...[TRUNCATED]" if len(text) > 8_000 else ""),
        }
    if "json" not in content_type:
        return {
            "type": "non_json",
            "content_type": content_type,
            "size": int(response.headers.get("content-length") or 0) or None,
        }
    try:
        return sanitize(response.json())
    except Exception:
        try:
            text = response.text()
            return {"type": "json_text", "preview": sanitize(text[:4_000])}
        except Exception:
            return None


def default_output(mode: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Path(f"/tmp/doupool-{mode}-network-{stamp}.jsonl")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture sanitized Doubao text/image/reference video API traffic.",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path("/tmp/doupool-login-capture-profile"),
        help="Chromium persistent profile with an authorized Doubao login.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="JSONL output path. Defaults to /tmp/doupool-<mode>-network-<timestamp>.jsonl",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(MODE_LABELS),
        default="all",
        help="Capture session label used in logs and default filename.",
    )
    parser.add_argument(
        "--start-url",
        default="https://www.doubao.com/chat/",
        help="Initial Doubao page to open.",
    )
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument(
        "--window-width",
        type=int,
        default=1500,
        help="Chromium window width in CSS pixels.",
    )
    parser.add_argument(
        "--window-height",
        type=int,
        default=980,
        help="Chromium window height in CSS pixels.",
    )
    parser.add_argument(
        "--window-x",
        type=int,
        default=40,
        help="Chromium window X position.",
    )
    parser.add_argument(
        "--window-y",
        type=int,
        default=30,
        help="Chromium window Y position.",
    )
    parser.add_argument(
        "--maximize",
        action="store_true",
        help="Start maximized and let the page use the full window (recommended on small displays).",
    )
    parser.add_argument(
        "--highlight-only-console",
        action="store_true",
        help="Only print high-signal events to stdout (all events still written to file).",
    )
    args = parser.parse_args()
    output = args.output or default_output(args.mode)
    output.unlink(missing_ok=True)
    mode_label = MODE_LABELS[args.mode]

    def write(event: dict[str, object]) -> None:
        event = {"mode": args.mode, "mode_label": mode_label, **event}
        with output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

        kind = str(event.get("class") or event.get("event") or "")
        highlighted = bool(event.get("highlight"))
        if args.highlight_only_console and event.get("event") in {"request", "response"} and not highlighted:
            return
        print(
            json.dumps(
                {
                    key: event[key]
                    for key in (
                        "event",
                        "class",
                        "method",
                        "status",
                        "url",
                        "highlight",
                        "mode",
                        "output",
                        "page",
                        "hint",
                    )
                    if key in event
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if kind in {"completion", "upload", "multimodal"} and event.get("event") == "request":
            body = event.get("body")
            if body is not None:
                preview = json.dumps(body, ensure_ascii=False)
                if len(preview) > 900:
                    preview = preview[:900] + "...[TRUNCATED]"
                print(f"  body: {preview}", flush=True)

    print(
        f"[capture] mode={args.mode} ({mode_label}) profile={args.profile} output={output}",
        flush=True,
    )
    print(
        "[capture] steps: login if needed → open video UI → run 图生视频 and/or 参考生视频 → Ctrl+C",
        flush=True,
    )

    with sync_playwright() as playwright:
        # Important: a fixed small viewport crops Doubao's bottom composer.
        # Use the real window size (no_viewport) so input boxes stay visible.
        launch_args = [
            f"--window-position={args.window_x},{args.window_y}",
            "--disable-infobars",
        ]
        if args.maximize:
            launch_args.append("--start-maximized")
        else:
            launch_args.append(f"--window-size={args.window_width},{args.window_height}")

        context = playwright.chromium.launch_persistent_context(
            str(args.profile),
            headless=False,
            no_viewport=True,
            args=launch_args,
            accept_downloads=True,
            ignore_default_args=["--enable-automation"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        # If the restored window is still too short, enlarge via CDP.
        try:
            width = max(args.window_width, 1280)
            height = max(args.window_height, 900)
            session = context.new_cdp_session(page)
            target = session.send("Browser.getWindowForTarget")
            window_id = target.get("windowId")
            if window_id is not None:
                bounds = {
                    "left": args.window_x,
                    "top": args.window_y,
                    "width": width,
                    "height": height,
                }
                if args.maximize:
                    session.send(
                        "Browser.setWindowBounds",
                        {"windowId": window_id, "bounds": {"windowState": "maximized"}},
                    )
                else:
                    session.send(
                        "Browser.setWindowBounds",
                        {"windowId": window_id, "bounds": bounds},
                    )
        except Exception as exc:  # pragma: no cover - best-effort UI fix
            print(f"[capture] window resize skipped: {exc}", flush=True)

        def on_request(request: Request) -> None:
            if not should_capture(request):
                return
            base_url, query = sanitize_query(request.url)
            body = sanitize_post_data(request)
            event_class = classify_event(base_url, body)
            write(
                {
                    "event": "request",
                    "timestamp": time.time(),
                    "class": event_class,
                    "highlight": is_highlight(base_url, body),
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "url": base_url,
                    "query": query,
                    "content_type": request.headers.get("content-type", ""),
                    "body": body,
                }
            )

        def on_response(response: Response) -> None:
            request = response.request
            if not should_capture(request):
                return
            base_url, query = sanitize_query(response.url)
            body = response_payload(response)
            event_class = classify_event(base_url, body)
            write(
                {
                    "event": "response",
                    "timestamp": time.time(),
                    "class": event_class,
                    "highlight": is_highlight(base_url, body),
                    "method": request.method,
                    "status": response.status,
                    "url": base_url,
                    "query": query,
                    "content_type": response.headers.get("content-type", ""),
                    "body": body,
                }
            )

        context.on("request", on_request)
        context.on("response", on_response)
        page.goto(args.start_url, wait_until="domcontentloaded", timeout=45_000)
        write(
            {
                "event": "browser_ready",
                "output": str(output),
                "page": page.url,
                "hint": (
                    "请在窗口内完成：1) 图生视频（上传 1 张图+提示词）"
                    " 2) 参考生视频（上传参考图/素材+提示词）。完成后 Ctrl+C。"
                ),
            }
        )

        deadline = time.monotonic() + args.seconds
        try:
            while time.monotonic() < deadline and not page.is_closed():
                page.wait_for_timeout(250)
        except (KeyboardInterrupt, PlaywrightError):
            pass

        write({"event": "capture_finished", "output": str(output)})
        context.close()
        print(f"[capture] finished → {output}", flush=True)
        print(
            f"[capture] analyze with: uv run python scripts/analyze_video_capture.py {output}",
            flush=True,
        )


if __name__ == "__main__":
    main()
