from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Callable

from core.attachment_mapper import AttachmentMapper
from core.ids import build_conversation_id
from core.models import (
    ConversationRecord,
    MessagePartRecord,
    MessageRecord,
    ResourceRecord,
    StoreManifest,
)

if TYPE_CHECKING:
    from core.backup_scanner import DatFileEntry


PHYSICAL_REFERENCE_TYPES = {"attachment", "file", "image", "audio", "video", "pdf"}


@dataclass
class ConversationStoreBuildResult:
    conversations: list[ConversationRecord]
    resources_payload: dict[str, Any]
    manifest: StoreManifest


class ConversationStoreBuilder:
    def __init__(self, backup_root: Path) -> None:
        self.backup_root = backup_root
        self.attachment_mapper = AttachmentMapper()

    def build(
        self,
        conversation_documents: list[Any],
        dat_files: list[DatFileEntry],
        asset_mapping_json: Any,
        source_backup_zip: str | None = None,
        progress: Callable[[int, int, dict[str, Any]], None] | None = None,
    ) -> ConversationStoreBuildResult:
        dat_lookup = {item.name: item for item in dat_files}
        dat_names = set(dat_lookup)
        relations, _asset_index = self.attachment_mapper.build_relations(
            asset_mapping_json,
            conversation_documents,
            dat_names,
        )
        dat_by_asset_id = {
            relation.asset_id: dat_lookup[relation.dat_name]
            for relation in relations.values()
            if relation.asset_id and relation.dat_name in dat_lookup
        }
        resources_index: list[dict[str, Any]] = []
        conversations: list[ConversationRecord] = []

        source_conversations = [
            conversation
            for document in conversation_documents
            for conversation in self._iter_conversations(document)
        ]
        total_conversations = len(source_conversations)
        for index, conversation in enumerate(source_conversations, start=1):
            if progress is not None:
                progress(index - 1, total_conversations, conversation)
            record, resources = self._build_conversation_record(conversation, dat_by_asset_id)
            conversations.append(record)
            resources_index.extend(resources)
            if progress is not None:
                progress(index, total_conversations, conversation)

        message_count = sum(len(conversation.messages) for conversation in conversations)
        manifest = StoreManifest(
            schema_version=1,
            generated_at=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
            source_backup_root=str(self.backup_root),
            source_backup_zip=source_backup_zip,
            conversation_count=len(conversations),
            message_count=message_count,
            resource_count=len(resources_index),
        )
        resources_payload = {
            "resources": resources_index,
        }
        return ConversationStoreBuildResult(
            conversations=conversations,
            resources_payload=resources_payload,
            manifest=manifest,
        )

    def _build_conversation_record(
        self,
        conversation: dict[str, Any],
        dat_by_asset_id: dict[str, DatFileEntry],
    ) -> tuple[ConversationRecord, list[dict[str, Any]]]:
        mapping = conversation.get("mapping")
        if not isinstance(mapping, dict):
            mapping = {}

        current_node = conversation.get("current_node")
        if not isinstance(current_node, str) or not current_node.strip():
            current_node = self._guess_current_node(mapping)

        branch_nodes = [node for node in mapping.values() if isinstance(node, dict)]
        conversation_id = self._extract_string(conversation, ("conversation_id", "id", "conversationId"))
        if not conversation_id:
            conversation_id = build_conversation_id(conversation, 0)

        resource_payloads: list[dict[str, Any]] = []
        messages: list[MessageRecord] = []
        for node in branch_nodes:
            message = node.get("message")
            if not isinstance(message, dict):
                continue

            author = self._extract_role(message)
            parts = self._extract_parts(message)
            text = "\n\n".join(
                part.text for part in parts if part.kind == "text" and isinstance(part.text, str) and part.text.strip()
            ).strip()
            resources, resource_entries = self._extract_resources(
                message,
                conversation_id,
                dat_by_asset_id,
            )
            message_id = self._extract_string(message, ("id",))
            messages.append(
                MessageRecord(
                    message_id=message_id,
                    node_id=self._extract_string(node, ("id",)) or "",
                    parent_id=self._extract_string(node, ("parent",)),
                    author=author,
                    created_at=self._format_date_value(message.get("create_time")),
                    text=text,
                    parts=parts,
                    resources=resources,
                    metadata=self._extract_message_metadata(message),
                )
            )
            resource_payloads.extend(resource_entries)

        messages.sort(key=self._message_sort_key)
        return (
            ConversationRecord(
                conversation_id=conversation_id,
                title=self._extract_string(conversation, ("title", "name")) or "Sin título",
                created_at=self._format_date_value(conversation.get("create_time") or conversation.get("created_at")),
                updated_at=self._format_date_value(conversation.get("update_time") or conversation.get("updated_at")),
                model=self._extract_string(conversation, ("default_model_slug",)) or self._extract_model(branch_nodes),
                current_node=current_node,
                messages=messages,
                metadata=self._extract_conversation_metadata(conversation),
            ),
            resource_payloads,
        )

    def _extract_resources(
        self,
        message: dict[str, Any],
        conversation_id: str,
        dat_by_asset_id: dict[str, DatFileEntry],
    ) -> tuple[list[ResourceRecord], list[dict[str, Any]]]:
        attachments = self._extract_attachments(message)
        resources: list[ResourceRecord] = []
        payloads: list[dict[str, Any]] = []
        message_id = self._extract_string(message, ("id",))

        for index, attachment in enumerate(attachments, start=1):
            asset_id = self._extract_string(attachment, ("asset_id", "id", "file_id"))
            file_id = self._extract_string(attachment, ("file_id", "id", "asset_id"))
            dat_entry = dat_by_asset_id.get(file_id or "") or dat_by_asset_id.get(asset_id or "")
            resource_id = f"{conversation_id}:{message_id or 'message'}:{index}"
            pointer = self._extract_string(
                attachment,
                ("asset_pointer", "asset_pointer_uri", "pointer", "download_url"),
            )
            mime_type = self._extract_string(attachment, ("mime_type", "content_type", "type"))
            if mime_type is None and dat_entry is not None:
                mime_type = dat_entry.mime_type
            original_name = self._extract_string(
                attachment,
                ("name", "filename", "file_name", "original_filename", "title"),
            )
            size = self._extract_size(attachment)
            kind = self._infer_resource_kind(mime_type, original_name)
            resolved_export_name = self._build_resolved_export_name(
                conversation_id=conversation_id,
                message_id=message_id,
                resource_index=index,
                kind=kind,
                original_name=original_name,
                source_dat_name=dat_entry.name if dat_entry is not None else None,
                mime_type=mime_type,
            )
            relative_source_path = dat_entry.relative_path if dat_entry is not None else None
            source_dat_name = dat_entry.name if dat_entry is not None else None

            resource = ResourceRecord(
                resource_id=resource_id,
                asset_id=asset_id,
                file_id=file_id,
                asset_pointer=pointer,
                original_name=original_name,
                resolved_export_name=resolved_export_name,
                mime_type=mime_type,
                size=size,
                kind=kind,
                source_dat_name=source_dat_name,
                relative_source_path=relative_source_path,
                message_id=message_id,
                conversation_id=conversation_id,
                metadata={k: v for k, v in attachment.items() if isinstance(k, str)},
            )
            resources.append(resource)
            payloads.append(
                {
                    "resource_id": resource.resource_id,
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "asset_id": resource.asset_id,
                    "file_id": resource.file_id,
                    "asset_pointer": resource.asset_pointer,
                    "original_name": resource.original_name,
                    "resolved_export_name": resource.resolved_export_name,
                    "mime_type": resource.mime_type,
                    "kind": resource.kind,
                    "size": resource.size,
                    "source_dat_name": resource.source_dat_name,
                    "relative_source_path": resource.relative_source_path,
                }
            )

        return resources, payloads

    def _extract_parts(self, message: dict[str, Any]) -> list[MessagePartRecord]:
        content = message.get("content")
        if not isinstance(content, dict):
            return []

        parts = content.get("parts")
        if isinstance(parts, list):
            records: list[MessagePartRecord] = []
            for part in parts:
                if isinstance(part, str):
                    records.append(MessagePartRecord(kind="text", text=part.strip()))
                elif isinstance(part, dict):
                    text = None
                    if isinstance(part.get("text"), str):
                        text = part["text"].strip()
                    elif isinstance(part.get("content"), str):
                        text = part["content"].strip()
                    records.append(
                        MessagePartRecord(
                            kind=str(part.get("content_type") or part.get("kind") or "structured"),
                            text=text,
                            raw={k: v for k, v in part.items() if isinstance(k, str)},
                        )
                    )
            return records

        text = content.get("text")
        if isinstance(text, str):
            return [MessagePartRecord(kind="text", text=text.strip())]
        result = content.get("result")
        if isinstance(result, str):
            return [MessagePartRecord(kind="text", text=result.strip())]
        return [MessagePartRecord(kind=str(content.get("content_type") or "structured"), raw=content)]

    def _extract_conversation_metadata(self, conversation: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in conversation.items()
            if key != "mapping" and not key.startswith("_processor_")
        }

    def _extract_message_metadata(self, message: dict[str, Any]) -> dict[str, Any]:
        metadata = message.get("metadata")
        result = dict(metadata) if isinstance(metadata, dict) else {}
        content = message.get("content")
        if isinstance(content, dict):
            for key in ("content_type", "language"):
                if key in content:
                    result[key] = content[key]
        return result

    def _extract_model(self, branch_nodes: list[dict[str, Any]]) -> str | None:
        for node in reversed(branch_nodes):
            message = node.get("message")
            if not isinstance(message, dict):
                continue
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                continue
            for key in ("model_slug", "default_model_slug", "model_name"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _extract_attachments(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        def add_candidate(candidate: dict[str, Any], source: str) -> None:
            normalized = self._normalize_attachment_candidate(candidate, source)
            if normalized is None:
                return
            dedupe_key = self._build_attachment_dedupe_key(normalized)
            if dedupe_key in seen_keys:
                return
            seen_keys.add(dedupe_key)
            attachments.append(normalized)

        direct = message.get("attachments")
        if isinstance(direct, list):
            for item in direct:
                if isinstance(item, dict):
                    add_candidate(item, "message.attachments")

        metadata = message.get("metadata")
        if isinstance(metadata, dict):
            nested = metadata.get("attachments")
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        add_candidate(item, "message.metadata.attachments")

            content_references = metadata.get("content_references")
            if isinstance(content_references, list):
                for item in content_references:
                    if isinstance(item, dict):
                        add_candidate(item, "message.metadata.content_references")

            image_results = metadata.get("image_results")
            if isinstance(image_results, list):
                for item in image_results:
                    if isinstance(item, dict):
                        add_candidate(item, "message.metadata.image_results")

            dictation_pointer = self._extract_string(metadata, ("dictation_asset_pointer",))
            if dictation_pointer:
                dictation_candidate: dict[str, Any] = {
                    "asset_pointer": dictation_pointer,
                    "content_type": "audio_asset_pointer",
                }
                dictation_format = self._extract_string(metadata, ("dictation_asset_format",))
                if dictation_format:
                    extension = dictation_format.strip().lower().lstrip(".")
                    if extension.isalnum():
                        dictation_candidate["file_name"] = f"dictation.{extension}"
                add_candidate(dictation_candidate, "message.metadata.dictation_asset_pointer")

        content = message.get("content")
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list):
                for item in parts:
                    if isinstance(item, dict) and self._part_contains_asset(item):
                        add_candidate(item, "message.content.parts")

        return attachments

    def _normalize_attachment_candidate(
        self,
        candidate: dict[str, Any],
        source: str,
    ) -> dict[str, Any] | None:
        if (
            source == "message.metadata.content_references"
            and not self._is_materializable_content_reference(candidate)
        ):
            return None
        normalized = {k: v for k, v in candidate.items() if isinstance(k, str)}
        asset_pointer = self._extract_string(
            normalized,
            ("asset_pointer", "asset_pointer_uri", "pointer", "download_url"),
        )
        asset_id = self._extract_string(normalized, ("id", "asset_id", "file_id"))

        if not asset_id and asset_pointer:
            asset_id = self._extract_asset_id_from_pointer(asset_pointer)

        if not asset_id and not asset_pointer:
            return None

        if asset_id and "id" not in normalized:
            normalized["id"] = asset_id
        if asset_id and "file_id" not in normalized:
            normalized["file_id"] = asset_id
        if asset_pointer and "asset_pointer" not in normalized:
            normalized["asset_pointer"] = asset_pointer

        content_type = self._extract_string(normalized, ("content_type", "type"))
        mime_type = self._extract_string(normalized, ("mime_type",))
        if not mime_type:
            mime_type = self._infer_mime_type_from_candidate(normalized)
            if mime_type:
                normalized["mime_type"] = mime_type
        if not normalized.get("attachment_type") and content_type:
            normalized["attachment_type"] = content_type

        normalized.setdefault("_resource_source", source)
        return normalized

    @staticmethod
    def _is_materializable_content_reference(candidate: dict[str, Any]) -> bool:
        reference_type = str(candidate.get("content_type") or candidate.get("type") or "").strip().lower()
        mime_type = str(candidate.get("mime_type") or "").strip().lower()
        if reference_type in PHYSICAL_REFERENCE_TYPES or reference_type.endswith("_asset_pointer"):
            return True
        if mime_type.startswith(("image/", "audio/", "video/", "text/", "application/")):
            return True
        if any(
            isinstance(candidate.get(key), str) and candidate[key].strip()
            for key in ("asset_id", "file_id", "asset_pointer", "asset_pointer_uri", "download_url")
        ):
            return True
        name = ConversationStoreBuilder._extract_string(
            candidate,
            ("name", "filename", "file_name", "original_filename", "title"),
        )
        suffix = Path(name).suffix if name else ""
        extension = suffix.removeprefix(".")
        return bool(extension and extension.isalnum() and any(character.isalpha() for character in extension))

    @staticmethod
    def _build_attachment_dedupe_key(candidate: dict[str, Any]) -> str:
        for key in ("id", "asset_id", "file_id", "asset_pointer", "download_url"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return repr(sorted(candidate.items()))

    @staticmethod
    def _part_contains_asset(part: dict[str, Any]) -> bool:
        if isinstance(part.get("asset_pointer"), str) and part["asset_pointer"].strip():
            return True
        content_type = str(part.get("content_type") or part.get("kind") or "").strip().lower()
        return content_type.endswith("_asset_pointer")

    @staticmethod
    def _extract_asset_id_from_pointer(pointer: str) -> str | None:
        match = re.search(r"(file_[a-zA-Z0-9]+)", pointer)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _infer_mime_type_from_candidate(candidate: dict[str, Any]) -> str | None:
        content_type = str(candidate.get("content_type") or candidate.get("kind") or "").strip().lower()
        if content_type == "image_asset_pointer":
            return "image/*"
        if content_type == "audio_asset_pointer":
            return "audio/*"
        if content_type == "video_asset_pointer":
            return "video/*"

        name = str(
            candidate.get("name")
            or candidate.get("filename")
            or candidate.get("file_name")
            or candidate.get("original_filename")
            or candidate.get("title")
            or ""
        ).strip().lower()
        suffix = Path(name).suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}:
            return f"image/{suffix.lstrip('.')}" if suffix != ".jpg" else "image/jpeg"
        if suffix in {".mp3", ".wav", ".m4a", ".ogg", ".flac"}:
            return "audio/*"
        if suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
            return "video/*"
        if suffix == ".pdf":
            return "application/pdf"
        return None

    def _build_active_branch(self, mapping: dict[str, Any], current_node: str | None) -> list[dict[str, Any]]:
        if not current_node:
            return []
        branch: list[dict[str, Any]] = []
        seen: set[str] = set()
        node_id: str | None = current_node
        while node_id:
            if node_id in seen:
                break
            seen.add(node_id)
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                break
            branch.append(node)
            parent = node.get("parent")
            node_id = parent if isinstance(parent, str) and parent.strip() else None
        branch.reverse()
        return branch

    def _guess_current_node(self, mapping: dict[str, Any]) -> str | None:
        child_ids: set[str] = set()
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            children = node.get("children")
            if isinstance(children, list):
                child_ids.update(child for child in children if isinstance(child, str))
        leaf_candidates = [node_id for node_id in mapping if node_id not in child_ids]
        if leaf_candidates:
            return leaf_candidates[0]
        return next(iter(mapping), None)

    @staticmethod
    def _iter_conversations(document: Any) -> list[dict[str, Any]]:
        if isinstance(document, list):
            return [item for item in document if isinstance(item, dict)]
        if isinstance(document, dict):
            items = document.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            return [document]
        return []

    @staticmethod
    def _extract_string(container: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_role(message: dict[str, Any]) -> str:
        author = message.get("author")
        if isinstance(author, dict):
            role = author.get("role")
            if isinstance(role, str) and role.strip():
                return role.strip().lower()
        return "unknown"

    @staticmethod
    def _extract_size(container: dict[str, Any]) -> int | None:
        for key in ("size", "size_bytes", "file_size"):
            value = container.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, float) and value.is_integer():
                return int(value)
        return None

    @staticmethod
    def _format_date_value(value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not math.isnan(value):
            timestamp = float(value)
            if timestamp > 1e12:
                timestamp /= 1000.0
            try:
                dt = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()
            except (OverflowError, OSError, ValueError):
                return str(value)
            return dt.strftime("%Y-%m-%d %H:%M:%S %z")
        return None

    @staticmethod
    def _message_sort_key(message: MessageRecord) -> tuple[int, float | str]:
        if message.created_at:
            try:
                timestamp = datetime.strptime(message.created_at, "%Y-%m-%d %H:%M:%S %z").timestamp()
                return 0, timestamp
            except ValueError:
                return 1, message.created_at
        return 2, message.node_id

    @staticmethod
    def _infer_resource_kind(mime_type: str | None, original_name: str | None) -> str:
        mime = (mime_type or "").lower()
        suffix = Path(original_name or "").suffix.lower()
        if mime.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}:
            return "image"
        if mime.startswith("audio/") or suffix in {".mp3", ".wav", ".m4a", ".ogg", ".flac"}:
            return "audio"
        if mime.startswith("video/") or suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
            return "video"
        if mime == "application/pdf" or suffix == ".pdf":
            return "pdf"
        return "attachment"

    @staticmethod
    def _build_resolved_export_name(
        conversation_id: str,
        message_id: str | None,
        resource_index: int,
        kind: str,
        original_name: str | None,
        source_dat_name: str | None,
        mime_type: str | None,
    ) -> str:
        candidate_name = (original_name or "").strip()
        if candidate_name:
            return ConversationStoreBuilder._sanitize_filename(
                ConversationStoreBuilder._ensure_extension(candidate_name, mime_type)
            )

        stem = "_".join(
            part
            for part in (
                kind or "adjunto",
                ConversationStoreBuilder._sanitize_slug(conversation_id)[:24] or "conv",
                ConversationStoreBuilder._sanitize_slug(message_id or "mensaje")[:24] or "mensaje",
                str(resource_index),
            )
            if part
        )
        extension = ConversationStoreBuilder._guess_extension(mime_type, source_dat_name)
        return ConversationStoreBuilder._sanitize_filename(f"{stem}{extension}")

    @staticmethod
    def _ensure_extension(name: str, mime_type: str | None) -> str:
        if Path(name).suffix:
            return name
        return f"{name}{ConversationStoreBuilder._guess_extension(mime_type, None)}"

    @staticmethod
    def _guess_extension(mime_type: str | None, source_dat_name: str | None) -> str:
        mime = (mime_type or "").lower()
        mapping = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/ogg": ".ogg",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "application/pdf": ".pdf",
            "text/plain": ".txt",
        }
        if mime in mapping:
            return mapping[mime]
        if source_dat_name:
            suffix = Path(source_dat_name).suffix.lower()
            if suffix and suffix != ".dat":
                return suffix
        if mime.startswith("image/"):
            return ".img"
        if mime.startswith("audio/"):
            return ".audio"
        if mime.startswith("video/"):
            return ".video"
        return ".bin"

    @staticmethod
    def _sanitize_slug(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        sanitized = re.sub(r'[<>:"/\\|?*]+', "_", name).strip().strip(".")
        return sanitized[:180] or "adjunto.bin"
