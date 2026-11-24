from __future__ import annotations

from datetime import datetime
from hashlib import sha1
import importlib.util
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
from time import monotonic
from typing import Any, Callable, NamedTuple
from uuid import uuid4

from core.html_exporter import (
    _html_conversation_id,
    export_conversation as export_html_conversation,
    export_index,
)
from core.ids import build_conversation_filename, slugify
from core.obsidian_exporter import export_conversation
from core.package_reader import ProjectPackageReadError, ProjectPackageReader


class ExportError(Exception):
    pass


class _PreparedExport(NamedTuple):
    process_root: Path
    resource_root: Path
    conversations: list[Any]
    loaded_conversations: dict[int, dict[str, Any]]
    conversation_paths: dict[int, Path]
    resource_map: dict[str, str]
    resource_sources: dict[str, Path]
    exported_to_source: dict[tuple[str, str], str]
    resources_by_path: dict[str, list[dict[str, Any]]]
    conversation_titles: dict[str, str]
    reported_missing_resources: set[str]
    new_export_state: dict[str, dict[str, Any]]
    export_state_path: Path
    project_count: int
    projects: list[Any]


def export_obsidian(
    backup_root: Path,
    *,
    convert_png: bool,
    convert_wav: bool,
    preserve_transparent_png: bool,
    convert_m4a: bool,
    include_conversation_audio: bool,
    include_user_attachments: bool = False,
    include_project_tag: bool,
    custom_roles: bool,
    user_role: str,
    assistant_role: str,
    selected_project_ids: set[str | None] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    backup_root = backup_root.resolve()
    export_root = backup_root / "export"
    logs_root = export_root / "logs"

    if export_root.is_symlink():
        raise ExportError(f"La carpeta de exportación no puede ser un enlace simbólico: {export_root}")
    if export_root.exists() and not export_root.is_dir():
        raise ExportError(f"La ruta de exportación no es una carpeta: {export_root}")
    if logs_root.is_symlink():
        raise ExportError(f"La carpeta de logs no puede ser un enlace simbólico: {logs_root}")
    if logs_root.exists() and not logs_root.is_dir():
        raise ExportError(f"La ruta de logs no es una carpeta: {logs_root}")

    logs_root.mkdir(parents=True, exist_ok=True)
    logger, handler, log_path = _create_export_logger(logs_root)
    export_started_at = monotonic()
    audit_state: dict[str, Any] = {
        "processed_resources": 0,
        "copied_resources": 0,
        "converted_resources": 0,
        "reused_resources": 0,
        "missing_resources": 0,
        "markdown_files": 0,
        "warnings": 0,
        "errors": 0,
        "broken_links": 0,
    }

    try:
        logger.info("EXPORT_START")
        logger.info("started_at=%s", datetime.now().astimezone().isoformat(timespec="seconds"))
        logger.info("format=obsidian")
        logger.info("option_png_to_jpg=%s", convert_png)
        logger.info("option_wav_to_mp3=%s", convert_wav)
        logger.info("option_preserve_transparent_png=%s", preserve_transparent_png)
        logger.info("option_m4a_to_mp3=%s", convert_m4a)
        logger.info("option_include_conversation_audio=%s", include_conversation_audio)
        logger.info("option_include_user_attachments=%s", include_user_attachments)
        logger.info("option_include_project_tag=%s", include_project_tag)
        logger.info("option_custom_roles=%s", custom_roles)
        logger.info("option_user_role=%s", user_role)
        logger.info("option_assistant_role=%s", assistant_role)
        logger.info("source_process=%s", backup_root / "process")
        logger.info("destination=%s", export_root / "obsidian")
        logger.info("log_file=%s", log_path)

        result = _run_obsidian_export(
            backup_root,
            convert_png=convert_png,
            convert_wav=convert_wav,
            preserve_transparent_png=preserve_transparent_png,
            convert_m4a=convert_m4a,
            include_conversation_audio=include_conversation_audio,
            include_user_attachments=include_user_attachments,
            include_project_tag=include_project_tag,
            custom_roles=custom_roles,
            user_role=user_role,
            assistant_role=assistant_role,
            selected_project_ids=selected_project_ids,
            progress=progress,
            logger=logger,
            state=audit_state,
        )
        logger.info(
            "EXPORT_SUMMARY status=%s elapsed_seconds=%.3f resources_processed=%s resources_copied=%s "
            "resources_converted=%s resources_reused=%s resources_missing=%s markdown_files=%s warnings=%s errors=%s broken_links=%s",
            result["status"],
            monotonic() - export_started_at,
            result["processed_resources"],
            result["copied_resources"],
            result["converted_resources"],
            result["reused_resources"],
            result["missing_resources"],
            result["markdown_files"],
            result["warnings"],
            result["errors"],
            result["broken_links"],
        )
        return result
    except Exception as error:
        audit_state["errors"] = max(1, int(audit_state["errors"]))
        logger.error(
            "EXPORT_SUMMARY status=failed elapsed_seconds=%.3f resources_processed=%s resources_copied=%s "
            "resources_converted=%s resources_reused=%s resources_missing=%s markdown_files=%s warnings=%s errors=%s broken_links=%s",
            monotonic() - export_started_at,
            audit_state["processed_resources"],
            audit_state["copied_resources"],
            audit_state["converted_resources"],
            audit_state["reused_resources"],
            audit_state["missing_resources"],
            audit_state["markdown_files"],
            audit_state["warnings"],
            audit_state["errors"],
            audit_state["broken_links"],
        )
        logger.exception(
            "EXPORT_FAILED elapsed_seconds=%.3f reason=%s",
            monotonic() - export_started_at,
            error,
        )
        raise
    finally:
        logger.removeHandler(handler)
        handler.close()


def export_html(
    backup_root: Path,
    *,
    convert_png: bool,
    convert_wav: bool,
    preserve_transparent_png: bool,
    convert_m4a: bool,
    include_conversation_audio: bool,
    include_user_attachments: bool = False,
    include_project_tag: bool,
    custom_roles: bool,
    user_role: str,
    assistant_role: str,
    selected_project_ids: set[str | None] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    backup_root = backup_root.resolve()
    export_root = backup_root / "export"
    logs_root = export_root / "logs"

    if export_root.is_symlink():
        raise ExportError(f"La carpeta de exportación no puede ser un enlace simbólico: {export_root}")
    if export_root.exists() and not export_root.is_dir():
        raise ExportError(f"La ruta de exportación no es una carpeta: {export_root}")
    if logs_root.is_symlink():
        raise ExportError(f"La carpeta de logs no puede ser un enlace simbólico: {logs_root}")
    if logs_root.exists() and not logs_root.is_dir():
        raise ExportError(f"La ruta de logs no es una carpeta: {logs_root}")

    logs_root.mkdir(parents=True, exist_ok=True)
    logger, handler, log_path = _create_export_logger(logs_root, "html")
    export_started_at = monotonic()
    audit_state: dict[str, Any] = {
        "processed_resources": 0,
        "copied_resources": 0,
        "converted_resources": 0,
        "reused_resources": 0,
        "missing_resources": 0,
        "html_files": 0,
        "warnings": 0,
        "errors": 0,
        "broken_links": 0,
    }

    try:
        logger.info("EXPORT_START")
        logger.info("started_at=%s", datetime.now().astimezone().isoformat(timespec="seconds"))
        logger.info("format=html")
        logger.info("option_png_to_jpg=%s", convert_png)
        logger.info("option_wav_to_mp3=%s", convert_wav)
        logger.info("option_preserve_transparent_png=%s", preserve_transparent_png)
        logger.info("option_m4a_to_mp3=%s", convert_m4a)
        logger.info("option_include_conversation_audio=%s", include_conversation_audio)
        logger.info("option_include_user_attachments=%s", include_user_attachments)
        logger.info("option_include_project_tag=%s", include_project_tag)
        logger.info("option_custom_roles=%s", custom_roles)
        logger.info("option_user_role=%s", user_role)
        logger.info("option_assistant_role=%s", assistant_role)
        logger.info("source_process=%s", backup_root / "process")
        logger.info("destination=%s", export_root / "html")
        logger.info("log_file=%s", log_path)

        result = _run_html_export(
            backup_root,
            convert_png=convert_png,
            convert_wav=convert_wav,
            preserve_transparent_png=preserve_transparent_png,
            convert_m4a=convert_m4a,
            include_conversation_audio=include_conversation_audio,
            include_user_attachments=include_user_attachments,
            include_project_tag=include_project_tag,
            custom_roles=custom_roles,
            user_role=user_role,
            assistant_role=assistant_role,
            selected_project_ids=selected_project_ids,
            progress=progress,
            logger=logger,
            state=audit_state,
        )
        logger.info(
            "EXPORT_SUMMARY status=%s elapsed_seconds=%.3f resources_processed=%s resources_copied=%s "
            "resources_converted=%s resources_reused=%s resources_missing=%s html_files=%s warnings=%s errors=%s broken_links=%s",
            result["status"],
            monotonic() - export_started_at,
            result["processed_resources"],
            result["copied_resources"],
            result["converted_resources"],
            result["reused_resources"],
            result["missing_resources"],
            result["html_files"],
            result["warnings"],
            result["errors"],
            result["broken_links"],
        )
        return result
    except Exception as error:
        audit_state["errors"] = max(1, int(audit_state["errors"]))
        logger.error(
            "EXPORT_SUMMARY status=failed elapsed_seconds=%.3f resources_processed=%s resources_copied=%s "
            "resources_converted=%s resources_reused=%s resources_missing=%s html_files=%s warnings=%s errors=%s broken_links=%s",
            monotonic() - export_started_at,
            audit_state["processed_resources"],
            audit_state["copied_resources"],
            audit_state["converted_resources"],
            audit_state["reused_resources"],
            audit_state["missing_resources"],
            audit_state["html_files"],
            audit_state["warnings"],
            audit_state["errors"],
            audit_state["broken_links"],
        )
        logger.exception(
            "EXPORT_FAILED elapsed_seconds=%.3f reason=%s",
            monotonic() - export_started_at,
            error,
        )
        raise
    finally:
        logger.removeHandler(handler)
        handler.close()


def _prepare_export(
    backup_root: Path,
    *,
    output_root: Path,
    convert_png: bool,
    convert_wav: bool,
    preserve_transparent_png: bool,
    convert_m4a: bool,
    include_conversation_audio: bool,
    include_user_attachments: bool,
    selected_project_ids: set[str | None] | None,
    logger: logging.Logger,
    state: dict[str, Any],
    emit: Callable[..., None],
) -> _PreparedExport:
    process_root = backup_root / "process"
    catalog_path = process_root / "index.json"
    projects_root = process_root / "projects"
    if (
        process_root.is_symlink()
        or catalog_path.is_symlink()
        or projects_root.is_symlink()
        or (process_root / "data").exists()
    ):
        raise ExportError("La representación process no puede usar enlaces simbólicos ni la estructura antigua.")
    if not catalog_path.is_file() or not projects_root.is_dir():
        raise ExportError("No existen paquetes procesados en la carpeta process.")

    catalog = _read_json(catalog_path, "process/index.json")
    catalog_projects = catalog.get("projects")
    if not isinstance(catalog_projects, list):
        raise ExportError("El catálogo de proyectos no tiene la estructura esperada.")

    package_data: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
    for catalog_entry in catalog_projects:
        if not isinstance(catalog_entry, dict):
            raise ExportError("El catálogo contiene un proyecto inválido.")
        project_id = catalog_entry.get("project_id")
        relative_path = catalog_entry.get("path")
        if not isinstance(project_id, str) or not project_id or not isinstance(relative_path, str):
            raise ExportError("El catálogo contiene una identidad de proyecto inválida.")
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts or relative.parts != ("projects", project_id):
            raise ExportError("La ruta de un paquete no es válida.")
        package_root = (process_root / Path(*relative.parts)).resolve()
        if not package_root.is_relative_to(projects_root.resolve()):
            raise ExportError("Un paquete sale de la carpeta process.")
        if selected_project_ids is not None and not (
            project_id in selected_project_ids
            or (project_id == "unassigned" and None in selected_project_ids)
        ):
            continue
        try:
            package = ProjectPackageReader(package_root).read()
        except ProjectPackageReadError as error:
            raise ExportError(f"No se pudo leer el paquete {project_id}: {error}") from error
        manifest = package["manifest"]
        if manifest.get("project_id") != project_id or package_root.name != project_id:
            raise ExportError("La identidad de un paquete no coincide con el catálogo.")
        package_data.append((catalog_entry, package, package_root))

    package_data.sort(key=lambda item: str(item[0]["project_id"]))
    project_bases: dict[str, list[str]] = {}
    for catalog_entry, package, _ in package_data:
        project_id = str(catalog_entry["project_id"])
        project_name = package["manifest"].get("project_name")
        if not isinstance(project_name, str) or not project_name.strip():
            raise ExportError(f"El paquete {project_id} no tiene un nombre de proyecto válido.")
        base = slugify(project_name, limit=52) or "proyecto"
        project_bases.setdefault(base.casefold(), []).append(project_id)

    export_slugs: dict[str, str] = {}
    for catalog_entry, package, _ in package_data:
        project_id = str(catalog_entry["project_id"])
        project_name = str(package["manifest"]["project_name"])
        base = slugify(project_name, limit=52) or "proyecto"
        if len(project_bases[base.casefold()]) > 1:
            base = f"{base[:55]}--{sha1(project_id.encode('utf-8')).hexdigest()[:8]}"
        export_slugs[project_id] = base

    conversations: list[Any] = []
    projects: list[Any] = []
    indexed_resources: list[Any] = []
    loaded_conversations: dict[int, dict[str, Any]] = {}
    conversation_paths: dict[int, Path] = {}
    resource_sources: dict[str, Path] = {}
    for catalog_entry, package, _ in package_data:
        project_id = str(catalog_entry["project_id"])
        project_name = str(package["manifest"]["project_name"])
        export_slug = export_slugs[project_id]
        projects.append({"project_id": project_id, "name": project_name, "slug": export_slug})
        for item in package["conversations"]:
            entry = dict(item["entry"])
            entry.update(project_slug=export_slug, project_id=project_id, project_name=project_name)
            position = len(conversations)
            conversations.append(entry)
            conversation_paths[position] = item["path"]
            conversation = _read_json(item["path"], str(entry.get("title") or "Conversación"))
            _namespace_conversation_resources(conversation, project_id)
            loaded_conversations[position] = conversation
        for item in package["resources"]:
            entry = dict(item["entry"])
            _namespace_resource_paths(entry, project_id)
            entry.update(project_slug=export_slug, project_id=project_id, project_name=project_name)
            indexed_resources.append(entry)
            source_key = _resource_source_key(entry)
            source_path = item["path"]
            if source_key and source_path is not None:
                previous = resource_sources.get(source_key)
                if previous is not None and previous != source_path:
                    raise ExportError(f"Un paquete contiene recursos incompatibles: {source_key}")
                resource_sources[source_key] = source_path

    prepare_block_count = (
        4
        + (0 if include_conversation_audio else 3)
        + (0 if include_user_attachments else 2)
    )
    prepare_block_index = 0

    def emit_prepare(current: int, total: int, *, block_completed: bool = False) -> None:
        block_ratio = current / total if total else (1.0 if block_completed else 0.0)
        progress_ratio = (prepare_block_index + block_ratio) / prepare_block_count
        emit(
            "prepare",
            "Preparar exportación",
            current,
            total,
            0,
            10,
            progress_ratio=progress_ratio,
        )

    def finish_prepare_block() -> None:
        nonlocal prepare_block_index
        prepare_block_index += 1

    def begin_prepare_block(total: int) -> bool:
        emit_prepare(0, total)
        if not total:
            emit_prepare(0, 0, block_completed=True)
        return True

    excluded_dictation_paths: set[str] = set()
    if not include_conversation_audio:
        dictation_paths: set[str] = set()
        dictation_resource_ids: set[str] = set()
        indexed_resource_total = len(indexed_resources)
        if begin_prepare_block(indexed_resource_total):
            for current, resource in enumerate(indexed_resources, start=1):
                if isinstance(resource, dict) and _is_dictation_audio_resource(resource):
                    resource_id = resource.get("resource_id")
                    if isinstance(resource_id, str) and resource_id:
                        dictation_resource_ids.add(resource_id)
                    source_key = _resource_source_key(resource)
                    if source_key:
                        dictation_paths.add(source_key)
                state["current_item"] = str(
                    resource.get("relative_resource_path") or resource.get("resource_id") or "recurso"
                ) if isinstance(resource, dict) else "recurso"
                emit_prepare(current, indexed_resource_total)
            finish_prepare_block()

        conversation_total = len(conversations)
        if begin_prepare_block(conversation_total):
            for conversation_index, entry in enumerate(conversations, start=1):
                if not isinstance(entry, dict):
                    state["current_item"] = "conversación"
                    emit_prepare(conversation_index, conversation_total)
                    continue
                try:
                    conversation = loaded_conversations[conversation_index - 1]
                except KeyError:
                    state["current_item"] = str(entry.get("title") or "conversación")
                    emit_prepare(conversation_index, conversation_total)
                    continue
                loaded_conversations[conversation_index - 1] = conversation
                messages = conversation.get("messages")
                if isinstance(messages, list):
                    for message in messages:
                        if not isinstance(message, dict):
                            continue
                        resources = message.get("resources")
                        if not isinstance(resources, list):
                            continue
                        for resource in resources:
                            if not _is_dictation_audio_resource(resource):
                                continue
                            resource_id = resource.get("resource_id")
                            if isinstance(resource_id, str) and resource_id:
                                dictation_resource_ids.add(resource_id)
                            source_key = _resource_source_key(resource)
                            if source_key:
                                dictation_paths.add(source_key)
                state["current_item"] = str(entry.get("title") or "conversación")
                emit_prepare(conversation_index, conversation_total)
            finish_prepare_block()

        filtered_resources: list[Any] = []
        other_resource_paths: set[str] = set()
        indexed_resource_total = len(indexed_resources)
        if begin_prepare_block(indexed_resource_total):
            for current, resource in enumerate(indexed_resources, start=1):
                excluded = _is_dictation_audio_resource(resource) or (
                    isinstance(resource, dict)
                    and (
                        (
                            isinstance(resource.get("resource_id"), str)
                            and resource["resource_id"] in dictation_resource_ids
                        )
                        or (
                            not resource.get("resource_id")
                            and _resource_source_key(resource) in dictation_paths
                        )
                    )
                )
                if not excluded:
                    filtered_resources.append(resource)
                    if isinstance(resource, dict):
                        source_key = _resource_source_key(resource)
                        if source_key:
                            other_resource_paths.add(source_key)
                state["current_item"] = str(
                    resource.get("relative_resource_path") or resource.get("resource_id") or "recurso"
                ) if isinstance(resource, dict) else "recurso"
                emit_prepare(current, indexed_resource_total)
            finish_prepare_block()
        indexed_resources = filtered_resources
        excluded_dictation_paths = dictation_paths - other_resource_paths

    excluded_user_attachment_paths: set[str] = set()
    if not include_user_attachments:
        user_attachment_paths: set[str] = set()
        other_resource_paths: set[str] = set()
        excluded_associations: set[tuple[str, str, str, str]] = set()
        conversation_total = len(conversations)
        if begin_prepare_block(conversation_total):
            for conversation_index, entry in enumerate(conversations, start=1):
                conversation = loaded_conversations.get(conversation_index - 1)
                project_id = str(entry.get("project_id") or "") if isinstance(entry, dict) else ""
                messages = conversation.get("messages") if isinstance(conversation, dict) else None
                if isinstance(messages, list):
                    for message in messages:
                        if not isinstance(message, dict):
                            continue
                        is_user_message = message.get("role") == "user"
                        resources = message.get("resources")
                        if not isinstance(resources, list):
                            continue
                        for resource in resources:
                            if not isinstance(resource, dict):
                                continue
                            source_key = _resource_source_key(resource)
                            if is_user_message and _is_user_attachment_resource(resource):
                                if source_key:
                                    user_attachment_paths.add(source_key)
                                else:
                                    excluded_associations.add(
                                        _resource_association_key(resource, project_id)
                                    )
                            elif source_key:
                                other_resource_paths.add(source_key)
                state["current_item"] = str(
                    entry.get("title") or "conversación"
                ) if isinstance(entry, dict) else "conversación"
                emit_prepare(conversation_index, conversation_total)
            finish_prepare_block()

        excluded_user_attachment_paths = user_attachment_paths - other_resource_paths
        filtered_resources = []
        indexed_resource_total = len(indexed_resources)
        if begin_prepare_block(indexed_resource_total):
            for current, resource in enumerate(indexed_resources, start=1):
                excluded = False
                if isinstance(resource, dict):
                    source_key = _resource_source_key(resource)
                    excluded = source_key in excluded_user_attachment_paths or (
                        not source_key
                        and _resource_association_key(
                            resource, str(resource.get("project_id") or "")
                        ) in excluded_associations
                    )
                if not excluded:
                    filtered_resources.append(resource)
                state["current_item"] = str(
                    resource.get("relative_resource_path") or resource.get("resource_id") or "recurso"
                ) if isinstance(resource, dict) else "recurso"
                emit_prepare(current, indexed_resource_total)
            finish_prepare_block()
        indexed_resources = filtered_resources

    logger.info(
        "EXPORT_DATA projects=%s conversations=%s indexed_resources=%s",
        len(projects),
        len(conversations),
        len(indexed_resources),
    )

    conversation_titles = {
        str(entry.get("conversation_id")): str(entry.get("title") or entry.get("conversation_id"))
        for entry in conversations
        if isinstance(entry, dict) and entry.get("conversation_id")
    }
    resources_by_path: dict[str, list[dict[str, Any]]] = {}
    for resource in indexed_resources:
        if not isinstance(resource, dict):
            continue
        relative = resource.get("relative_resource_path")
        if isinstance(relative, str) and relative.strip():
            resources_by_path.setdefault(Path(relative).as_posix(), []).append(resource)

    begin_prepare_block(1)
    resource_files = sorted(
        (source_key, source_path)
        for source_key, source_path in resource_sources.items()
        if source_key not in excluded_dictation_paths
        and source_key not in excluded_user_attachment_paths
    )
    state["current_item"] = str(projects_root)
    emit_prepare(1, 1)
    finish_prepare_block()
    project_directories = [project for project in projects if isinstance(project, dict)]
    plans: list[tuple[Path, Path, str, str, bool, str | None, str, int, int]] = []
    target_sources: dict[str, Path] = {}
    begin_prepare_block(len(resource_files))
    needs_png = convert_png and any(source.suffix.lower() == ".png" for _, source in resource_files)
    if needs_png and importlib.util.find_spec("PIL") is None:
        raise ExportError("La conversión PNG a JPG o la comprobación de transparencia requiere Pillow.")
    needs_audio = False
    for source_key, source in resource_files:
        source_parts = Path(source_key).parts
        if len(source_parts) < 2:
            raise ExportError(f"La ruta de recurso no identifica un paquete: {source_key}")
        project_id = source_parts[0]
        project_directory_name = export_slugs.get(project_id)
        if project_directory_name is None:
            raise ExportError(f"No existe el proyecto del recurso: {source_key}")
        relative = Path(*source_parts[1:])
        suffix = source.suffix.lower()
        preserve_png = (
            convert_png
            and preserve_transparent_png
            and suffix == ".png"
            and _png_has_transparency(source)
        )
        converting_png = convert_png and suffix == ".png" and not preserve_png
        converting_audio = (convert_wav and suffix == ".wav") or (convert_m4a and suffix == ".m4a")
        converted = converting_png or converting_audio
        conversion_kind = "png" if converting_png else "audio" if converting_audio else None
        treatment = (
            "png_to_jpg" if converting_png else
            "audio_to_mp3" if converting_audio else
            "png_preserved" if preserve_png else
            "copy"
        )
        target_relative = relative.with_suffix(".jpg") if converting_png else relative
        if converting_audio:
            target_relative = relative.with_suffix(".mp3")
        target_key = f"{project_directory_name}/{target_relative.as_posix()}"
        previous = target_sources.get(target_key.casefold())
        if previous is not None and previous != source:
            raise ExportError(f"Dos recursos producirían la misma ruta exportada: {target_key}")
        target_sources[target_key.casefold()] = source
        source_stat = source.stat()
        plans.append((
            source, target_relative, source_key, project_directory_name, converted, conversion_kind,
            treatment, source_stat.st_size, source_stat.st_mtime_ns,
        ))
        state["current_item"] = source.name
        emit_prepare(len(plans), len(resource_files))
        needs_audio = needs_audio or converting_audio
    finish_prepare_block()

    ffmpeg_path = shutil.which("ffmpeg") if needs_audio else None
    if needs_audio and ffmpeg_path is None:
        raise ExportError("La conversión de WAV/M4A a MP3 requiere FFmpeg disponible en PATH.")

    export_root = output_root.parent
    export_state_path = output_root / "export_state.json"
    if export_root.is_symlink() or output_root.is_symlink():
        raise ExportError(f"La salida export/{output_root.name} no puede usar enlaces simbólicos.")
    if export_root.exists() and not export_root.is_dir():
        raise ExportError("La ruta export existe y no es una carpeta.")
    if output_root.exists() and not output_root.is_dir():
        raise ExportError(f"La ruta export/{output_root.name} existe y no es una carpeta.")
    if export_state_path.is_symlink():
        raise ExportError("El estado de exportación no puede ser un enlace simbólico.")
    previous_export_state = _load_export_state(export_state_path)
    output_root.mkdir(parents=True, exist_ok=True)
    begin_prepare_block(len(project_directories))
    project_current = 0
    for project in projects:
        if isinstance(project, dict):
            slug = str(project.get("slug") or "unassigned")
            (output_root / slugify(slug, limit=64)).mkdir(parents=True, exist_ok=True)
            project_current += 1
            state["project"] = slug
            state["current_item"] = slug
            emit_prepare(project_current, len(project_directories))
    finish_prepare_block()
    prepared_resource_directories = sorted({
        (project_directory_name, target_relative.parent)
        for _, target_relative, _, project_directory_name, _, _, _, _, _ in plans
    })
    begin_prepare_block(len(prepared_resource_directories))
    resource_directory_current = 0
    for project_directory_name, resource_directory in prepared_resource_directories:
        (output_root / project_directory_name / "resources" / resource_directory).mkdir(parents=True, exist_ok=True)
        resource_directory_current += 1
        state["project"] = project_directory_name
        state["current_item"] = resource_directory.as_posix()
        emit_prepare(resource_directory_current, len(prepared_resource_directories))
    finish_prepare_block()
    state["total_resources"] = len(plans)

    resource_map: dict[str, str] = {}
    exported_to_source: dict[tuple[str, str], str] = {}
    reported_missing_resources: set[str] = set()
    resource_errors: list[str] = []
    total_resources = len(plans)
    state["project"] = None
    state["current_item"] = None
    emit("resources", "Procesar recursos", 0, total_resources, 10, 65)
    new_export_state: dict[str, dict[str, Any]] = {}
    for current, (
        source, target_relative, source_key, project_directory_name, converted, conversion_kind,
        treatment, source_size, source_mtime_ns,
    ) in enumerate(plans, start=1):
        project = project_directory_name
        state["project"] = project
        state["current_item"] = source.name
        logger.info(
            "RESOURCE_START project=%s resource=%s processed=%s total=%s",
            project,
            source.name,
            current - 1,
            total_resources,
        )
        emit("resources", "Procesar recursos", current - 1, total_resources, 10, 65)
        project_resource_relative = target_relative
        destination = output_root / project_directory_name / "resources" / target_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        resource_reference = f"resources/{project_resource_relative.as_posix()}"
        exported_path = f"{project_directory_name}/{resource_reference}"
        previous_entry = previous_export_state.get(source_key)
        reusable = (
            isinstance(previous_entry, dict)
            and previous_entry.get("source_size") == source_size
            and previous_entry.get("source_mtime_ns") == source_mtime_ns
            and previous_entry.get("exported_path") == exported_path
            and previous_entry.get("treatment") == treatment
            and destination.is_file()
        )
        if reusable:
            resource_map[source_key] = resource_reference
            exported_to_source[(project_directory_name, resource_reference)] = source_key
            new_export_state[source_key] = {
                "source_key": source_key,
                "source_size": source_size,
                "source_mtime_ns": source_mtime_ns,
                "exported_path": exported_path,
                "treatment": treatment,
            }
            state["reused_resources"] += 1
            state["processed_resources"] = current
            logger.info("RESOURCE_REUSED source=%s exported_path=%s treatment=%s", source_key, exported_path, treatment)
            logger.info(
                "RESOURCE_DONE project=%s resource=%s processed=%s total=%s",
                project,
                source.name,
                current,
                total_resources,
            )
            emit("resources", "Procesar recursos", current, total_resources, 10, 65)
            continue

        converting_audio = conversion_kind == "audio"
        conversion_started_at = monotonic() if converting_audio else None
        conversion_log_name = f"{source.suffix[1:].upper()}_CONVERSION" if converting_audio else None
        resource_ids = sorted({
            str(resource.get("resource_id"))
            for resource in resources_by_path.get(source_key, [])
            if resource.get("resource_id")
        })
        logged_resource_ids = ",".join(resource_ids) or "None"
        try:
            if conversion_kind == "png":
                _convert_png_to_jpg(source, destination)
            elif converting_audio:
                _convert_audio_to_mp3(source, destination, ffmpeg_path or "ffmpeg")
            else:
                shutil.copy2(source, destination)
        except (OSError, RuntimeError, ValueError) as error:
            if destination.exists():
                destination.unlink()
            detail = f"{source.name}: {error}"
            resource_errors.append(detail)
            state["errors"] = len(resource_errors)
            state["last_error"] = detail
            if converting_audio:
                logger.error(
                    "%s source=%s destination=%s project=%s resource_id=%s "
                    "result=failed duration_seconds=%.3f reason=%s",
                    conversion_log_name,
                    source,
                    destination,
                    project,
                    logged_resource_ids,
                    monotonic() - (conversion_started_at or monotonic()),
                    error,
                )
            for resource in resources_by_path.get(source_key) or [None]:
                _log_resource_error(
                    logger,
                    "resource_conversion" if converted else "resource_copy",
                    resource,
                    conversation_titles,
                    source,
                    resource_reference,
                    str(error),
                )
        else:
            resource_map[source_key] = resource_reference
            exported_to_source[(project_directory_name, resource_reference)] = source_key
            new_export_state[source_key] = {
                "source_key": source_key,
                "source_size": source_size,
                "source_mtime_ns": source_mtime_ns,
                "exported_path": exported_path,
                "treatment": treatment,
            }
            if isinstance(previous_entry, dict) and previous_entry.get("exported_path") != exported_path:
                _remove_recorded_export(output_root, previous_entry.get("exported_path"))
            if converting_audio:
                logger.info(
                    "%s source=%s destination=%s project=%s resource_id=%s "
                    "result=success duration_seconds=%.3f",
                    conversion_log_name,
                    source,
                    destination,
                    project,
                    logged_resource_ids,
                    monotonic() - (conversion_started_at or monotonic()),
                )
            if converted:
                state["converted_resources"] += 1
                logger.info(
                    "RESOURCE_CONVERTED project=%s resource=%s exported_path=%s",
                    project,
                    source.name,
                    resource_map[source_key],
                )
            else:
                state["copied_resources"] += 1
                logger.info(
                    "RESOURCE_COPIED project=%s resource=%s exported_path=%s",
                    project,
                    source.name,
                    resource_map[source_key],
                )
        state["processed_resources"] = current
        logger.info(
            "RESOURCE_DONE project=%s resource=%s processed=%s total=%s",
            project,
            source.name,
            current,
            total_resources,
        )
        emit("resources", "Procesar recursos", current, total_resources, 10, 65)

    if resource_errors:
        raise ExportError(
            f"Falló la exportación de {len(resource_errors)} recurso(s). Último error: {resource_errors[-1]}"
        )

    retained_export_paths = {
        entry["exported_path"] for entry in new_export_state.values()
    }
    selected_export_directories = {
        slugify(str(project.get("slug") or "unassigned"), limit=64)
        for project in projects
        if isinstance(project, dict)
    }
    for source_key, previous_entry in previous_export_state.items():
        previous_path = previous_entry.get("exported_path")
        previous_directory = (
            PurePosixPath(previous_path).parts[0]
            if isinstance(previous_path, str) and PurePosixPath(previous_path).parts
            else None
        )
        if (
            source_key not in new_export_state
            and previous_path not in retained_export_paths
            and (selected_project_ids is None or previous_directory in selected_export_directories)
        ):
            _remove_recorded_export(output_root, previous_entry.get("exported_path"))

    for position, resource in enumerate(indexed_resources, start=1):
        if not isinstance(resource, dict):
            continue
        relative = resource.get("relative_resource_path")
        source_key = Path(relative).as_posix() if isinstance(relative, str) and relative.strip() else ""
        if source_key and source_key in resource_map:
            continue
        identity = _resource_identity(resource, source_key, position)
        if identity in reported_missing_resources:
            continue
        reported_missing_resources.add(identity)
        state["missing_resources"] += 1
        state["warnings"] += 1
        source_resource = resource_sources.get(source_key)
        _log_resource_error(
            logger,
            "missing_source",
            resource,
            conversation_titles,
            source_resource,
            _expected_export_path(
                source_key,
                convert_png,
                convert_wav,
                preserve_transparent_png,
                convert_m4a,
                source_resource,
            ),
            "No existe un archivo de origen materializado para el recurso",
            warning=True,
        )

    return _PreparedExport(
        process_root=process_root,
        resource_root=projects_root,
        conversations=conversations,
        loaded_conversations=loaded_conversations,
        conversation_paths=conversation_paths,
        resource_map=resource_map,
        resource_sources=resource_sources,
        exported_to_source=exported_to_source,
        resources_by_path=resources_by_path,
        conversation_titles=conversation_titles,
        reported_missing_resources=reported_missing_resources,
        new_export_state=new_export_state,
        export_state_path=export_state_path,
        project_count=len(projects),
        projects=projects,
    )


def _run_obsidian_export(
    backup_root: Path,
    *,
    convert_png: bool,
    convert_wav: bool,
    preserve_transparent_png: bool,
    convert_m4a: bool,
    include_conversation_audio: bool,
    include_user_attachments: bool,
    selected_project_ids: set[str | None] | None,
    include_project_tag: bool,
    custom_roles: bool,
    user_role: str,
    assistant_role: str,
    progress: Callable[[dict[str, Any]], None] | None,
    logger: logging.Logger,
    state: dict[str, Any],
) -> dict[str, Any]:
    backup_root = backup_root.resolve()

    started_at = monotonic()
    last_percent = 0.0
    state.clear()
    state.update({
        "project": None,
        "current_item": None,
        "processed_resources": 0,
        "total_resources": 0,
        "copied_resources": 0,
        "converted_resources": 0,
        "reused_resources": 0,
        "missing_resources": 0,
        "markdown_files": 0,
        "warnings": 0,
        "errors": 0,
        "broken_links": 0,
        "last_error": None,
    })

    def emit(
        stage: str,
        phase_label: str,
        current: int,
        total: int,
        start_percent: float,
        end_percent: float,
        *,
        completed: bool = False,
        progress_ratio: float | None = None,
    ) -> None:
        nonlocal last_percent
        if completed:
            percent = 100.0
        elif progress_ratio is not None:
            percent = start_percent + (end_percent - start_percent) * min(1.0, max(0.0, progress_ratio))
        elif total:
            percent = start_percent + (end_percent - start_percent) * min(1.0, current / total)
        else:
            percent = end_percent
        percent = min(100.0, max(last_percent, percent))
        last_percent = percent
        if progress is not None:
            progress({
                "stage": stage,
                "phase": stage,
                "phase_label": phase_label,
                "project": state["project"],
                "current_item": state["current_item"],
                "processed_resources": state["processed_resources"],
                "total_resources": state["total_resources"],
                "converted_resources": state["converted_resources"],
                "reused_resources": state["reused_resources"],
                "missing_resources": state["missing_resources"],
                "markdown_files": state["markdown_files"],
                "warnings": state["warnings"],
                "errors": state["errors"],
                "last_error": state["last_error"],
                "percent": round(percent, 2),
                "current": current,
                "total": total,
                "total_elapsed": monotonic() - started_at,
            })

    output_root = backup_root / "export" / "obsidian"
    prepared = _prepare_export(
        backup_root,
        output_root=output_root,
        convert_png=convert_png,
        convert_wav=convert_wav,
        preserve_transparent_png=preserve_transparent_png,
        convert_m4a=convert_m4a,
        include_conversation_audio=include_conversation_audio,
        include_user_attachments=include_user_attachments,
        selected_project_ids=selected_project_ids,
        logger=logger,
        state=state,
        emit=emit,
    )
    conversations = prepared.conversations
    loaded_conversations = prepared.loaded_conversations
    resource_map = prepared.resource_map
    exported_to_source = prepared.exported_to_source
    resources_by_path = prepared.resources_by_path
    conversation_titles = prepared.conversation_titles
    reported_missing_resources = prepared.reported_missing_resources
    new_export_state = prepared.new_export_state
    export_state_path = prepared.export_state_path
    project_count = prepared.project_count

    references: set[tuple[str, str]] = set()
    markdown_total = len(conversations)
    state["project"] = None
    state["current_item"] = None
    emit("obsidian", "Generar Obsidian", 0, markdown_total, 65, 90)
    for current, entry in enumerate(conversations, start=1):
        if not isinstance(entry, dict):
            raise ExportError("El índice contiene una conversación inválida.")
        project_slug = str(entry.get("project_slug") or "unassigned")
        title = str(entry.get("title") or entry.get("conversation_id") or "Conversación")
        state["project"] = project_slug
        state["current_item"] = title
        logger.info(
            "MARKDOWN_START project=%s conversation=%s current=%s total=%s",
            project_slug,
            title,
            current,
            markdown_total,
        )
        emit("obsidian", "Generar Obsidian", current - 1, markdown_total, 65, 90)
        try:
            conversation = loaded_conversations.get(current - 1)
            if conversation is None:
                source_path = prepared.conversation_paths.get(current - 1)
                if source_path is None:
                    raise ExportError("No existe la conversación del paquete.")
                conversation = _read_json(source_path, title)
            if not include_conversation_audio:
                _exclude_dictation_audio_resources(conversation)
            if not include_user_attachments:
                _exclude_user_attachment_resources(conversation)
            _, conversation_references, conversation_missing_resources = export_conversation(
                conversation,
                project_slug,
                output_root,
                resource_map,
                include_project_tag=include_project_tag,
                custom_roles=custom_roles,
                user_role=user_role,
                assistant_role=assistant_role,
            )
        except ExportError as error:
            state["errors"] += 1
            state["last_error"] = str(error)
            logger.error(
                "ERROR markdown_generation project=%s conversation=%s conversation_id=%s reason=%s",
                project_slug,
                title,
                entry.get("conversation_id"),
                error,
            )
            raise
        except (OSError, ValueError) as error:
            state["errors"] += 1
            state["last_error"] = str(error)
            logger.error(
                "ERROR markdown_generation project=%s conversation=%s conversation_id=%s reason=%s",
                project_slug,
                title,
                entry.get("conversation_id"),
                error,
            )
            raise ExportError(f"No se pudo generar el Markdown de {title}: {error}") from error
        for position, resource in enumerate(conversation_missing_resources, start=1):
            source_key = str(resource.pop("_export_source_key", ""))
            identity = _resource_identity(resource, source_key, position)
            if identity in reported_missing_resources:
                continue
            reported_missing_resources.add(identity)
            state["missing_resources"] += 1
            state["warnings"] += 1
            source_resource = prepared.resource_sources.get(source_key)
            _log_resource_error(
                logger,
                "missing_export_reference",
                resource,
                conversation_titles,
                source_resource,
                _expected_export_path(
                    source_key,
                    convert_png,
                    convert_wav,
                    preserve_transparent_png,
                    convert_m4a,
                    source_resource,
                ),
                "No existe una ruta exportada porque el recurso físico no está disponible",
                warning=True,
            )
        project_directory_name = slugify(project_slug or "unassigned", limit=64)
        references.update((project_directory_name, reference) for reference in conversation_references)
        state["markdown_files"] = current
        logger.info("MARKDOWN_CREATED project=%s conversation=%s", project_slug, title)
        emit("obsidian", "Generar Obsidian", current, markdown_total, 65, 90)

    validation_errors: list[str] = []
    reference_list = sorted(references)
    state["project"] = None
    state["current_item"] = None
    emit("validate", "Validar y finalizar", 0, len(reference_list), 90, 99)
    for current, (project_directory_name, relative) in enumerate(reference_list, start=1):
        exported_path = f"{project_directory_name}/{relative}"
        state["current_item"] = exported_path
        if not (output_root / project_directory_name / Path(relative)).is_file():
            validation_errors.append(exported_path)
            state["errors"] = len(validation_errors)
            state["last_error"] = f"Recurso exportado ausente: {exported_path}"
            source_key = exported_to_source.get((project_directory_name, relative), "")
            source_resource = prepared.resource_sources.get(source_key)
            for resource in resources_by_path.get(source_key) or [None]:
                _log_resource_error(
                    logger,
                    "validation",
                    resource,
                    conversation_titles,
                    source_resource,
                    exported_path,
                    "Recurso exportado ausente",
                )
            logger.error(
                "ERROR validation exported_path=%s expected_absolute_path=%s reason=Recurso exportado ausente",
                exported_path,
                output_root / project_directory_name / Path(relative),
            )
        emit("validate", "Validar y finalizar", current, len(reference_list), 90, 99)
    if validation_errors:
        state["broken_links"] = len(validation_errors)
        raise ExportError(
            f"La validación encontró {len(validation_errors)} referencia(s) sin recurso exportado."
        )

    _write_export_state(
        export_state_path,
        {
            "convert_png": convert_png,
            "convert_wav": convert_wav,
            "preserve_transparent_png": preserve_transparent_png,
            "convert_m4a": convert_m4a,
            "include_conversation_audio": include_conversation_audio,
            "include_user_attachments": include_user_attachments,
            "include_project_tag": include_project_tag,
            "custom_roles": custom_roles,
            "user_role": user_role,
            "assistant_role": assistant_role,
        },
        new_export_state,
    )

    state["current_item"] = "Exportación Obsidian completada"
    emit("validate", "Validar y finalizar", 1, 1, 99, 100, completed=True)
    return {
        "status": "success_with_warnings" if state["warnings"] else "success",
        "format": "obsidian",
        "output_root": str(output_root),
        "project_count": project_count,
        "conversation_count": len(conversations),
        "resource_count": len(resource_map),
        "converted_resource_count": state["converted_resources"],
        "markdown_count": state["markdown_files"],
        "validation_error_count": 0,
        "processed_resources": state["processed_resources"],
        "copied_resources": state["copied_resources"],
        "converted_resources": state["converted_resources"],
        "reused_resources": state["reused_resources"],
        "missing_resources": state["missing_resources"],
        "markdown_files": state["markdown_files"],
        "warnings": state["warnings"],
        "errors": state["errors"],
        "broken_links": state["broken_links"],
    }


def _run_html_export(
    backup_root: Path,
    *,
    convert_png: bool,
    convert_wav: bool,
    preserve_transparent_png: bool,
    convert_m4a: bool,
    include_conversation_audio: bool,
    include_user_attachments: bool,
    selected_project_ids: set[str | None] | None,
    include_project_tag: bool,
    custom_roles: bool,
    user_role: str,
    assistant_role: str,
    progress: Callable[[dict[str, Any]], None] | None,
    logger: logging.Logger,
    state: dict[str, Any],
) -> dict[str, Any]:
    backup_root = backup_root.resolve()
    started_at = monotonic()
    last_percent = 0.0
    state.clear()
    state.update({
        "project": None,
        "current_item": None,
        "processed_resources": 0,
        "total_resources": 0,
        "copied_resources": 0,
        "converted_resources": 0,
        "reused_resources": 0,
        "missing_resources": 0,
        "html_files": 0,
        "warnings": 0,
        "errors": 0,
        "broken_links": 0,
        "last_error": None,
    })

    def emit(
        stage: str,
        phase_label: str,
        current: int,
        total: int,
        start_percent: float,
        end_percent: float,
        *,
        completed: bool = False,
        progress_ratio: float | None = None,
    ) -> None:
        nonlocal last_percent
        if completed:
            percent = 100.0
        elif progress_ratio is not None:
            percent = start_percent + (end_percent - start_percent) * min(1.0, max(0.0, progress_ratio))
        elif total:
            percent = start_percent + (end_percent - start_percent) * min(1.0, current / total)
        else:
            percent = end_percent
        percent = min(100.0, max(last_percent, percent))
        last_percent = percent
        if progress is not None:
            progress({
                "stage": stage,
                "phase": stage,
                "phase_label": phase_label,
                "project": state["project"],
                "current_item": state["current_item"],
                "processed_resources": state["processed_resources"],
                "total_resources": state["total_resources"],
                "converted_resources": state["converted_resources"],
                "reused_resources": state["reused_resources"],
                "missing_resources": state["missing_resources"],
                "html_files": state["html_files"],
                "warnings": state["warnings"],
                "errors": state["errors"],
                "last_error": state["last_error"],
                "percent": round(percent, 2),
                "current": current,
                "total": total,
                "total_elapsed": monotonic() - started_at,
            })

    output_root = backup_root / "export" / "html"
    prepared = _prepare_export(
        backup_root,
        output_root=output_root,
        convert_png=convert_png,
        convert_wav=convert_wav,
        preserve_transparent_png=preserve_transparent_png,
        convert_m4a=convert_m4a,
        include_conversation_audio=include_conversation_audio,
        include_user_attachments=include_user_attachments,
        selected_project_ids=selected_project_ids,
        logger=logger,
        state=state,
        emit=emit,
    )
    conversations = prepared.conversations
    conversation_data: list[dict[str, Any]] = []
    for position, entry in enumerate(conversations):
        if not isinstance(entry, dict):
            raise ExportError("El índice contiene una conversación inválida.")
        title = str(entry.get("title") or entry.get("conversation_id") or "Conversación")
        conversation = prepared.loaded_conversations.get(position)
        if conversation is None:
            source_path = prepared.conversation_paths.get(position)
            if source_path is None:
                raise ExportError("No existe la conversación del paquete.")
            conversation = _read_json(source_path, title)
        conversation_data.append(conversation)

    planned_paths, navigation_by_conversation, index_navigation = _build_html_navigation(
        prepared.projects, conversations, conversation_data, output_root,
    )
    references: set[tuple[str, str]] = set()
    html_total = len(conversations)
    state["project"] = None
    state["current_item"] = None
    emit("html", "Generar HTML", 0, html_total, 65, 90)
    for current, (entry, conversation, planned_path, navigation) in enumerate(
        zip(conversations, conversation_data, planned_paths, navigation_by_conversation), start=1
    ):
        project_slug = str(entry.get("project_slug") or "unassigned")
        title = str(entry.get("title") or entry.get("conversation_id") or "Conversación")
        state["project"] = project_slug
        state["current_item"] = title
        logger.info(
            "HTML_START project=%s conversation=%s current=%s total=%s",
            project_slug,
            title,
            current,
            html_total,
        )
        emit("html", "Generar HTML", current - 1, html_total, 65, 90)
        try:
            if not include_conversation_audio:
                _exclude_dictation_audio_resources(conversation)
            if not include_user_attachments:
                _exclude_user_attachment_resources(conversation)
            output_path, conversation_references, conversation_missing_resources = export_html_conversation(
                conversation,
                project_slug,
                output_root,
                prepared.resource_map,
                navigation,
                custom_roles=custom_roles,
                user_role=user_role,
                assistant_role=assistant_role,
            )
            if output_path != planned_path:
                raise ExportError(f"La ruta HTML generada no coincide con la navegación: {output_path}")
        except ExportError as error:
            state["errors"] += 1
            state["last_error"] = str(error)
            logger.error(
                "ERROR html_generation project=%s conversation=%s conversation_id=%s reason=%s",
                project_slug, title, entry.get("conversation_id"), error,
            )
            raise
        except (OSError, ValueError) as error:
            state["errors"] += 1
            state["last_error"] = str(error)
            logger.error(
                "ERROR html_generation project=%s conversation=%s conversation_id=%s reason=%s",
                project_slug, title, entry.get("conversation_id"), error,
            )
            raise ExportError(f"No se pudo generar el HTML de {title}: {error}") from error
        for position, resource in enumerate(conversation_missing_resources, start=1):
            source_key = str(resource.pop("_export_source_key", ""))
            identity = _resource_identity(resource, source_key, position)
            if identity in prepared.reported_missing_resources:
                continue
            prepared.reported_missing_resources.add(identity)
            state["missing_resources"] += 1
            state["warnings"] += 1
            source_resource = prepared.resource_sources.get(source_key)
            _log_resource_error(
                logger,
                "missing_export_reference",
                resource,
                prepared.conversation_titles,
                source_resource,
                _expected_export_path(
                    source_key, convert_png, convert_wav, preserve_transparent_png,
                    convert_m4a, source_resource,
                ),
                "No existe una ruta exportada porque el recurso físico no está disponible",
                warning=True,
            )
        project_directory_name = slugify(project_slug or "unassigned", limit=64)
        references.update((project_directory_name, reference) for reference in conversation_references)
        state["html_files"] = current
        logger.info("HTML_CREATED project=%s conversation=%s", project_slug, title)
        emit("html", "Generar HTML", current, html_total, 65, 90)

    index_path = export_index(output_root, index_navigation)

    validation_errors: list[str] = []
    reference_list = sorted(references)
    state["project"] = None
    state["current_item"] = None
    emit("validate", "Validar y finalizar", 0, len(reference_list), 90, 99)
    for current, (project_directory_name, relative) in enumerate(reference_list, start=1):
        exported_path = f"{project_directory_name}/{relative}"
        state["current_item"] = exported_path
        if not (output_root / project_directory_name / Path(relative)).is_file():
            validation_errors.append(exported_path)
            state["errors"] = len(validation_errors)
            state["last_error"] = f"Recurso exportado ausente: {exported_path}"
            source_key = prepared.exported_to_source.get((project_directory_name, relative), "")
            source_resource = prepared.resource_sources.get(source_key)
            for resource in prepared.resources_by_path.get(source_key) or [None]:
                _log_resource_error(
                    logger, "validation", resource, prepared.conversation_titles,
                    source_resource, exported_path, "Recurso exportado ausente",
                )
            logger.error(
                "ERROR validation exported_path=%s expected_absolute_path=%s reason=Recurso exportado ausente",
                exported_path, output_root / project_directory_name / Path(relative),
            )
        emit("validate", "Validar y finalizar", current, len(reference_list), 90, 99)
    if validation_errors:
        state["broken_links"] = len(validation_errors)
        raise ExportError(
            f"La validación encontró {len(validation_errors)} referencia(s) sin recurso exportado."
        )

    _write_export_state(
        prepared.export_state_path,
        {
            "convert_png": convert_png,
            "convert_wav": convert_wav,
            "preserve_transparent_png": preserve_transparent_png,
            "convert_m4a": convert_m4a,
            "include_conversation_audio": include_conversation_audio,
            "include_user_attachments": include_user_attachments,
            "include_project_tag": include_project_tag,
            "custom_roles": custom_roles,
            "user_role": user_role,
            "assistant_role": assistant_role,
        },
        prepared.new_export_state,
    )
    state["current_item"] = "Exportación HTML completada"
    emit("validate", "Validar y finalizar", 1, 1, 99, 100, completed=True)
    return {
        "status": "success_with_warnings" if state["warnings"] else "success",
        "format": "html",
        "output_root": str(output_root),
        "index_path": str(index_path),
        "project_count": prepared.project_count,
        "conversation_count": len(conversations),
        "resource_count": len(prepared.resource_map),
        "converted_resource_count": state["converted_resources"],
        "html_files": state["html_files"],
        "validation_error_count": 0,
        "processed_resources": state["processed_resources"],
        "copied_resources": state["copied_resources"],
        "converted_resources": state["converted_resources"],
        "reused_resources": state["reused_resources"],
        "missing_resources": state["missing_resources"],
        "warnings": state["warnings"],
        "errors": state["errors"],
        "broken_links": state["broken_links"],
    }


def _build_html_navigation(
    projects: list[Any],
    entries: list[Any],
    conversations: list[dict[str, Any]],
    output_root: Path,
) -> tuple[list[Path], list[list[dict[str, Any]]], list[dict[str, Any]]]:
    groups: dict[str, dict[str, Any]] = {}
    for project in projects:
        if not isinstance(project, dict):
            continue
        slug = str(project.get("slug") or "unassigned")
        groups.setdefault(slug, {
            "name": str(project.get("name") or slug),
            "conversations": [],
        })

    occupied: dict[Path, dict[str, list[Path]]] = {}
    reserved: dict[Path, dict[str, str]] = {}
    planned_paths: list[Path] = []
    for entry, conversation in zip(entries, conversations):
        slug = str(entry.get("project_slug") or "unassigned")
        group = groups.setdefault(slug, {"name": slug, "conversations": []})
        project_dir = output_root / slugify(slug, limit=64)
        if project_dir not in occupied:
            existing_by_name: dict[str, list[Path]] = {}
            if project_dir.is_dir():
                for path in project_dir.iterdir():
                    existing_by_name.setdefault(path.name.casefold(), []).append(path)
            occupied[project_dir] = existing_by_name
            reserved[project_dir] = {}
        conversation_id = str(conversation.get("conversation_id") or "conversation")
        title = str(conversation.get("title") or "Sin título").strip() or "Sin título"
        clean_name = Path(build_conversation_filename(conversation_id, title)).with_suffix(".html").name
        clean_path = Path(clean_name)
        number = 1
        while True:
            filename = clean_name if number == 1 else f"{clean_path.stem}__{number}{clean_path.suffix}"
            key = filename.casefold()
            matches = occupied[project_dir].get(key, [])
            matching_existing = next((
                path for path in matches
                if path.is_file() and not path.is_symlink() and _html_conversation_id(path) == conversation_id
            ), None)
            if matching_existing is not None:
                destination = matching_existing
                break
            if not matches and reserved[project_dir].get(key) in (None, conversation_id):
                destination = project_dir / filename
                reserved[project_dir][key] = conversation_id
                break
            number = 2 if number == 1 else number + 1
        planned_paths.append(destination)
        group["conversations"].append({"title": title, "target": destination})

    def navigation_from(start: Path) -> list[dict[str, Any]]:
        navigation: list[dict[str, Any]] = []
        for group in groups.values():
            navigation.append({
                "name": group["name"],
                "conversations": [
                    {
                        "title": item["title"],
                        "path": Path(os.path.relpath(item["target"], start=start)).as_posix(),
                    }
                    for item in group["conversations"]
                ],
            })
        return navigation

    navigation_by_conversation = [navigation_from(source.parent) for source in planned_paths]
    return planned_paths, navigation_by_conversation, navigation_from(output_root)


def _create_export_logger(
    logs_root: Path, format_name: str = "obsidian"
) -> tuple[logging.Logger, logging.FileHandler, Path]:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = logs_root / f"{format_name}_{timestamp}.log"
    duplicate = 2
    while log_path.exists():
        log_path = logs_root / f"{format_name}_{timestamp}_{duplicate}.log"
        duplicate += 1

    logger = logging.getLogger(f"oai_processor.export.{uuid4().hex}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
    return logger, handler, log_path


def _load_export_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExportError(f"No se pudo leer export_state.json: {error}") from error
    resources = data.get("resources") if isinstance(data, dict) else None
    if not isinstance(resources, dict):
        raise ExportError("export_state.json no tiene la estructura esperada.")
    return {
        source_key: entry
        for source_key, entry in resources.items()
        if isinstance(source_key, str) and isinstance(entry, dict)
    }


def _remove_recorded_export(output_root: Path, exported_path: Any) -> None:
    if not isinstance(exported_path, str):
        return
    relative = PurePosixPath(exported_path)
    legacy_resource_path = bool(relative.parts) and relative.parts[0] == "resources"
    project_resource_path = len(relative.parts) >= 3 and relative.parts[1] == "resources"
    if relative.is_absolute() or ".." in relative.parts or not (legacy_resource_path or project_resource_path):
        return
    candidate = output_root.joinpath(*relative.parts)
    parent = candidate.parent
    while parent != output_root and output_root in parent.parents:
        if parent.is_symlink():
            return
        parent = parent.parent
    if candidate.is_file() or candidate.is_symlink():
        candidate.unlink()


def _write_export_state(
    path: Path,
    options: dict[str, bool],
    resources: dict[str, dict[str, Any]],
) -> None:
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps({"options": options, "resources": resources}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _log_resource_error(
    logger: logging.Logger,
    error_type: str,
    resource: dict[str, Any] | None,
    conversation_titles: dict[str, str],
    source_path: Path | None,
    expected_export_path: str | None,
    reason: str,
    *,
    warning: bool = False,
) -> None:
    details = resource or {}
    conversation_id = details.get("conversation_id")
    project = details.get("project_name") or details.get("project_slug")
    if not project and isinstance(details.get("project"), dict):
        project_data = details["project"]
        project = project_data.get("project_name") or project_data.get("slug")
    log = logger.warning if warning else logger.error
    label = "WARNING" if warning else "ERROR"
    log(
        "%s %s\n"
        "resource_id=%s\n"
        "asset_id=%s\n"
        "file_id=%s\n"
        "message_id=%s\n"
        "conversation_id=%s\n"
        "conversation=%s\n"
        "project=%s\n"
        "original_name=%s\n"
        "source_path=%s\n"
        "indexed_source_path=%s\n"
        "expected_export_path=%s\n"
        "resource_type=%s\n"
        "mime=%s\n"
        "reason=%s",
        label,
        error_type,
        details.get("resource_id"),
        details.get("openai_asset_id") or details.get("asset_id"),
        details.get("openai_file_id") or details.get("file_id"),
        details.get("message_id"),
        conversation_id,
        conversation_titles.get(str(conversation_id), "") if conversation_id else "",
        project,
        details.get("original_name"),
        source_path,
        details.get("source_path"),
        expected_export_path,
        details.get("kind"),
        details.get("mime_type"),
        reason,
    )


def _expected_export_path(
    source_key: str,
    convert_png: bool,
    convert_wav: bool,
    preserve_transparent_png: bool,
    convert_m4a: bool,
    source_path: Path | None = None,
) -> str | None:
    if not source_key:
        return None
    relative = Path(source_key)
    if convert_png and relative.suffix.lower() == ".png":
        has_transparency = (
            preserve_transparent_png
            and source_path is not None
            and source_path.is_file()
            and _png_has_transparency(source_path)
        )
        if not has_transparency:
            relative = relative.with_suffix(".jpg")
    elif convert_wav and relative.suffix.lower() == ".wav":
        relative = relative.with_suffix(".mp3")
    elif convert_m4a and relative.suffix.lower() == ".m4a":
        relative = relative.with_suffix(".mp3")
    resource_relative = Path(*relative.parts[1:]) if len(relative.parts) > 1 else relative
    return f"resources/{resource_relative.as_posix()}"


def _is_dictation_audio_resource(resource: Any) -> bool:
    if not isinstance(resource, dict):
        return False
    metadata = resource.get("metadata")
    return (
        isinstance(metadata, dict)
        and metadata.get("_resource_source") == "message.metadata.dictation_asset_pointer"
    )


def _is_user_attachment_resource(resource: Any) -> bool:
    if not isinstance(resource, dict):
        return False
    metadata = resource.get("metadata")
    return (
        isinstance(metadata, dict)
        and metadata.get("_resource_source") == "message.metadata.attachments"
    )


def _resource_source_key(resource: dict[str, Any]) -> str:
    relative = resource.get("relative_resource_path")
    if isinstance(relative, str) and relative.strip():
        return Path(relative).as_posix()
    raw = resource.get("raw_relative_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).as_posix().removeprefix("resources/")
    return ""


def _namespace_resource_paths(resource: dict[str, Any], project_id: str) -> None:
    """Keep package-local resource paths distinct in the shared export maps."""
    relative = resource.get("relative_resource_path")
    if isinstance(relative, str) and relative.strip():
        resource["relative_resource_path"] = _package_resource_key(project_id, relative)
    raw = resource.get("raw_relative_path")
    if isinstance(raw, str) and raw.strip():
        raw_relative = Path(raw).as_posix().removeprefix("resources/")
        resource["raw_relative_path"] = f"resources/{_package_resource_key(project_id, raw_relative)}"


def _namespace_conversation_resources(conversation: dict[str, Any], project_id: str) -> None:
    messages = conversation.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        resources = message.get("resources")
        if not isinstance(resources, list):
            continue
        for resource in resources:
            if isinstance(resource, dict):
                _namespace_resource_paths(resource, project_id)


def _package_resource_key(project_id: str, relative_path: str) -> str:
    return (Path(project_id) / Path(relative_path)).as_posix()


def _resource_association_key(
    resource: dict[str, Any], project_id: str
) -> tuple[str, str, str, str]:
    return (
        project_id,
        str(resource.get("conversation_id") or ""),
        str(resource.get("message_id") or ""),
        str(resource.get("resource_id") or ""),
    )


def _exclude_dictation_audio_resources(conversation: dict[str, Any]) -> None:
    messages = conversation.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        resources = message.get("resources")
        if isinstance(resources, list):
            message["resources"] = [
                resource for resource in resources
                if not _is_dictation_audio_resource(resource)
            ]


def _exclude_user_attachment_resources(conversation: dict[str, Any]) -> None:
    messages = conversation.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        resources = message.get("resources")
        if isinstance(resources, list):
            message["resources"] = [
                resource for resource in resources
                if not _is_user_attachment_resource(resource)
            ]


def _resource_identity(resource: dict[str, Any], source_key: str, position: int) -> str:
    resource_key = str(
        resource.get("resource_id")
        or resource.get("openai_asset_id")
        or resource.get("openai_file_id")
        or source_key
        or f"resource-{position}"
    )
    return "|".join((
        resource_key,
        str(resource.get("conversation_id") or ""),
        str(resource.get("message_id") or ""),
    ))


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExportError(f"No se pudo leer {label}: {error}") from error
    if not isinstance(payload, dict):
        raise ExportError(f"{label} no contiene un objeto JSON válido.")
    return payload


def _safe_process_path(process_root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise ExportError("Una conversación no tiene ruta dentro de process.")
    candidate = (process_root / Path(relative)).resolve()
    if not candidate.is_relative_to(process_root.resolve()) or not candidate.is_file():
        raise ExportError(f"No existe la conversación procesada: {relative}")
    return candidate


def _convert_png_to_jpg(source: Path, destination: Path) -> None:
    from PIL import Image

    with Image.open(source) as image:
        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            converted = Image.new("RGB", rgba.size, "white")
            converted.paste(rgba, mask=rgba.getchannel("A"))
        else:
            converted = image.convert("RGB")
        converted.save(destination, format="JPEG", quality=95, optimize=True)


def _png_has_transparency(source: Path) -> bool:
    from PIL import Image

    with Image.open(source) as image:
        return image.convert("RGBA").getchannel("A").getextrema()[0] < 255


def _convert_audio_to_mp3(source: Path, destination: Path, ffmpeg_path: str) -> None:
    result = subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(destination),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"FFmpeg no pudo convertir el archivo {source.suffix.upper()}.")
