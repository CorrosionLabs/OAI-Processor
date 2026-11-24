from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Callable

from core.conversation_extractor import ExtractedConversation, ExtractedResource
from core.ids import build_resource_filename, build_resource_id


RESOURCE_KIND_DIRECTORIES = {
    "image": "images",
    "audio": "audio",
    "video": "video",
    "pdf": "pdf",
    "attachment": "files",
}


@dataclass
class ResourceExtractionResult:
    copied_resources: int
    copied_bytes: int
    missing_resources: int
    error_items: list[dict[str, str]]
    resource_root: Path
    orphan_resources: list[dict]


class ResourceExtractor:
    def __init__(self, backup_root: Path, dat_files: list[object]) -> None:
        self.backup_root = backup_root.resolve()
        self.dat_files = dat_files
        self.dat_by_name = {item.name: item for item in dat_files}
        self.dat_by_id: dict[str, list[object]] = {}
        for item in dat_files:
            if item.name.startswith("file_"):
                self.dat_by_id.setdefault(Path(item.name).stem, []).append(item)

    def materialize(
        self,
        conversations: list[ExtractedConversation],
        process_root: Path,
        project_slugs: set[str],
        progress: Callable[[int, int, str, str | None, int, int, str], None] | None = None,
        selected_project_slugs: set[str] | None = None,
        package_mode: bool = False,
    ) -> ResourceExtractionResult:
        resource_root = process_root / "resources"
        for slug in project_slugs | {"unassigned"}:
            for directory in RESOURCE_KIND_DIRECTORIES.values():
                destination = resource_root / directory if package_mode else resource_root / slug / directory
                destination.mkdir(parents=True, exist_ok=True)

        copied_resources = 0
        copied_bytes = 0
        missing_resources = 0
        error_items: list[dict[str, str]] = []
        orphan_resources: list[dict] = []
        copied_registry: dict[tuple[str, str], str] = {}
        referenced_sources: set[str] = set()
        orphan_entries = self.dat_files
        if selected_project_slugs is not None:
            orphan_entries = [
                entry
                for entry in self.dat_files
                if (
                    (entry_slug := getattr(entry, "project_slug", None)) in selected_project_slugs
                    or (entry_slug is None and "unassigned" in selected_project_slugs)
                )
            ]
        total_resources = (
            sum(len(message.resources) for conversation in conversations for message in conversation.messages)
            + len(orphan_entries)
        )
        processed_resources = 0

        def report(
            slug: str,
            kind: str | None,
            current_item: str,
            copied: int = 0,
            missing: int = 0,
        ) -> None:
            nonlocal processed_resources
            processed_resources += 1
            if progress is not None:
                progress(processed_resources, total_resources, slug, kind, copied, missing, current_item)

        def show_current(slug: str, kind: str | None, current_item: str) -> None:
            if progress is not None:
                progress(processed_resources, total_resources, slug, kind, 0, 0, current_item)

        for conversation in conversations:
            slug = conversation.project.slug if conversation.project and conversation.project.slug else "unassigned"
            for message in conversation.messages:
                for resource_index, resource in enumerate(message.resources, start=1):
                    dat_entry = self._find_entry(resource)
                    if dat_entry is not None:
                        resource.source_dat_name = dat_entry.name
                        resource.relative_source_path = dat_entry.relative_path
                        resource.size = resource.size if resource.size is not None else dat_entry.size_bytes
                        resource.mime_type = resource.mime_type or dat_entry.mime_type
                        if Path(dat_entry.name).suffix.lower() == ".m4a" or resource.kind == "attachment":
                            resource.kind = self._kind_from_mime(resource.mime_type, dat_entry.name)
                        resource.metadata.setdefault("source_file", {
                            "size_bytes": dat_entry.size_bytes,
                            "sha256": dat_entry.sha256,
                            "detector_used": dat_entry.detector_used,
                        })
                        referenced_sources.add(dat_entry.relative_path)
                    resource.resource_id = build_resource_id(
                        asset_id=resource.openai_asset_id or resource.openai_file_id,
                        source_dat_name=resource.source_dat_name,
                        conversation_id=conversation.conversation_id,
                        message_id=message.message_id,
                        resource_index=resource_index,
                        sha256=getattr(dat_entry, "sha256", None),
                    )
                    current_item = (
                        resource.original_name
                        or resource.source_dat_name
                        or resource.resolved_export_name
                        or resource.resource_id
                    )
                    show_current(slug, resource.kind, current_item)
                    source = self._resolve_source_path(resource.relative_source_path)
                    if source is None or not source.is_file():
                        missing_resources += 1
                        error_items.append(self._build_error_item(resource, "missing_resource"))
                        report(slug, resource.kind, current_item, missing=1)
                        continue
                    resource.source_path = str(source)
                    registry_key = (slug, resource.resource_id)
                    already_copied = registry_key in copied_registry
                    if already_copied:
                        relative = copied_registry[registry_key]
                    else:
                        try:
                            relative = self._copy_resource(
                                source=source,
                                resource_root=resource_root,
                                slug=slug,
                                kind=resource.kind,
                                resource_id=resource.resource_id,
                                original_name=resource.original_name or resource.resolved_export_name,
                                fallback_extension=getattr(dat_entry, "probable_extension", None) or source.suffix or ".bin",
                                package_mode=package_mode,
                            )
                        except OSError:
                            missing_resources += 1
                            error_items.append(self._build_error_item(resource, "resource_read_error"))
                            report(slug, resource.kind, current_item, missing=1)
                            continue
                        copied_registry[registry_key] = relative
                        copied_resources += 1
                        copied_bytes += getattr(dat_entry, "size_bytes", None) or source.stat().st_size
                        report(slug, resource.kind, current_item, copied=1)
                    resource.relative_resource_path = relative
                    resource.raw_relative_path = f"resources/{relative}"
                    if already_copied:
                        report(slug, None, current_item)

        for entry in self.dat_files:
            entry_project_slug = getattr(entry, "project_slug", None)
            if selected_project_slugs is not None and (
                entry_project_slug not in selected_project_slugs
                and (entry_project_slug is not None or "unassigned" not in selected_project_slugs)
            ):
                continue
            slug = entry_project_slug or "unassigned"
            show_current(slug, self._kind_from_mime(entry.mime_type, entry.name), entry.name)
            if entry.relative_path in referenced_sources:
                report(slug, None, entry.name)
                continue
            source = self._resolve_source_path(entry.relative_path)
            if source is None or not source.is_file():
                missing_resources += 1
                error_items.append({"file": entry.name, "error_type": "missing_resource"})
                report(
                    slug,
                    self._kind_from_mime(entry.mime_type, entry.name),
                    entry.name,
                    missing=1,
                )
                continue
            asset_id = Path(entry.name).stem if entry.name.startswith("file_") else None
            resource_id = build_resource_id(
                asset_id=asset_id,
                source_dat_name=entry.name,
                conversation_id="",
                message_id="",
                resource_index=0,
                sha256=getattr(entry, "sha256", None),
            )
            kind = self._kind_from_mime(entry.mime_type, entry.name)
            original_name = entry.name if source.suffix.lower() != ".dat" else None
            try:
                relative = self._copy_resource(
                    source=source,
                    resource_root=resource_root,
                    slug=slug,
                    kind=kind,
                    resource_id=resource_id,
                    original_name=original_name,
                    fallback_extension=entry.probable_extension,
                    package_mode=package_mode,
                )
            except OSError:
                missing_resources += 1
                error_items.append({"file": entry.name, "error_type": "resource_read_error"})
                report(slug, kind, entry.name, missing=1)
                continue
            copied_resources += 1
            copied_bytes += entry.size_bytes
            report(slug, kind, entry.name, copied=1)
            orphan_resources.append({
                "resource_id": resource_id,
                "openai_asset_id": asset_id,
                "openai_file_id": asset_id,
                "conversation_id": None,
                "message_id": None,
                "project_slug": slug,
                "original_name": original_name,
                "mime_type": entry.mime_type,
                "kind": kind,
                "source_path": str(source),
                "relative_source_path": entry.relative_path,
                "relative_resource_path": relative,
                "metadata": {
                    "size_bytes": entry.size_bytes,
                    "sha256": entry.sha256,
                    "detector_used": entry.detector_used,
                },
            })

        return ResourceExtractionResult(
            copied_resources=copied_resources,
            copied_bytes=copied_bytes,
            missing_resources=missing_resources,
            error_items=error_items,
            resource_root=resource_root,
            orphan_resources=orphan_resources,
        )

    def _find_entry(self, resource: ExtractedResource) -> object | None:
        project_slug = resource.project.slug if resource.project else None
        for key in (resource.openai_file_id, resource.openai_asset_id):
            if key and key in self.dat_by_id:
                entries = self.dat_by_id[key]
                return next(
                    (item for item in entries if getattr(item, "project_slug", None) == project_slug),
                    entries[0],
                )
        return self.dat_by_name.get(resource.source_dat_name or "")

    def _resolve_source_path(self, relative_path: str | None) -> Path | None:
        if not relative_path:
            return None
        candidate = (self.backup_root / Path(*Path(relative_path).parts)).resolve()
        return candidate if candidate.is_relative_to(self.backup_root) else None

    @staticmethod
    def _kind_from_mime(mime_type: str | None, source_name: str | None = None) -> str:
        mime = (mime_type or "").lower()
        if Path(source_name or "").suffix.lower() == ".m4a":
            return "audio"
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("video/"):
            return "video"
        if mime == "application/pdf":
            return "pdf"
        return "attachment"

    @staticmethod
    def _copy_resource(
        *, source: Path, resource_root: Path, slug: str, kind: str,
        resource_id: str, original_name: str | None, fallback_extension: str,
        package_mode: bool,
    ) -> str:
        filename = build_resource_filename(
            stable_resource_id=resource_id,
            original_name=original_name,
            fallback_extension=fallback_extension,
        )
        directory = RESOURCE_KIND_DIRECTORIES.get(kind, "files")
        destination = (
            resource_root / directory / filename
            if package_mode
            else resource_root / slug / directory / filename
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination.relative_to(resource_root).as_posix()

    @staticmethod
    def _build_error_item(resource: ExtractedResource, error_type: str) -> dict[str, str]:
        return {
            "file": resource.source_dat_name or resource.original_name or resource.resource_id,
            "error_type": error_type,
        }
