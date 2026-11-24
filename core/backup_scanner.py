"""Read the selected backup without changing its source files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
from time import monotonic
from typing import Any, Callable

from core.attachment_mapper import AttachmentMapper
from core.conversation_extractor import ConversationExtractor, ProjectReference
from core.dat_analyzer import DatAnalyzer, HashCache
from core.ids import slugify
from core.manifest_reader import read_json_file, read_optional_json
from core.models import ProjectPackageManifest, StoreManifest
from core.resource_extractor import ResourceExtractor
from core.store_repository import ConversationStoreRepository


CONVERSATION_PATTERN = re.compile(r"^conversations(?:-\d+)?\.json$")


class BackupValidationError(Exception):
    """The selected folder does not contain supported conversation JSON."""


@dataclass
class FileEntry:
    name: str
    relative_path: str
    size_bytes: int


@dataclass
class DatFileEntry(FileEntry):
    mime_type: str
    file_type: str
    probable_extension: str
    sha256: str
    detector_used: str
    original_name: str | None = None
    conversation_id: str | None = None
    conversation_title: str | None = None
    message_id: str | None = None
    attachment_type: str | None = None
    date_text: str | None = None
    relation_source: str = ""
    relation_notes: list[str] | None = None
    project_slug: str | None = None


@dataclass
class ScannedBackup:
    source_path: Path
    backup_root: Path
    extracted_from_zip: bool
    parsed_json: dict[str, Any]
    conversation_documents: list[Any]
    dat_files: list[DatFileEntry]
    projects: list[ProjectReference]


class BackupScanner:
    def __init__(self, backup_root: Path, process_root: Path) -> None:
        self.backup_root = backup_root.resolve()
        self.output_path = process_root.absolute()
        self.process_root = process_root.resolve()
        self.dat_analyzer = DatAnalyzer(HashCache(self.process_root / "data" / ".hash_cache.json"))

    def process(self, progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        if self.output_path.name != "process" or self.output_path.parent.resolve() != self.backup_root:
            raise ValueError("La salida debe ser la carpeta process del backup seleccionado.")
        if self.output_path.is_symlink():
            raise ValueError("La carpeta process no puede ser un enlace simbólico.")

        started_at = monotonic()
        phase_started_at = started_at
        phase_key = ""
        phase_label = ""
        process_stage = ""
        phase_start_percent = 0.0
        phase_end_percent = 0.0
        last_percent = 0.0
        phase_durations: dict[str, float] = {}
        counters: dict[str, Any] = {
            "project": None,
            "current_conversation": 0,
            "total_conversations": 0,
            "images": 0,
            "audio": 0,
            "video": 0,
            "pdf": 0,
            "files": 0,
            "current_item": None,
            "copied_resources": 0,
            "missing_resources": 0,
        }

        def emit(current: int = 0, total: int = 0, *, completed: bool = False) -> None:
            nonlocal last_percent
            if progress is None:
                return
            if completed:
                percent = 100.0
            elif total:
                percent = phase_start_percent + (phase_end_percent - phase_start_percent) * min(
                    1.0, current / total
                )
            else:
                percent = phase_start_percent
            percent = min(100.0, max(last_percent, percent))
            last_percent = percent
            now = monotonic()
            durations = dict(phase_durations)
            if phase_key:
                durations[phase_key] = now - phase_started_at
            progress({
                "phase": phase_key,
                "phase_label": phase_label,
                "stage": process_stage,
                "current_item": counters["current_item"],
                "project": counters["project"],
                "current_conversation": counters["current_conversation"],
                "total_conversations": counters["total_conversations"],
                "images": counters["images"],
                "audio": counters["audio"],
                "video": counters["video"],
                "pdf": counters["pdf"],
                "files": counters["files"],
                "copied_resources": counters["copied_resources"],
                "missing_resources": counters["missing_resources"],
                "percent": round(percent, 2),
                "phase_elapsed": now - phase_started_at,
                "total_elapsed": now - started_at,
                "phase_durations": durations,
                "current": current,
                "total": total,
            })

        def start_phase(key: str, label: str, stage: str, start: float, end: float) -> None:
            nonlocal phase_key, phase_label, process_stage
            nonlocal phase_start_percent, phase_end_percent, phase_started_at
            if phase_key:
                phase_durations[phase_key] = monotonic() - phase_started_at
            phase_key = key
            phase_label = label
            process_stage = stage
            phase_start_percent = start
            phase_end_percent = end
            phase_started_at = monotonic()
            counters["project"] = None
            counters["current_item"] = None
            emit()

        start_phase("analysis", "Análisis del backup", "analysis", 0, 10)

        def analysis_progress(current: int, total: int, current_item: str) -> None:
            counters["current_item"] = current_item
            emit(current, total)

        backup = self.scan(analysis_progress)
        emit(1, 1)
        counters["total_conversations"] = sum(
            len(self._iter_conversations(document)) for document in backup.conversation_documents
        )

        def conversation_progress(
            conversation_stage: str,
            current: int,
            total: int,
            project: ProjectReference | None,
            current_item: str,
        ) -> None:
            if conversation_stage == "rebuild":
                if phase_key != "rebuild":
                    start_phase("rebuild", "Reconstrucción de conversaciones", "conversations", 10, 20)
                counters["current_conversation"] = current
                counters["total_conversations"] = total
                counters["project"] = project.project_name if project and project.project_name else None
                counters["current_item"] = current_item
                emit(current, total)
                return
            if phase_key != "normalize":
                start_phase("normalize", "Normalización de conversaciones", "conversations", 20, 30)
            counters["current_conversation"] = current
            counters["total_conversations"] = total
            counters["project"] = project.project_name if project and project.project_name else None
            counters["current_item"] = current_item
            emit(current, total)

        start_phase("rebuild", "Reconstrucción de conversaciones", "conversations", 10, 20)
        conversations = ConversationExtractor().extract(backup, conversation_progress)
        if not conversations:
            start_phase("normalize", "Normalización de conversaciones", "conversations", 20, 30)
            emit(1, 1)
        projects = self._assign_projects(conversations, backup.projects)
        project_slugs = {project.slug for project in projects if project.slug}
        project_names = {
            project.slug: project.project_name or project.slug
            for project in projects
            if project.slug
        }

        repository = ConversationStoreRepository(self.process_root)
        repository.prepare(project_slugs)
        start_phase("resources", "Extracción de recursos", "resources", 30, 90)

        def resource_progress(
            current: int,
            total: int,
            slug: str,
            kind: str | None,
            copied: int,
            missing: int,
            current_item: str,
        ) -> None:
            counters["project"] = project_names.get(slug)
            counters["current_item"] = current_item
            if copied or missing:
                if kind == "image":
                    counters["images"] += 1
                elif kind == "audio":
                    counters["audio"] += 1
                elif kind == "video":
                    counters["video"] += 1
                elif kind == "pdf":
                    counters["pdf"] += 1
                elif kind == "attachment":
                    counters["files"] += 1
            counters["copied_resources"] += copied
            counters["missing_resources"] += missing
            emit(current, total)

        resource_result = ResourceExtractor(self.backup_root, backup.dat_files).materialize(
            conversations, self.process_root, project_slugs, resource_progress
        )
        emit(1, 1)
        start_phase("conversation_files", "Guardando conversaciones", "indexes", 90, 96)

        def saved_conversation(current: int, total: int, slug: str, filename: str) -> None:
            counters["project"] = project_names.get(slug)
            counters["current_item"] = filename
            emit(current, total)

        locations = repository.save_conversations(conversations, saved_conversation)
        if not conversations:
            emit(1, 1)
        start_phase("index", "Creación de índices", "indexes", 96, 98)
        counters["current_item"] = "index.json"
        emit(0, 2)
        index = self._build_index(conversations, projects, locations, resource_result.orphan_resources)
        emit(1, 2)
        repository.save_index_payload(index)
        emit(2, 2)
        processed_at = datetime.now(timezone.utc).isoformat()
        manifest = StoreManifest(
            schema_version=1,
            generated_at=processed_at,
            processed_at=processed_at,
            source_backup_root=str(self.backup_root),
            source_backup_zip=None,
            project_count=len(projects),
            conversation_count=len(conversations),
            message_count=sum(len(item.messages) for item in conversations),
            resource_count=len(index["resources"]),
            materialized_resource_count=resource_result.copied_resources,
            missing_resource_count=resource_result.missing_resources,
        )
        start_phase("finalization", "Finalización y cierre", "indexes", 98, 100)
        counters["current_item"] = "manifest.json"
        emit(0, 1)
        repository.save_manifest(manifest)
        emit(1, 1, completed=True)
        return {
            "project_count": manifest.project_count,
            "conversation_count": manifest.conversation_count,
            "message_count": manifest.message_count,
            "resource_count": manifest.resource_count,
            "materialized_resource_count": manifest.materialized_resource_count,
            "missing_resource_count": manifest.missing_resource_count,
            "process_root": str(self.process_root),
            "phase_durations": phase_durations,
        }

    def process_selected(
        self,
        selected_project_ids: set[str | None],
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Process only selected projects into an isolated temporary repository."""
        if not isinstance(selected_project_ids, set) or not selected_project_ids:
            raise ValueError("Debe seleccionar al menos un proyecto.")
        if any(
            project_id is not None
            and (not isinstance(project_id, str) or not project_id.strip())
            for project_id in selected_project_ids
        ):
            raise ValueError("Los identificadores de proyecto no son válidos.")
        selected_project_ids = {
            project_id.strip() if isinstance(project_id, str) else None
            for project_id in selected_project_ids
        }

        started_at = monotonic()
        phase_started_at = started_at
        phase_key = ""
        phase_label = ""
        process_stage = ""
        phase_start_percent = 0.0
        phase_end_percent = 0.0
        last_percent = 0.0
        phase_durations: dict[str, float] = {}
        counters: dict[str, Any] = {
            "project": None,
            "current_conversation": 0,
            "total_conversations": 0,
            "images": 0,
            "audio": 0,
            "video": 0,
            "pdf": 0,
            "files": 0,
            "current_item": None,
            "copied_resources": 0,
            "missing_resources": 0,
        }

        def emit(current: int = 0, total: int = 0, *, completed: bool = False) -> None:
            nonlocal last_percent
            if progress is None:
                return
            if completed:
                percent = 100.0
            elif total:
                percent = phase_start_percent + (phase_end_percent - phase_start_percent) * min(
                    1.0, current / total
                )
            else:
                percent = phase_start_percent
            percent = min(100.0, max(last_percent, percent))
            last_percent = percent
            now = monotonic()
            durations = dict(phase_durations)
            if phase_key:
                durations[phase_key] = now - phase_started_at
            progress({
                "phase": phase_key,
                "phase_label": phase_label,
                "stage": process_stage,
                "current_item": counters["current_item"],
                "project": counters["project"],
                "current_conversation": counters["current_conversation"],
                "total_conversations": counters["total_conversations"],
                "images": counters["images"],
                "audio": counters["audio"],
                "video": counters["video"],
                "pdf": counters["pdf"],
                "files": counters["files"],
                "copied_resources": counters["copied_resources"],
                "missing_resources": counters["missing_resources"],
                "percent": round(percent, 2),
                "phase_elapsed": now - phase_started_at,
                "total_elapsed": now - started_at,
                "phase_durations": durations,
                "current": current,
                "total": total,
            })

        def start_phase(key: str, label: str, stage: str, start: float, end: float) -> None:
            nonlocal phase_key, phase_label, process_stage
            nonlocal phase_start_percent, phase_end_percent, phase_started_at
            if phase_key:
                phase_durations[phase_key] = monotonic() - phase_started_at
            phase_key = key
            phase_label = label
            process_stage = stage
            phase_start_percent = start
            phase_end_percent = end
            phase_started_at = monotonic()
            counters["project"] = None
            counters["current_item"] = None
            emit()

        start_phase("analysis", "Análisis del backup", "analysis", 0, 10)

        def analysis_progress(current: int, total: int, current_item: str) -> None:
            counters["current_item"] = current_item
            emit(current, total)

        backup = self.scan(analysis_progress, selected_project_ids=selected_project_ids)
        emit(1, 1)
        counters["total_conversations"] = sum(
            len(self._iter_conversations(document)) for document in backup.conversation_documents
        )

        def conversation_progress(
            conversation_stage: str,
            current: int,
            total: int,
            project: ProjectReference | None,
            current_item: str,
        ) -> None:
            if conversation_stage == "rebuild":
                if phase_key != "rebuild":
                    start_phase("rebuild", "Reconstrucción de conversaciones", "conversations", 10, 20)
                counters["current_conversation"] = current
                counters["total_conversations"] = total
                counters["project"] = project.project_name if project and project.project_name else None
                counters["current_item"] = current_item
                emit(current, total)
                return
            if phase_key != "normalize":
                start_phase("normalize", "Normalización de conversaciones", "conversations", 20, 30)
            counters["current_conversation"] = current
            counters["total_conversations"] = total
            counters["project"] = project.project_name if project and project.project_name else None
            counters["current_item"] = current_item
            emit(current, total)

        start_phase("rebuild", "Reconstrucción de conversaciones", "conversations", 10, 20)
        conversations = ConversationExtractor().extract(backup, conversation_progress)
        if not conversations:
            start_phase("normalize", "Normalización de conversaciones", "conversations", 20, 30)
            emit(1, 1)
        projects = self._assign_projects(conversations, backup.projects)
        project_slugs = {project.slug for project in projects if project.slug}
        if None in selected_project_ids:
            project_slugs.add("unassigned")
        project_names = {
            project.slug: project.project_name or project.slug
            for project in projects
            if project.slug
        }

        repository: ConversationStoreRepository | None = None
        try:
            repository = ConversationStoreRepository.prepare_selected_projects(
                self.backup_root, project_slugs
            )
            start_phase("resources", "Extracción de recursos", "resources", 30, 90)

            def resource_progress(
                current: int,
                total: int,
                slug: str,
                kind: str | None,
                copied: int,
                missing: int,
                current_item: str,
            ) -> None:
                counters["project"] = project_names.get(slug)
                counters["current_item"] = current_item
                if copied or missing:
                    if kind == "image":
                        counters["images"] += 1
                    elif kind == "audio":
                        counters["audio"] += 1
                    elif kind == "video":
                        counters["video"] += 1
                    elif kind == "pdf":
                        counters["pdf"] += 1
                    elif kind == "attachment":
                        counters["files"] += 1
                counters["copied_resources"] += copied
                counters["missing_resources"] += missing
                emit(current, total)

            resource_result = ResourceExtractor(self.backup_root, backup.dat_files).materialize(
                conversations,
                repository.output_dir,
                project_slugs,
                resource_progress,
                selected_project_slugs=project_slugs,
            )
            emit(1, 1)
            start_phase("conversation_files", "Guardando conversaciones", "indexes", 90, 96)

            def saved_conversation(current: int, total: int, slug: str, filename: str) -> None:
                counters["project"] = project_names.get(slug)
                counters["current_item"] = filename
                emit(current, total)

            locations = repository.save_conversations(conversations, saved_conversation)
            if not conversations:
                emit(1, 1)
            start_phase("index", "Creación de índices", "indexes", 96, 98)
            counters["current_item"] = "index.json"
            emit(0, 2)
            index = self._build_index(conversations, projects, locations, resource_result.orphan_resources)
            emit(1, 2)
            repository.save_index_payload(index)
            emit(2, 2)
            processed_at = datetime.now(timezone.utc).isoformat()
            manifest = StoreManifest(
                schema_version=1,
                generated_at=processed_at,
                processed_at=processed_at,
                source_backup_root=str(self.backup_root),
                source_backup_zip=None,
                project_count=len(projects),
                conversation_count=len(conversations),
                message_count=sum(len(item.messages) for item in conversations),
                resource_count=len(index["resources"]),
                materialized_resource_count=resource_result.copied_resources,
                missing_resource_count=resource_result.missing_resources,
            )
            start_phase("finalization", "Finalización y cierre", "indexes", 98, 100)
            counters["current_item"] = "manifest.json"
            emit(0, 1)
            repository.save_manifest(manifest)
            emit(1, 1, completed=True)
            return {
                "temporary_root": str(repository.output_dir),
                "process_root": str(repository.output_dir),
                "projects": [
                    {
                        "project_id": project.project_id,
                        "project_name": project.project_name,
                        "slug": project.slug,
                    }
                    for project in projects
                ],
                "project_count": manifest.project_count,
                "conversation_count": manifest.conversation_count,
                "message_count": manifest.message_count,
                "resource_count": manifest.resource_count,
                "materialized_resource_count": manifest.materialized_resource_count,
                "missing_resource_count": manifest.missing_resource_count,
                "phase_durations": phase_durations,
            }
        except Exception:
            if repository is not None:
                temporary_root = repository.output_dir.resolve()
                if temporary_root.parent == self.backup_root:
                    shutil.rmtree(temporary_root, ignore_errors=True)
            raise

    def process_project_package(
        self,
        project_id: str | None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Build an isolated temporary package for one project."""
        started_at = monotonic()
        phase_started_at = started_at
        phase_key = ""
        phase_label = ""
        process_stage = ""
        phase_start_percent = 0.0
        phase_end_percent = 0.0
        last_percent = 0.0
        phase_durations: dict[str, float] = {}
        counters: dict[str, Any] = {
            "project": None,
            "current_conversation": 0,
            "total_conversations": 0,
            "images": 0,
            "audio": 0,
            "video": 0,
            "pdf": 0,
            "files": 0,
            "current_item": None,
            "copied_resources": 0,
            "missing_resources": 0,
        }

        def emit(current: int = 0, total: int = 0, *, completed: bool = False) -> None:
            nonlocal last_percent
            if progress is None:
                return
            if completed:
                percent = 100.0
            elif total:
                percent = phase_start_percent + (phase_end_percent - phase_start_percent) * min(
                    1.0, current / total
                )
            else:
                percent = phase_start_percent
            percent = min(100.0, max(last_percent, percent))
            last_percent = percent
            now = monotonic()
            durations = dict(phase_durations)
            if phase_key:
                durations[phase_key] = now - phase_started_at
            progress({
                "phase": phase_key,
                "phase_label": phase_label,
                "stage": process_stage,
                "current_item": counters["current_item"],
                "project": counters["project"],
                "current_conversation": counters["current_conversation"],
                "total_conversations": counters["total_conversations"],
                "images": counters["images"],
                "audio": counters["audio"],
                "video": counters["video"],
                "pdf": counters["pdf"],
                "files": counters["files"],
                "copied_resources": counters["copied_resources"],
                "missing_resources": counters["missing_resources"],
                "percent": round(percent, 2),
                "phase_elapsed": now - phase_started_at,
                "total_elapsed": now - started_at,
                "phase_durations": durations,
                "current": current,
                "total": total,
            })

        def start_phase(key: str, label: str, stage: str, start: float, end: float) -> None:
            nonlocal phase_key, phase_label, process_stage
            nonlocal phase_start_percent, phase_end_percent, phase_started_at
            if phase_key:
                phase_durations[phase_key] = monotonic() - phase_started_at
            phase_key = key
            phase_label = label
            process_stage = stage
            phase_start_percent = start
            phase_end_percent = end
            phase_started_at = monotonic()
            counters["project"] = None
            counters["current_item"] = None
            emit()

        start_phase("analysis", "Análisis del backup", "analysis", 0, 10)

        def analysis_progress(current: int, total: int, current_item: str) -> None:
            counters["current_item"] = current_item
            emit(current, total)

        backup = self.scan(analysis_progress, selected_project_ids={project_id})
        emit(1, 1)
        counters["total_conversations"] = sum(
            len(self._iter_conversations(document)) for document in backup.conversation_documents
        )

        def conversation_progress(
            conversation_stage: str,
            current: int,
            total: int,
            project: ProjectReference | None,
            current_item: str,
        ) -> None:
            if conversation_stage == "rebuild":
                if phase_key != "rebuild":
                    start_phase("rebuild", "Reconstrucción de conversaciones", "conversations", 10, 20)
                counters["current_conversation"] = current
                counters["total_conversations"] = total
                counters["project"] = project.project_name if project and project.project_name else None
                counters["current_item"] = current_item
                emit(current, total)
                return
            if phase_key != "normalize":
                start_phase("normalize", "Normalización de conversaciones", "conversations", 20, 30)
            counters["current_conversation"] = current
            counters["total_conversations"] = total
            counters["project"] = project.project_name if project and project.project_name else None
            counters["current_item"] = current_item
            emit(current, total)

        start_phase("rebuild", "Reconstrucción de conversaciones", "conversations", 10, 20)
        conversations = ConversationExtractor().extract(backup, conversation_progress)
        if not conversations:
            start_phase("normalize", "Normalización de conversaciones", "conversations", 20, 30)
            emit(1, 1)
        projects = self._assign_projects(conversations, backup.projects)
        project_slugs = {project.slug for project in projects if project.slug}
        if project_id is None:
            project_slugs.add("unassigned")
        project_names = {
            project.slug: project.project_name or project.slug
            for project in projects
            if project.slug
        }
        package_project = next(
            (project for project in projects if project.project_id == project_id), None
        )
        package_name = (
            "Sin proyecto"
            if project_id is None
            else package_project.project_name if package_project and package_project.project_name else project_id
        )

        repository = None
        package_installed = False
        try:
            repository = ConversationStoreRepository.prepare_project_package(
                self.backup_root, project_id
            )
            start_phase("resources", "Extracción de recursos", "resources", 30, 90)

            def resource_progress(
                current: int,
                total: int,
                slug: str,
                kind: str | None,
                copied: int,
                missing: int,
                current_item: str,
            ) -> None:
                counters["project"] = project_names.get(slug, package_name)
                counters["current_item"] = current_item
                if copied or missing:
                    if kind == "image":
                        counters["images"] += 1
                    elif kind == "audio":
                        counters["audio"] += 1
                    elif kind == "video":
                        counters["video"] += 1
                    elif kind == "pdf":
                        counters["pdf"] += 1
                    elif kind == "attachment":
                        counters["files"] += 1
                counters["copied_resources"] += copied
                counters["missing_resources"] += missing
                emit(current, total)

            resource_result = ResourceExtractor(self.backup_root, backup.dat_files).materialize(
                conversations,
                repository.package_root,
                project_slugs,
                resource_progress,
                selected_project_slugs=project_slugs,
                package_mode=True,
            )
            emit(1, 1)
            start_phase("conversation_files", "Guardando conversaciones", "indexes", 90, 96)

            locations = repository.save_conversations(conversations)
            counters["current_item"] = "conversations"
            emit(1, 1)
            start_phase("index", "Creación de índices", "indexes", 96, 98)
            counters["current_item"] = "index.json"
            emit(0, 2)
            index = self._build_index(conversations, projects, locations, resource_result.orphan_resources)
            index["projects"] = [
                item for item in index["projects"] if item["slug"] in project_slugs
            ]
            emit(1, 2)
            repository.save_index_payload(index)
            emit(2, 2)
            processed_at = datetime.now(timezone.utc).isoformat()
            manifest = ProjectPackageManifest(
                schema_version=1,
                project_id=repository.project_id,
                project_name=package_name,
                generated_at=processed_at,
                processed_at=processed_at,
                source_backup_root=str(self.backup_root),
                conversation_count=len(conversations),
                message_count=sum(len(item.messages) for item in conversations),
                resource_count=len(index["resources"]),
                materialized_resource_count=resource_result.copied_resources,
                missing_resource_count=resource_result.missing_resources,
            )
            start_phase("finalization", "Finalización y cierre", "indexes", 98, 100)
            counters["current_item"] = "manifest.json"
            emit(0, 1)
            repository.save_manifest(manifest)
            package_root = ConversationStoreRepository.install_project_package(
                repository.package_root, self.backup_root
            )
            package_installed = True
            ConversationStoreRepository.save_project_catalog(self.backup_root)
            emit(1, 1, completed=True)
            return {
                "package_root": str(package_root),
                "project_id": repository.project_id,
                "project_name": package_name,
                "conversation_count": manifest.conversation_count,
                "message_count": manifest.message_count,
                "resource_count": manifest.resource_count,
                "materialized_resource_count": manifest.materialized_resource_count,
                "missing_resource_count": manifest.missing_resource_count,
                "phase_durations": phase_durations,
            }
        except Exception:
            if repository is not None and not package_installed:
                staging_root = repository.package_root.parents[2]
                if staging_root.parent == self.backup_root:
                    shutil.rmtree(staging_root, ignore_errors=True)
            raise

    def scan(
        self,
        progress: Callable[[int, int, str], None] | None = None,
        selected_project_ids: set[str | None] | None = None,
    ) -> ScannedBackup:
        if not self.backup_root.is_dir():
            raise BackupValidationError("La carpeta del backup no existe.")

        source_root = self.backup_root / "backup"
        if not source_root.is_dir():
            source_root = self.backup_root
        if progress is not None:
            progress(0, 0, source_root.name)

        documents: list[Any] = []
        projects: list[ProjectReference] = []
        resource_paths: dict[Path, str | None] = {}
        selected_project_folders: list[tuple[Path, str]] = []
        selected_documents: dict[str | None, list[Any]] = {}

        conversations_dir = source_root / "conversations"
        if conversations_dir.is_dir() and (selected_project_ids is None or None in selected_project_ids):
            for path in sorted(conversations_dir.glob("*.json")):
                if progress is not None:
                    progress(0, 0, path.relative_to(self.backup_root).as_posix())
                document = read_json_file(path)
                for conversation in self._iter_conversations(document):
                    conversation["_processor_unassigned"] = True
                documents.append(document)
                if selected_project_ids is not None:
                    selected_documents.setdefault(None, []).append(document)

        if selected_project_ids is None or None in selected_project_ids:
            self._collect_resources(source_root / "resources", None, resource_paths)
        projects_dir = source_root / "projects"
        used_slugs = {"unassigned"}
        if projects_dir.is_dir():
            for folder in sorted(projects_dir.iterdir()):
                if not folder.is_dir():
                    continue
                if progress is not None:
                    progress(0, 0, (folder / "project.json").relative_to(self.backup_root).as_posix())
                project_data = read_optional_json(folder / "project.json")
                project_data = project_data if isinstance(project_data, dict) else {}
                raw_id = project_data.get("id")
                raw_name = project_data.get("name")
                project_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None
                project_name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else folder.name
                base_slug = slugify(folder.name, limit=64)
                slug = base_slug
                suffix = 2
                while slug.casefold() in used_slugs:
                    slug = slugify(f"{base_slug}_{suffix}")
                    suffix += 1
                used_slugs.add(slug.casefold())
                project = ProjectReference(
                    project_id=project_id,
                    project_name=project_name,
                    source_path=(folder / "project.json").relative_to(self.backup_root).as_posix(),
                    slug=slug,
                )
                if selected_project_ids is not None and (project_id is None or project_id not in selected_project_ids):
                    continue
                projects.append(project)
                if selected_project_ids is not None:
                    selected_project_folders.append((folder, slug))
                for path in sorted((folder / "conversations").glob("*.json")):
                    if progress is not None:
                        progress(0, 0, path.relative_to(self.backup_root).as_posix())
                    document = read_json_file(path)
                    for conversation in self._iter_conversations(document):
                        conversation["project"] = {"id": project_id, "name": project_name}
                        if project_id:
                            conversation["project_id"] = project_id
                        conversation["project_name"] = project_name
                    documents.append(document)
                    if selected_project_ids is not None:
                        selected_documents.setdefault(slug, []).append(document)
                self._collect_resources(folder / "resources", slug, resource_paths)

        if not documents and selected_project_ids is None:
            for path in sorted(source_root.rglob("*.json")):
                if self.process_root in path.parents or not CONVERSATION_PATTERN.match(path.name):
                    continue
                if progress is not None:
                    progress(0, 0, path.relative_to(self.backup_root).as_posix())
                documents.append(read_json_file(path))

        if not documents and selected_project_ids is None:
            raise BackupValidationError("No se encontraron conversaciones JSON en el backup seleccionado.")

        if selected_project_ids is None:
            for path in source_root.rglob("file_*.dat"):
                if self.process_root not in path.parents and path.is_file():
                    resource_paths.setdefault(path, None)
        else:
            if None in selected_project_ids:
                unassigned_roots = (source_root, conversations_dir)
                for root in unassigned_roots:
                    paths = root.glob("file_*.dat") if root == source_root else root.rglob("file_*.dat")
                    for path in paths:
                        if path.is_file():
                            resource_paths.setdefault(path, None)
            for folder, slug in selected_project_folders:
                for path in folder.rglob("file_*.dat"):
                    if path.is_file():
                        resource_paths.setdefault(path, slug)
            self._collect_selected_dat_resources(
                source_root,
                selected_documents,
                selected_project_folders,
                read_optional_json(source_root / "conversation_asset_file_names.json"),
                resource_paths,
            )

        resource_items = sorted(resource_paths.items())
        dat_files: list[DatFileEntry] = []
        for current, (path, slug) in enumerate(resource_items, start=1):
            if progress is not None:
                progress(current - 1, len(resource_items), path.relative_to(self.backup_root).as_posix())
            dat_files.append(self._to_dat_entry(path, slug))
            if progress is not None:
                progress(current, len(resource_items), path.relative_to(self.backup_root).as_posix())
        asset_mapping_path = source_root / "conversation_asset_file_names.json"
        if progress is not None:
            progress(len(resource_items), len(resource_items), asset_mapping_path.name)
        return ScannedBackup(
            source_path=self.backup_root,
            backup_root=self.backup_root,
            extracted_from_zip=False,
            parsed_json={
                "conversation_asset_file_names.json": read_optional_json(asset_mapping_path)
            },
            conversation_documents=documents,
            dat_files=dat_files,
            projects=projects,
        )

    @staticmethod
    def _iter_conversations(document: Any) -> list[dict[str, Any]]:
        if isinstance(document, list):
            return [item for item in document if isinstance(item, dict)]
        if isinstance(document, dict):
            items = document.get("items")
            return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else [document]
        return []

    @staticmethod
    def _collect_resources(root: Path, slug: str | None, found: dict[Path, str | None]) -> None:
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file():
                    found[path] = slug

    def _collect_selected_dat_resources(
        self,
        source_root: Path,
        documents_by_slug: dict[str | None, list[Any]],
        selected_project_folders: list[tuple[Path, str]],
        asset_mapping_json: Any,
        resource_paths: dict[Path, str | None],
    ) -> None:
        mapper = AttachmentMapper()
        mapped_conversations = mapper._extract_asset_mapping(asset_mapping_json)
        conversation_ids: dict[str | None, set[str]] = {}
        asset_ids: dict[str | None, set[str]] = {}
        for slug, documents in documents_by_slug.items():
            ids: set[str] = set()
            assets: set[str] = set()
            for document in documents:
                for conversation in self._iter_conversations(document):
                    conversation_id = mapper._extract_conversation_id(conversation)
                    if conversation_id:
                        ids.add(conversation_id)
                    mapping = conversation.get("mapping")
                    if not isinstance(mapping, dict):
                        continue
                    for node in mapping.values():
                        message = node.get("message") if isinstance(node, dict) else None
                        if not isinstance(message, dict):
                            continue
                        for attachment in mapper._collect_message_attachments(message):
                            for key in ("id", "asset_id", "file_id"):
                                value = attachment.get(key)
                                if isinstance(value, str) and value:
                                    assets.add(value)
                            pointer = attachment.get("asset_pointer")
                            if isinstance(pointer, str):
                                asset_id = mapper._extract_asset_id_from_pointer(pointer)
                                if asset_id:
                                    assets.add(asset_id)
                        pending: list[Any] = [message]
                        while pending:
                            value = pending.pop()
                            if isinstance(value, list):
                                pending.extend(value)
                            elif isinstance(value, dict):
                                for key, item in value.items():
                                    if key in ("asset_id", "file_id") and isinstance(item, str) and item:
                                        assets.add(item)
                                    elif key in ("asset_pointer", "asset_pointer_uri", "dictation_asset_pointer") and isinstance(item, str):
                                        asset_id = mapper._extract_asset_id_from_pointer(item)
                                        if asset_id:
                                            assets.add(asset_id)
                                    if isinstance(item, (dict, list)):
                                        pending.append(item)
            conversation_ids[slug] = ids
            asset_ids[slug] = assets

        mapped_names = {
            slug: {
                name
                for name, linked_ids in mapped_conversations.items()
                if linked_ids & conversation_ids[slug]
            }
            for slug in documents_by_slug
        }
        needed_names = set().union(*mapped_names.values()) if mapped_names else set()
        needed_ids = set().union(*asset_ids.values()) if asset_ids else set()
        if not needed_names and not needed_ids:
            return

        candidates: dict[str, list[Path]] = {}
        for path in resource_paths:
            if path.name.startswith("file_") and path.suffix.lower() == ".dat":
                candidates.setdefault(path.name, []).append(path)

        projects_dir = source_root / "projects"
        for current_root, folders, files in os.walk(source_root):
            current = Path(current_root)
            folders[:] = [
                name for name in folders
                if current / name != self.process_root
            ]
            for name in files:
                if not name.startswith("file_") or not name.endswith(".dat"):
                    continue
                if name not in needed_names and Path(name).stem not in needed_ids:
                    continue
                path = current / name
                paths = candidates.setdefault(name, [])
                if path not in paths:
                    paths.append(path)

        candidates_by_id: dict[str, list[Path]] = {}
        for name, paths in candidates.items():
            candidates_by_id.setdefault(Path(name).stem, []).extend(paths)
        folder_by_slug = {slug: folder for folder, slug in selected_project_folders}
        chosen_by_name: dict[str, Path] = {}
        for slug in documents_by_slug:
            references = [
                (name, candidates.get(name, [])) for name in mapped_names[slug]
            ]
            for asset_id in asset_ids[slug]:
                if any(Path(name).stem == asset_id for name in mapped_names[slug]):
                    continue
                references.append((asset_id, candidates_by_id.get(asset_id, [])))
            own_folder = folder_by_slug.get(slug)
            for reference, possible in references:
                own = [
                    path for path in possible
                    if path.is_relative_to(own_folder)
                ] if own_folder is not None else [
                    path for path in possible
                    if not path.is_relative_to(projects_dir)
                ]
                shared = [
                    path for path in possible
                    if not path.is_relative_to(projects_dir)
                ]
                matches = own or shared
                if len(matches) > 1:
                    raise BackupValidationError(f"Referencia de recurso ambigua: {reference}")
                if not matches:
                    if possible:
                        raise BackupValidationError(
                            f"No se puede atribuir el recurso al proyecto seleccionado: {reference}"
                        )
                    continue
                name = matches[0].name
                previous = chosen_by_name.get(name)
                if previous is not None and previous != matches[0]:
                    raise BackupValidationError(f"Referencia de recurso ambigua: {name}")
                chosen_by_name[name] = matches[0]
                resource_paths.setdefault(matches[0], slug if matches[0] in own else None)
        for name, chosen in chosen_by_name.items():
            if any(path.name == name and path != chosen for path in resource_paths):
                raise BackupValidationError(f"Referencia de recurso ambigua: {name}")

    def _to_dat_entry(self, path: Path, project_slug: str | None) -> DatFileEntry:
        analysis = self.dat_analyzer.analyze(path)
        return DatFileEntry(
            name=path.name,
            relative_path=path.relative_to(self.backup_root).as_posix(),
            size_bytes=path.stat().st_size,
            mime_type=analysis.mime_type,
            file_type=analysis.file_type,
            probable_extension=analysis.probable_extension,
            sha256=analysis.sha256,
            detector_used=analysis.detector_used,
            project_slug=project_slug,
        )

    @staticmethod
    def _assign_projects(conversations: list[Any], detected: list[ProjectReference]) -> list[ProjectReference]:
        projects = list(detected)
        used_slugs = {"unassigned", *(item.slug.casefold() for item in projects if item.slug)}
        by_id = {item.project_id: item for item in projects if item.project_id}
        by_name = {item.project_name.casefold(): item for item in projects if item.project_name}

        for conversation in conversations:
            reference = conversation.project
            if reference is None:
                continue
            project = by_id.get(reference.project_id) if reference.project_id else None
            if project is None and reference.project_name:
                project = by_name.get(reference.project_name.casefold())
            if project is None:
                name = reference.project_name or reference.project_id or "project"
                base_slug = slugify(name, limit=64)
                slug = base_slug
                suffix = 2
                while slug.casefold() in used_slugs:
                    slug = slugify(f"{base_slug}_{suffix}")
                    suffix += 1
                used_slugs.add(slug.casefold())
                project = ProjectReference(
                    project_id=reference.project_id,
                    project_name=reference.project_name or name,
                    source_path=reference.source_path,
                    slug=slug,
                )
                projects.append(project)
                if project.project_id:
                    by_id[project.project_id] = project
                if project.project_name:
                    by_name[project.project_name.casefold()] = project
            conversation.project = project
            for message in conversation.messages:
                for resource in message.resources:
                    resource.project = project
        return projects

    @staticmethod
    def _build_index(
        conversations: list[Any],
        projects: list[ProjectReference],
        locations: dict[tuple[str, str], str],
        orphan_resources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        project_entries: list[dict[str, Any]] = []
        conversation_entries: list[dict[str, Any]] = []
        resource_entries: list[dict[str, Any]] = []
        for project in [*projects, ProjectReference(project_name="unassigned", slug="unassigned")]:
            slug = project.slug or "unassigned"
            grouped = [item for item in conversations if (item.project.slug if item.project else "unassigned") == slug]
            project_entries.append({
                "project_id": project.project_id,
                "name": project.project_name,
                "slug": slug,
                "conversation_count": len(grouped),
            })

        for conversation in conversations:
            slug = conversation.project.slug if conversation.project and conversation.project.slug else "unassigned"
            conversation_entries.append({
                "conversation_id": conversation.conversation_id,
                "openai_conversation_id": conversation.openai_conversation_id,
                "title": conversation.title,
                "project_slug": slug,
                "path": locations[(slug, conversation.conversation_id)],
                "message_count": len(conversation.messages),
                "resource_count": sum(len(message.resources) for message in conversation.messages),
            })
            for message in conversation.messages:
                for resource in message.resources:
                    resource_entries.append({
                        "resource_id": resource.resource_id,
                        "openai_asset_id": resource.openai_asset_id,
                        "openai_file_id": resource.openai_file_id,
                        "conversation_id": conversation.conversation_id,
                        "message_id": message.message_id,
                        "project_slug": slug,
                        "project_id": conversation.project.project_id if conversation.project else None,
                        "project_name": conversation.project.project_name if conversation.project else None,
                        "original_name": resource.original_name,
                        "mime_type": resource.mime_type,
                        "kind": resource.kind,
                        "source_path": resource.source_path,
                        "relative_resource_path": resource.relative_resource_path,
                    })
        projects_by_slug = {item.slug: item for item in projects}
        for resource in orphan_resources:
            project = projects_by_slug.get(resource["project_slug"])
            resource_entries.append({
                **resource,
                "project_id": project.project_id if project else None,
                "project_name": project.project_name if project else None,
            })
        return {
            "schema_version": 1,
            "projects": project_entries,
            "conversations": conversation_entries,
            "resources": resource_entries,
        }
