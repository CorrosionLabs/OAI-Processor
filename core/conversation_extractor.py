from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from core.ids import build_conversation_id, build_message_id, build_resource_id, prefer_openai_id
from core.models import ConversationRecord, MessageRecord, ResourceRecord
from core.store_builder import ConversationStoreBuilder


PROJECT_CONTAINER_KEYS = {"project", "workspace", "folder", "space", "collection"}
PROJECT_ID_KEYS = {"project_id", "workspace_id", "folder_id", "space_id", "collection_id"}
PROJECT_NAME_KEYS = {
    "project_name",
    "workspace_name",
    "folder_name",
    "space_name",
    "collection_name",
    "project_title",
    "workspace_title",
    "folder_title",
    "space_title",
    "collection_title",
}


@dataclass
class ProjectReference:
    project_id: str | None = None
    project_name: str | None = None
    source_path: str | None = None
    slug: str | None = None


@dataclass
class ExtractedResource:
    resource_id: str
    openai_asset_id: str | None
    message_id: str
    conversation_id: str
    original_name: str | None
    resolved_export_name: str
    mime_type: str | None
    size: int | None
    kind: str
    source_dat_name: str | None
    relative_source_path: str | None
    asset_pointer: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_relative_path: str | None = None
    openai_file_id: str | None = None
    project: ProjectReference | None = None
    source_path: str | None = None
    relative_resource_path: str | None = None


@dataclass
class ExtractedMessage:
    message_id: str
    openai_message_id: str | None
    node_id: str
    parent_id: str | None
    role: str
    created_at: str | None
    text: str
    parts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    resources: list[ExtractedResource] = field(default_factory=list)


@dataclass
class ExtractedConversation:
    conversation_id: str
    openai_conversation_id: str | None
    title: str
    created_at: str | None
    updated_at: str | None
    model: str | None
    current_node: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    project: ProjectReference | None = None
    messages: list[ExtractedMessage] = field(default_factory=list)


class ConversationExtractor:
    def extract(
        self,
        backup: Any,
        progress: Callable[[str, int, int, ProjectReference | None, str], None] | None = None,
    ) -> list[ExtractedConversation]:
        raw_conversations = [
            conversation
            for document in backup.conversation_documents
            for conversation in self._iter_conversations(document)
        ]

        def rebuilding(current: int, total: int, source_conversation: dict[str, Any]) -> None:
            if progress is None:
                return
            project_reference = (
                None if source_conversation.get("_processor_unassigned")
                else self._find_project_reference(source_conversation)
            )
            current_item = next(
                (
                    value.strip()
                    for key in ("title", "name", "conversation_id", "id", "conversationId")
                    if isinstance((value := source_conversation.get(key)), str) and value.strip()
                ),
                "Conversación en proceso",
            )
            progress("rebuild", current, total, project_reference, current_item)

        builder = ConversationStoreBuilder(backup.backup_root)
        build_result = builder.build(
            backup.conversation_documents,
            backup.dat_files,
            backup.parsed_json.get("conversation_asset_file_names.json"),
            source_backup_zip=str(backup.source_path) if backup.extracted_from_zip else None,
            progress=rebuilding if progress is not None else None,
        )

        raw_by_openai_id = {
            value: conversation
            for conversation in raw_conversations
            for value in (
                conversation.get("conversation_id"),
                conversation.get("id"),
                conversation.get("conversationId"),
            )
            if isinstance(value, str) and value.strip()
        }

        extracted: list[ExtractedConversation] = []
        for index, record in enumerate(build_result.conversations, start=1):
            source_conversation = raw_conversations[index - 1] if index - 1 < len(raw_conversations) else None
            if source_conversation is None:
                source_conversation = raw_by_openai_id.get(record.conversation_id)
            if source_conversation is None:
                source_conversation = {}

            stable_conversation_id = build_conversation_id(source_conversation, index)
            project_reference = (
                None if source_conversation.get("_processor_unassigned")
                else self._find_project_reference(source_conversation)
            )
            current_item = record.title or stable_conversation_id
            if progress is not None:
                progress(
                    "normalize",
                    index - 1,
                    len(build_result.conversations),
                    project_reference,
                    current_item,
                )
            fallback_resources = self._build_fallback_resources_by_message(
                builder,
                source_conversation,
                stable_conversation_id,
            )
            extracted.append(
                self._convert_conversation(
                    record=record,
                    source_conversation=source_conversation,
                    stable_conversation_id=stable_conversation_id,
                    project_reference=project_reference,
                    fallback_resources_by_message=fallback_resources,
                )
            )
            if progress is not None:
                progress(
                    "normalize",
                    index,
                    len(build_result.conversations),
                    project_reference,
                    current_item,
                )
        return extracted

    def _convert_conversation(
        self,
        *,
        record: ConversationRecord,
        source_conversation: dict[str, Any],
        stable_conversation_id: str,
        project_reference: ProjectReference | None,
        fallback_resources_by_message: dict[str, list[ExtractedResource]],
    ) -> ExtractedConversation:
        messages: list[ExtractedMessage] = []
        for message_index, message in enumerate(record.messages, start=1):
            stable_message_id = build_message_id(
                {
                    "id": message.message_id,
                    "author": {"role": message.author},
                    "create_time": message.created_at,
                    "content": {"parts": [part.text for part in message.parts if part.text]},
                },
                conversation_id=stable_conversation_id,
                fallback_index=message_index,
                node_id=message.node_id,
            )
            messages.append(
                self._convert_message(
                    message=message,
                    stable_conversation_id=stable_conversation_id,
                    stable_message_id=stable_message_id,
                    fallback_resources=fallback_resources_by_message.get(
                        message.message_id or message.node_id or "",
                        [],
                    ),
                )
            )

        for item in messages:
            for resource in item.resources:
                resource.project = project_reference

        return ExtractedConversation(
            conversation_id=stable_conversation_id,
            openai_conversation_id=prefer_openai_id(
                source_conversation.get("conversation_id"),
                source_conversation.get("id"),
                source_conversation.get("conversationId"),
            ),
            title=record.title,
            created_at=record.created_at,
            updated_at=record.updated_at,
            model=record.model,
            current_node=record.current_node,
            metadata={
                **record.metadata,
                "source_keys": sorted(k for k in source_conversation.keys() if isinstance(k, str)),
            },
            project=project_reference,
            messages=messages,
        )

    def _convert_message(
        self,
        *,
        message: MessageRecord,
        stable_conversation_id: str,
        stable_message_id: str,
        fallback_resources: list[ExtractedResource],
    ) -> ExtractedMessage:
        resources: list[ExtractedResource] = []
        for resource_index, resource in enumerate(message.resources, start=1):
            resources.append(
                self._convert_resource(
                    resource=resource,
                    stable_conversation_id=stable_conversation_id,
                    stable_message_id=stable_message_id,
                    resource_index=resource_index,
                )
            )

        if not resources and fallback_resources:
            resources = [
                ExtractedResource(
                    resource_id=item.resource_id,
                    openai_asset_id=item.openai_asset_id,
                    message_id=stable_message_id,
                    conversation_id=stable_conversation_id,
                    original_name=item.original_name,
                    resolved_export_name=item.resolved_export_name,
                    mime_type=item.mime_type,
                    size=item.size,
                    kind=item.kind,
                    source_dat_name=item.source_dat_name,
                    relative_source_path=item.relative_source_path,
                    asset_pointer=item.asset_pointer,
                    metadata=item.metadata,
                    openai_file_id=item.openai_file_id,
                    project=item.project,
                    source_path=item.source_path,
                    relative_resource_path=item.relative_resource_path,
                )
                for item in fallback_resources
            ]

        normalized_text = message.text
        if normalized_text == "[Sin contenido textual]" and not any(part.text for part in message.parts if part.text):
            normalized_text = ""

        return ExtractedMessage(
            message_id=stable_message_id,
            openai_message_id=message.message_id,
            node_id=message.node_id,
            parent_id=message.parent_id,
            role=message.author,
            created_at=message.created_at,
            text=normalized_text,
            parts=[
                {
                    "kind": part.kind,
                    "text": part.text,
                    "raw": part.raw,
                }
                for part in message.parts
            ],
            metadata=message.metadata,
            resources=resources,
        )

    def _convert_resource(
        self,
        *,
        resource: ResourceRecord,
        stable_conversation_id: str,
        stable_message_id: str,
        resource_index: int,
    ) -> ExtractedResource:
        stable_resource_id = build_resource_id(
            asset_id=resource.asset_id or resource.file_id,
            source_dat_name=resource.source_dat_name,
            conversation_id=stable_conversation_id,
            message_id=stable_message_id,
            resource_index=resource_index,
            sha256=None,
        )
        return ExtractedResource(
            resource_id=stable_resource_id,
            openai_asset_id=resource.asset_id or resource.file_id,
            message_id=stable_message_id,
            conversation_id=stable_conversation_id,
            original_name=resource.original_name,
            resolved_export_name=resource.resolved_export_name,
            mime_type=resource.mime_type,
            size=resource.size,
            kind=resource.kind,
            source_dat_name=resource.source_dat_name,
            relative_source_path=resource.relative_source_path,
            asset_pointer=resource.asset_pointer,
            metadata=resource.metadata,
            openai_file_id=resource.file_id,
        )

    def _build_fallback_resources_by_message(
        self,
        builder: ConversationStoreBuilder,
        source_conversation: dict[str, Any],
        stable_conversation_id: str,
    ) -> dict[str, list[ExtractedResource]]:
        mapping = source_conversation.get("mapping")
        current_node = source_conversation.get("current_node")
        if not isinstance(mapping, dict) or not mapping:
            return {}
        if not isinstance(current_node, str) or not current_node.strip():
            current_node = builder._guess_current_node(mapping)

        fallback: dict[str, list[ExtractedResource]] = {}
        branch_nodes = [node for node in mapping.values() if isinstance(node, dict)]
        for node in branch_nodes:
            if not isinstance(node, dict):
                continue
            message = node.get("message")
            if not isinstance(message, dict):
                continue

            stable_message_id = build_message_id(
                message,
                conversation_id=stable_conversation_id,
                fallback_index=len(fallback) + 1,
                node_id=node.get("id") if isinstance(node.get("id"), str) else None,
            )
            message_key = (
                message.get("id")
                if isinstance(message.get("id"), str) and message.get("id")
                else node.get("id")
                if isinstance(node.get("id"), str) and node.get("id")
                else stable_message_id
            )
            attachments = builder._extract_attachments(message)
            if not attachments:
                attachments = self._collect_loose_attachments(message)
            resources: list[ExtractedResource] = []
            for resource_index, attachment in enumerate(attachments, start=1):
                original_name = self._extract_string(
                    attachment,
                    ("original_filename", "filename", "file_name", "name", "title"),
                )
                source_dat_name = self._extract_string(attachment, ("file_name",))
                mime_type = self._extract_string(attachment, ("mime_type", "content_type", "type"))
                kind = builder._infer_resource_kind(mime_type, original_name)
                resolved_export_name = original_name or source_dat_name or f"resource_{resource_index}"
                stable_resource_id = build_resource_id(
                    asset_id=self._extract_string(attachment, ("id", "asset_id", "file_id")),
                    source_dat_name=source_dat_name,
                    conversation_id=stable_conversation_id,
                    message_id=stable_message_id,
                    resource_index=resource_index,
                    sha256=None,
                )
                resources.append(
                    ExtractedResource(
                        resource_id=stable_resource_id,
                        openai_asset_id=self._extract_string(attachment, ("id", "asset_id", "file_id")),
                        message_id=stable_message_id,
                        conversation_id=stable_conversation_id,
                        original_name=original_name,
                        resolved_export_name=resolved_export_name,
                        mime_type=mime_type,
                        size=None,
                        kind=kind,
                        source_dat_name=source_dat_name,
                        relative_source_path=source_dat_name,
                        asset_pointer=self._extract_string(
                            attachment,
                            ("asset_pointer", "asset_pointer_uri", "pointer", "download_url"),
                        ),
                        metadata={k: v for k, v in attachment.items() if isinstance(k, str)},
                        openai_file_id=self._extract_string(attachment, ("file_id",)),
                    )
                )
            if resources:
                fallback[message_key] = resources
        return fallback

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

    def _find_project_reference(self, source_conversation: dict[str, Any]) -> ProjectReference | None:
        header = {key: value for key, value in source_conversation.items() if key != "mapping"}
        result = self._walk_for_project_reference(header, path="$")
        if result is None:
            return None
        if not result.project_id and not result.project_name:
            return None
        return result

    def _walk_for_project_reference(self, node: Any, *, path: str) -> ProjectReference | None:
        if isinstance(node, dict):
            direct = self._extract_project_reference_from_dict(node, path)
            if direct is not None:
                return direct

            for key, value in node.items():
                if not isinstance(key, str):
                    continue
                child_path = f"{path}.{key}"
                if key in PROJECT_CONTAINER_KEYS:
                    nested = self._extract_project_reference_from_container_key(key, value, child_path)
                    if nested is not None:
                        return nested
                nested = self._walk_for_project_reference(value, path=child_path)
                if nested is not None:
                    return nested

        if isinstance(node, list):
            for index, value in enumerate(node):
                nested = self._walk_for_project_reference(value, path=f"{path}[{index}]")
                if nested is not None:
                    return nested
        return None

    @staticmethod
    def _extract_project_reference_from_dict(node: dict[str, Any], path: str) -> ProjectReference | None:
        project_id = None
        project_name = None

        for key in PROJECT_ID_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                project_id = value.strip()
                break

        for key in PROJECT_NAME_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                project_name = value.strip()
                break

        project_value = node.get("project")
        if project_name is None and isinstance(project_value, str) and project_value.strip():
            project_name = project_value.strip()

        if project_id or project_name:
            return ProjectReference(project_id=project_id, project_name=project_name, source_path=path)
        return None

    @staticmethod
    def _extract_project_reference_from_container_key(
        key: str,
        value: Any,
        path: str,
    ) -> ProjectReference | None:
        if isinstance(value, str) and value.strip():
            return ProjectReference(project_id=None, project_name=value.strip(), source_path=path)
        if not isinstance(value, dict):
            return None

        project_id = None
        for candidate in ("id", f"{key}_id", "project_id", "workspace_id", "folder_id"):
            raw_value = value.get(candidate)
            if isinstance(raw_value, str) and raw_value.strip():
                project_id = raw_value.strip()
                break

        project_name = None
        for candidate in ("name", "title", f"{key}_name", "project_name", "workspace_name", "folder_name"):
            raw_value = value.get(candidate)
            if isinstance(raw_value, str) and raw_value.strip():
                project_name = raw_value.strip()
                break

        if project_id or project_name:
            return ProjectReference(project_id=project_id, project_name=project_name, source_path=path)
        return None

    @staticmethod
    def _extract_string(container: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _collect_loose_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []

        direct = message.get("attachments")
        if isinstance(direct, list):
            attachments.extend(item for item in direct if isinstance(item, dict))

        metadata = message.get("metadata")
        if isinstance(metadata, dict):
            nested = metadata.get("attachments")
            if isinstance(nested, list):
                attachments.extend(item for item in nested if isinstance(item, dict))

        return attachments
