from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.state_store import FileStateStore

ALLOWED_ATTACHMENT_SUFFIXES = {".txt", ".md", ".json"}
MAX_ATTACHMENTS_PER_MESSAGE = 3
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
MAX_MESSAGE_LENGTH = 2000


async def parse_message_inputs(message: Any) -> dict[str, Any]:
    attachments = list(message.attachments)
    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        return {
            "error": f"添付は最大{MAX_ATTACHMENTS_PER_MESSAGE}件までです。必要なファイルだけ再送してください。",
            "body": "",
            "attachments": [],
        }

    parsed_attachments: list[dict[str, str]] = []
    for attachment in attachments:
        suffix = Path(attachment.filename).suffix.lower()
        if suffix not in ALLOWED_ATTACHMENT_SUFFIXES:
            allowed = ", ".join(sorted(ALLOWED_ATTACHMENT_SUFFIXES))
            return {
                "error": (f"`{attachment.filename}` は非対応形式です。 {allowed} のいずれかにして再送してください。"),
                "body": "",
                "attachments": [],
            }
        if attachment.size > MAX_ATTACHMENT_BYTES:
            return {
                "error": (f"`{attachment.filename}` はサイズ上限を超えています。 2MB 以下にして再送してください。"),
                "body": "",
                "attachments": [],
            }
        raw = await attachment.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        parsed_attachments.append(
            {
                "filename": attachment.filename,
                "content": text,
                "url": attachment.url,
            }
        )

    content = str(getattr(message, "content", "")).strip()
    body_parts: list[str] = []
    if content:
        body_parts.append(content)
    for item in parsed_attachments:
        body_parts.append(
            "\n".join(
                [
                    f"[attachment:{item['filename']}]",
                    item["content"],
                    f"[/attachment:{item['filename']}]",
                ]
            )
        )
    return {
        "error": "",
        "body": "\n\n".join(part for part in body_parts if part.strip()),
        "attachments": parsed_attachments,
    }


def ensure_new_thread_body(parsed: dict[str, Any]) -> dict[str, Any]:
    body = str(parsed["body"]).strip()
    if body:
        return parsed
    updated = dict(parsed)
    updated["error"] = "本文か対応添付ファイルが必要です。`txt` `md` `json` を最大3件、各2MB以内で再送してください。"
    return updated


def materialize_message_payload(
    *,
    thread_id: int,
    message_id: int,
    parsed: dict[str, Any],
    state_store: FileStateStore,
) -> str:
    attachments = parsed.get("attachments", [])
    materialized: list[dict[str, str]] = []
    for item in attachments:
        safe_name = safe_attachment_name(message_id, str(item["filename"]))
        saved_path = state_store.write_attachment_text(thread_id, safe_name, str(item["content"]))
        materialized.append(
            {
                "filename": str(item["filename"]),
                "saved_path": saved_path,
                "url": str(item["url"]),
            }
        )
    payload = str(parsed.get("body", "")).strip()
    if materialized:
        payload += "\n\n[attachment-metadata]\n" + json.dumps({"items": materialized}, ensure_ascii=False, indent=2)
    return payload.strip()


def safe_attachment_name(message_id: int, filename: str) -> str:
    suffix = Path(filename).suffix
    stem = Path(filename).stem
    sanitized = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in stem)[:80] or "attachment"
    return f"{message_id}_{sanitized}{suffix}"


def chunk_message(content: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    if len(content) <= max_length:
        return [content]
    chunks: list[str] = []
    remaining = content
    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length)
        if split_at <= 0:
            split_at = max_length
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks or [""]
