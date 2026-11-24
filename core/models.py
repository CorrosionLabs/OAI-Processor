from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ResourceRecord:
    resource_id: str
    asset_id: str | None
    file_id: str | None
    asset_pointer: str | None
    original_name: str | None
    resolved_export_name: str
    mime_type: str | None
    size: int | None
    kind: str
    source_dat_name: str | None
    relative_source_path: str | None
    message_id: str | None
    conversation_id: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MessagePartRecord:
    kind: str
    text: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class MessageRecord:
    message_id: str | None
    node_id: str
    parent_id: str | None
    author: str
    created_at: str | None
    text: str
    parts: list[MessagePartRecord] = field(default_factory=list)
    resources: list[ResourceRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationRecord:
    conversation_id: str
    title: str
    created_at: str | None
    updated_at: str | None
    model: str | None
    current_node: str | None
    messages: list[MessageRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StoreManifest:
    schema_version: int
    generated_at: str
    source_backup_root: str
    source_backup_zip: str | None
    conversation_count: int
    message_count: int
    resource_count: int
    project_count: int = 0
    processed_at: str | None = None
    materialized_resource_count: int = 0
    missing_resource_count: int = 0


@dataclass
class ProjectPackageManifest:
    schema_version: int
    project_id: str
    project_name: str
    generated_at: str
    processed_at: str
    source_backup_root: str
    conversation_count: int
    message_count: int
    resource_count: int
    materialized_resource_count: int
    missing_resource_count: int


def to_plain_data(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value
