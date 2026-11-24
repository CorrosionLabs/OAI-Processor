from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any


@dataclass
class AttachmentRelation:
    dat_name: str
    asset_id: str | None
    original_name: str | None
    mime_type: str | None
    size: int | None
    conversation_id: str | None
    conversation_title: str | None
    message_id: str | None
    attachment_type: str | None
    date_text: str | None
    source: str
    relation_notes: list[str]


class AttachmentMapper:
    def build_relations(
        self,
        asset_mapping_json: Any,
        conversation_documents: list[Any],
        dat_names: set[str],
    ) -> tuple[dict[str, AttachmentRelation], dict[str, dict[str, Any]]]:
        asset_index = self._build_asset_index(conversation_documents)
        dat_to_conversations = self._extract_asset_mapping(asset_mapping_json)

        relations: dict[str, AttachmentRelation] = {}
        for dat_name in dat_names:
            file_id = self._extract_file_id(dat_name)
            asset = asset_index.get(file_id) if file_id else None
            mapped_conversations = sorted(dat_to_conversations.get(dat_name, set()))
            notes: list[str] = []

            conversation_id = asset.get("conversation_id") if asset else None
            if conversation_id is None and mapped_conversations:
                conversation_id = mapped_conversations[0]

            if len(mapped_conversations) > 1:
                notes.append(
                    "El archivo aparece asociado a varias conversaciones en conversation_asset_file_names.json: "
                    + ", ".join(mapped_conversations)
                )

            if asset is None:
                notes.append(
                    "No se encontro el adjunto en conversations -> mapping -> message -> metadata -> attachments."
                )

            original_name = asset.get("filename") if asset else None
            if original_name is None:
                notes.append("Nombre original no disponible.")

            mime_type = asset.get("mime_type") if asset else None
            if mime_type is None:
                notes.append("MIME del adjunto no disponible.")

            message_id = asset.get("message_id") if asset else None
            if message_id is None:
                notes.append("Mensaje asociado no disponible.")

            if conversation_id is None:
                notes.append("Conversacion asociada no disponible.")

            relation = AttachmentRelation(
                dat_name=dat_name,
                asset_id=file_id,
                original_name=original_name,
                mime_type=mime_type,
                size=asset.get("size") if asset else None,
                conversation_id=conversation_id,
                conversation_title=asset.get("conversation_title") if asset else None,
                message_id=message_id,
                attachment_type=asset.get("attachment_type") if asset else None,
                date_text=asset.get("date_text") if asset else None,
                source=str(asset.get("source") or "message.metadata.attachments") if asset else "sin_relacion",
                relation_notes=self._deduplicate_notes(notes),
            )
            relations[dat_name] = relation

        return relations, asset_index

    def _build_asset_index(self, conversation_documents: list[Any]) -> dict[str, dict[str, Any]]:
        asset_index: dict[str, dict[str, Any]] = {}

        for document in conversation_documents:
            for conversation in self._iter_conversations(document):
                conversation_id = self._extract_conversation_id(conversation)
                conversation_title = self._extract_string(conversation, ("title", "name"))
                conversation_date = self._extract_date_from_dict(conversation)
                mapping = conversation.get("mapping")
                if not isinstance(mapping, dict):
                    continue

                for mapping_node in mapping.values():
                    if not isinstance(mapping_node, dict):
                        continue

                    message = mapping_node.get("message")
                    if not isinstance(message, dict):
                        continue

                    message_id = self._extract_message_id(message)
                    message_date = self._extract_date_from_dict(message) or conversation_date
                    metadata = message.get("metadata")
                    for attachment in self._collect_message_attachments(message):
                        asset_id = self._extract_attachment_id(attachment)
                        if not asset_id:
                            continue

                        asset_index[asset_id] = {
                            "filename": self._extract_string(
                                attachment,
                                ("name", "filename", "file_name", "original_filename", "title"),
                            ),
                            "mime_type": self._extract_string(
                                attachment,
                                ("mime_type", "content_type", "type"),
                            ),
                            "size": self._extract_size(attachment),
                            "conversation_id": conversation_id,
                            "conversation_title": conversation_title,
                            "message_id": message_id,
                            "attachment_type": self._extract_string(
                                attachment,
                                ("attachment_type", "asset_type", "kind", "type"),
                            ),
                            "date_text": message_date,
                            "source": self._extract_string(attachment, ("_resource_source",)) or "desconocido",
                        }

        return asset_index

    def _collect_message_attachments(self, message: dict[str, Any]) -> list[dict[str, Any]]:
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
        normalized = {k: v for k, v in candidate.items() if isinstance(k, str)}
        asset_id = self._extract_attachment_id(normalized)
        asset_pointer = self._extract_string(
            normalized,
            ("asset_pointer", "asset_pointer_uri", "pointer", "download_url"),
        )
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

        mime_type = self._extract_string(normalized, ("mime_type",))
        if not mime_type:
            mime_type = self._infer_mime_type_from_candidate(normalized)
            if mime_type:
                normalized["mime_type"] = mime_type

        content_type = self._extract_string(normalized, ("content_type", "type"))
        if not normalized.get("attachment_type") and content_type:
            normalized["attachment_type"] = content_type

        normalized.setdefault("_resource_source", source)
        return normalized

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

    def _extract_asset_mapping(self, data: Any) -> dict[str, set[str]]:
        mapping: dict[str, set[str]] = {}

        if isinstance(data, dict):
            for conversation_id, value in data.items():
                for dat_name in self._collect_dat_names(value):
                    mapping.setdefault(dat_name, set()).add(str(conversation_id))

        return mapping

    def _iter_conversations(self, document: Any) -> list[dict[str, Any]]:
        if isinstance(document, list):
            return [item for item in document if isinstance(item, dict)]
        if isinstance(document, dict):
            if isinstance(document.get("items"), list):
                return [item for item in document["items"] if isinstance(item, dict)]
            return [document]
        return []

    @staticmethod
    def _extract_conversation_id(conversation: dict[str, Any]) -> str | None:
        for key in ("id", "conversation_id", "conversationId"):
            value = conversation.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _extract_message_id(message: dict[str, Any]) -> str | None:
        value = message.get("id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _extract_attachment_id(attachment: dict[str, Any]) -> str | None:
        for key in ("id", "asset_id", "file_id"):
            value = attachment.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _extract_asset_id_from_pointer(pointer: str) -> str | None:
        marker = "file_"
        start = pointer.find(marker)
        if start < 0:
            return None
        end = start + len(marker)
        while end < len(pointer) and pointer[end].isalnum():
            end += 1
        return pointer[start:end] if end > start + len(marker) else None

    @staticmethod
    def _extract_file_id(dat_name: str) -> str | None:
        if not dat_name.startswith("file_"):
            return None
        return dat_name.rsplit(".", 1)[0]

    @staticmethod
    def _extract_string(container: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = container.get(key)
            if isinstance(value, str) and value:
                return value
        return None

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
        if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")):
            if name.endswith(".jpg") or name.endswith(".jpeg"):
                return "image/jpeg"
            if name.endswith(".svg"):
                return "image/svg+xml"
            return f"image/{name.rsplit('.', 1)[-1]}"
        if name.endswith((".mp3", ".wav", ".m4a", ".ogg", ".flac")):
            return "audio/*"
        if name.endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
            return "video/*"
        if name.endswith(".pdf"):
            return "application/pdf"
        return None

    def _collect_dat_names(self, value: Any) -> set[str]:
        names: set[str] = set()
        if isinstance(value, str):
            if value.startswith("file_") and value.endswith(".dat"):
                names.add(value)
        elif isinstance(value, list):
            for item in value:
                names.update(self._collect_dat_names(item))
        elif isinstance(value, dict):
            for item in value.values():
                names.update(self._collect_dat_names(item))
        return names

    def _extract_date_from_dict(self, container: dict[str, Any] | None) -> str | None:
        if not isinstance(container, dict):
            return None

        for key in ("create_time", "created_at", "update_time", "updated_at", "timestamp"):
            value = container.get(key)
            formatted = self._format_date_value(value)
            if formatted:
                return formatted
        return None

    @staticmethod
    def _format_date_value(value: Any) -> str | None:
        if isinstance(value, str) and value:
            return value
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
    def _deduplicate_notes(notes: list[str]) -> list[str]:
        seen: set[str] = set()
        unique: list[str] = []
        for note in notes:
            if note not in seen:
                seen.add(note)
                unique.append(note)
        return unique
