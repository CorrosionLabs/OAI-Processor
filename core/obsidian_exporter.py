from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from core.ids import build_conversation_filename, slugify


def export_conversation(
    conversation: dict[str, Any],
    project_slug: str,
    output_root: Path,
    resource_map: dict[str, str],
    include_project_tag: bool = False,
    custom_roles: bool = False,
    user_role: str = "Usuario",
    assistant_role: str = "Asistente",
) -> tuple[Path, set[str], list[dict[str, Any]]]:
    conversation_id = str(conversation.get("conversation_id") or "conversation")
    title = str(conversation.get("title") or "Sin título").strip() or "Sin título"
    project_dir = output_root / slugify(project_slug or "unassigned", limit=64)
    project_dir.mkdir(parents=True, exist_ok=True)
    clean_filename = build_conversation_filename(conversation_id, title)
    output_path = _conversation_output_path(project_dir, clean_filename, conversation_id)
    markdown, references, missing_resources = _render_markdown(
        conversation,
        project_slug,
        resource_map,
        include_project_tag=include_project_tag,
        custom_roles=custom_roles,
        user_role=user_role,
        assistant_role=assistant_role,
    )
    output_path.write_text(markdown, encoding="utf-8")
    return output_path, references, missing_resources


def _conversation_output_path(project_dir: Path, clean_filename: str, conversation_id: str) -> Path:
    existing_by_name: dict[str, list[Path]] = {}
    for path in project_dir.iterdir():
        existing_by_name.setdefault(path.name.casefold(), []).append(path)

    clean_path = Path(clean_filename)
    stem = clean_path.stem
    suffix = clean_path.suffix
    number = 1
    while True:
        filename = clean_filename if number == 1 else f"{stem}__{number}{suffix}"
        matches = existing_by_name.get(filename.casefold(), [])
        if not matches:
            return project_dir / filename
        for existing in matches:
            if (
                existing.is_file()
                and not existing.is_symlink()
                and _markdown_conversation_id(existing) == conversation_id
            ):
                return existing
        number = 2 if number == 1 else number + 1


def _markdown_conversation_id(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as markdown_file:
            if markdown_file.readline().strip() != "---":
                return None
            for line in markdown_file:
                line = line.strip()
                if line == "---":
                    return None
                if line.startswith("conversation_id:"):
                    try:
                        value = json.loads(line.partition(":")[2].strip())
                    except json.JSONDecodeError:
                        return None
                    return str(value) if value is not None else ""
    except (OSError, UnicodeError):
        return None
    return None


def _render_markdown(
    conversation: dict[str, Any],
    project_slug: str,
    resource_map: dict[str, str],
    include_project_tag: bool = False,
    custom_roles: bool = False,
    user_role: str = "Usuario",
    assistant_role: str = "Asistente",
) -> tuple[str, set[str], list[dict[str, Any]]]:
    title = str(conversation.get("title") or "Sin título").strip() or "Sin título"
    lines = [
        "---",
        f"title: {_yaml_string(title)}",
        f"conversation_id: {_yaml_string(conversation.get('conversation_id'))}",
        f"openai_conversation_id: {_yaml_string(conversation.get('openai_conversation_id'))}",
        f"project: {_yaml_string(project_slug or 'unassigned')}",
        f"created_at: {_yaml_string(conversation.get('created_at'))}",
        f"updated_at: {_yaml_string(conversation.get('updated_at'))}",
        f"model: {_yaml_string(conversation.get('model'))}",
    ]
    if include_project_tag:
        project_tag = re.sub(r"\s+", "_", project_slug.strip().lower()).replace("#", "")
        lines.extend(("tags:", f"  - {project_tag}"))
    lines.extend([
        "---",
        "",
        f"# {title}",
        "",
    ])
    references: set[str] = set()
    missing_resources: list[dict[str, Any]] = []
    messages = conversation.get("messages")
    if not isinstance(messages, list):
        messages = []

    role_names = {
        "user": user_role if custom_roles else "Usuario",
        "assistant": assistant_role if custom_roles else "Asistente",
        "system": "Sistema",
        "tool": "Herramienta",
    }
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown").lower()
        metadata = message.get("metadata")
        if isinstance(metadata, dict) and metadata.get("command") == "context_stuff":
            continue
        resources = message.get("resources")
        if role == "tool":
            resources = [
                resource
                for resource in resources
                if isinstance(resource, dict) and resource.get("kind") == "image"
            ] if isinstance(resources, list) else []
        text = "" if role == "tool" else message.get("text")
        message_text = text.strip() if isinstance(text, str) else ""
        if not message_text:
            parts = message.get("parts")
            if isinstance(parts, list):
                message_text = "\n\n".join(
                    part["text"].strip()
                    for part in parts
                    if isinstance(part, dict)
                    and isinstance(part.get("text"), str)
                    and part["text"].strip()
                )
        message_text = _clean_exported_message_text(message_text)
        has_resources = isinstance(resources, list) and any(
            isinstance(resource, dict) for resource in resources
        )
        if not message_text and not has_resources:
            continue

        if role != "tool":
            lines.append(f"## {role_names.get(role, role.capitalize())}")
            created_at = message.get("created_at")
            if isinstance(created_at, str) and created_at.strip():
                lines.extend(("", f"*{created_at.strip()}*"))
        if message_text and role != "tool":
            is_code = isinstance(metadata, dict) and metadata.get("content_type") == "code"
            is_html_document = (
                not is_code
                and message_text.lstrip().lower().startswith(("<!doctype html>", "<html>"))
            )
            if is_code or is_html_document:
                if is_code:
                    language = metadata.get("language")
                    language = language.strip() if isinstance(language, str) else ""
                    if not language or language.lower() == "unknown":
                        language = ""
                else:
                    language = "html"
                longest_fence = max(
                    (len(match.group(0)) for match in re.finditer(r"`+", message_text)),
                    default=0,
                )
                fence = "`" * max(3, longest_fence + 1)
                message_text = f"{fence}{language}\n{message_text}\n{fence}"
            lines.extend(("", message_text))

        if isinstance(resources, list):
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                source_key = _resource_key(resource)
                final_path = resource_map.get(source_key)
                if not final_path:
                    missing_resources.append({**resource, "_export_source_key": source_key})
                    missing_name = str(
                        resource.get("original_name")
                        or resource.get("resource_id")
                        or resource.get("openai_asset_id")
                        or resource.get("openai_file_id")
                        or "recurso"
                    )
                    missing_name = missing_name.replace("\n", " ").replace("]", "-")
                    lines.extend(("", f"[Recurso no disponible: {missing_name}]"))
                    continue
                references.add(final_path)
                name = str(resource.get("original_name") or Path(final_path).name)
                name = name.replace("|", "-").replace("]", "-")
                kind = str(resource.get("kind") or "attachment")
                if kind in {"image", "audio", "video", "pdf"}:
                    lines.extend(("", f"![[{final_path}]]"))
                else:
                    lines.extend(("", f"[[{final_path}|{name}]]"))
        lines.extend(("", "---", ""))

    return "\n".join(lines).rstrip() + "\n", references, missing_resources


def _resource_key(resource: dict[str, Any]) -> str:
    relative = resource.get("relative_resource_path")
    if isinstance(relative, str) and relative.strip():
        return Path(relative).as_posix()
    raw = resource.get("raw_relative_path")
    if isinstance(raw, str) and raw.strip():
        normalized = Path(raw).as_posix()
        return normalized.removeprefix("resources/")
    return ""


def _clean_exported_message_text(value: str) -> str:
    value = re.sub(
        r"(?m)^[ \t]*:::writing\{[^\r\n]*\}[ \t]*(?:\r?\n|$)",
        "",
        value,
    )
    value = re.sub(r"(?m)^[ \t]*:::[ \t]*(?:\r?\n|$)", "", value)
    return re.sub(
        r"(?is)<(?:filecite|memcite)\b[^>]*>.*?</(?:filecite|memcite)>"
        r"|\uE200[^\uE201]*\uE201"
        r"|【(?:filecite|memcite)[^】]*】"
        r"|\[(?:filecite|memcite)[^\]]*\]"
        r"|(?<!\w)(?:filecite|memcite)(?:\s*[:†]\s*[^\s\]】>]+)?",
        "",
        value,
    )


def _yaml_string(value: object) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)
