from __future__ import annotations

from hashlib import sha1
import json
from pathlib import Path
import re
from typing import Any


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
MULTISPACE = re.compile(r"\s+")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def prefer_openai_id(*candidates: object) -> str | None:
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def stable_hash_payload(payload: Any, *, prefix: str, length: int = 16) -> str:
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}_{sha1(normalized.encode('utf-8')).hexdigest()[:length]}"


def build_conversation_id(conversation: dict[str, Any], fallback_index: int) -> str:
    preferred = prefer_openai_id(
        conversation.get("conversation_id"),
        conversation.get("id"),
        conversation.get("conversationId"),
    )
    if preferred is not None:
        return preferred

    fallback_payload = {
        "title": conversation.get("title") or conversation.get("name"),
        "create_time": conversation.get("create_time") or conversation.get("created_at"),
        "update_time": conversation.get("update_time") or conversation.get("updated_at"),
        "current_node": conversation.get("current_node"),
        "mapping_keys": sorted(
            key for key in (conversation.get("mapping") or {}).keys() if isinstance(key, str)
        ),
        "fallback_index": fallback_index,
    }
    return stable_hash_payload(fallback_payload, prefix="conv")


def build_message_id(
    message: dict[str, Any],
    *,
    conversation_id: str,
    fallback_index: int,
    node_id: str | None = None,
) -> str:
    preferred = prefer_openai_id(message.get("id"), node_id)
    if preferred is not None:
        return preferred

    fallback_payload = {
        "conversation_id": conversation_id,
        "fallback_index": fallback_index,
        "author": message.get("author"),
        "create_time": message.get("create_time"),
        "content": message.get("content"),
    }
    return stable_hash_payload(fallback_payload, prefix="msg")


def build_resource_id(
    *,
    asset_id: str | None,
    source_dat_name: str | None,
    conversation_id: str,
    message_id: str,
    resource_index: int,
    sha256: str | None,
) -> str:
    preferred = prefer_openai_id(asset_id)
    if preferred is not None:
        return preferred

    if isinstance(source_dat_name, str) and source_dat_name.startswith("file_") and source_dat_name.endswith(".dat"):
        return source_dat_name[:-4]

    fallback_payload = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "resource_index": resource_index,
        "sha256": sha256,
        "source_dat_name": source_dat_name,
    }
    return stable_hash_payload(fallback_payload, prefix="res")


def slugify(value: str, *, limit: int = 80) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", value or "")
    cleaned = "".join(char for char in cleaned if ord(char) >= 32)
    cleaned = MULTISPACE.sub(" ", cleaned).strip().rstrip(". ")
    cleaned = re.sub(r"_+", "_", cleaned).strip(" _.-")
    if not cleaned:
        cleaned = "untitled"
    cleaned = cleaned[:limit].rstrip(" ._-") or "untitled"
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"{cleaned}_"
    return cleaned


def sanitize_filename(value: str, *, limit: int = 180) -> str:
    path = Path(value)
    stem = slugify(path.stem or value, limit=max(1, limit))
    suffix = path.suffix
    max_stem_length = max(1, limit - len(suffix))
    stem = stem[:max_stem_length].rstrip(" ._-") or "file"
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"{stem}_"
    safe_suffix = INVALID_FILENAME_CHARS.sub("_", suffix)
    return f"{stem}{safe_suffix}".rstrip(". ") or "file"


def build_conversation_filename(conversation_id: str, title: str | None) -> str:
    title_slug = slugify(title or "conversation", limit=96)
    return sanitize_filename(f"{title_slug}.md")


def build_resource_filename(
    *,
    stable_resource_id: str,
    original_name: str | None,
    fallback_extension: str,
) -> str:
    base_name = original_name.strip() if isinstance(original_name, str) and original_name.strip() else stable_resource_id
    base_path = Path(base_name)
    suffix = base_path.suffix or fallback_extension
    stem = slugify(base_path.stem or stable_resource_id, limit=24)
    safe_id = slugify(stable_resource_id, limit=40)
    if stem and stem != safe_id:
        return sanitize_filename(f"{safe_id}__{stem}{suffix}", limit=80)
    return sanitize_filename(f"{safe_id}{suffix}", limit=80)
