from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
import shutil
import ssl
from time import monotonic, sleep
from typing import Any, Callable
import urllib.error
import urllib.request
from uuid import uuid4

from core.ids import slugify


_MANIFEST_DIRECTORY = ".openai_obsidian_processor"
_MANIFEST_NAME = "manifest.json"
_BACKUP_NAME = "manifest.backup.json"
_OBSIDIAN_API_URL = "https://127.0.0.1:27124/"
_OBSIDIAN_CONNECTION_TIMEOUT_SECONDS = 5
_OBSIDIAN_ACTIVITY_TIMEOUT_SECONDS = 90
_OBSIDIAN_STABILITY_SECONDS = 20
_OBSIDIAN_TOTAL_WAIT_SECONDS = 1800


def analyze_vault_export(
    root: Path,
    vault_path: Path,
    selected_project_ids: set[str | None] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, object]:
    """Classify files in the generated Obsidian export against a vault manifest."""
    source_root, vault, manifest_path, _ = _resolve_paths(root, vault_path)
    previous = _load_manifest(manifest_path)
    current_resource_source_keys = _load_export_resource_source_keys(source_root / "export_state.json")
    previous_resource_paths: dict[str, list[dict[str, str]]] = {}
    for source_path, entry in previous.items():
        resource_source_key = entry.get("resource_source_key")
        if isinstance(resource_source_key, str) and resource_source_key.strip():
            previous_resource_paths.setdefault(resource_source_key, []).append({
                "source_path": source_path,
                "destination_path": entry["destination_path"],
            })

    resource_path_changes: list[dict[str, str]] = []
    resource_path_conflicts: list[dict[str, object]] = []
    for resource_source_key, previous_entries in sorted(previous_resource_paths.items()):
        if len(previous_entries) > 1:
            resource_path_conflicts.append({
                "resource_source_key": resource_source_key,
                "previous_entries": sorted(previous_entries, key=lambda item: item["source_path"]),
            })
            continue
        new_source_path = next(
            (
                exported_path
                for exported_path, current_source_key in current_resource_source_keys.items()
                if current_source_key == resource_source_key
            ),
            None,
        )
        previous_entry = previous_entries[0]
        old_source_path = previous_entry["source_path"]
        if new_source_path is not None and old_source_path != new_source_path:
            resource_path_changes.append({
                "resource_source_key": resource_source_key,
                "old_source_path": old_source_path,
                "old_destination_path": previous_entry["destination_path"],
                "new_source_path": new_source_path,
            })

    valid_resource_changes: list[dict[str, Any]] = []
    for change in resource_path_changes:
        old_reference = _resource_markdown_reference(change["old_source_path"])
        new_reference = _resource_markdown_reference(change["new_source_path"])
        if not old_reference or not new_reference or old_reference == new_reference:
            continue
        valid_resource_changes.append({
            "change": change,
            "old_reference": old_reference,
            "new_reference": new_reference,
            "entries": [],
        })
    changes_by_reference: dict[str, list[dict[str, Any]]] = {}
    for prepared_change in valid_resource_changes:
        changes_by_reference.setdefault(prepared_change["old_reference"], []).append(prepared_change)
    obsidian_target_trie = _build_obsidian_target_trie(changes_by_reference)

    previous_markdown: dict[str, list[dict[str, str]]] = {}
    for source_path, entry in previous.items():
        conversation_id = entry.get("conversation_id")
        if (
            PurePosixPath(source_path).suffix.lower() == ".md"
            and isinstance(conversation_id, str)
            and conversation_id.strip()
        ):
            previous_markdown.setdefault(conversation_id, []).append({
                "source_path": source_path,
                "destination_path": entry["destination_path"],
            })

    markdown_files: list[Path] = []
    vault_discovery_count = 0
    for markdown_path in vault.rglob("*"):
        vault_discovery_count += 1
        if progress is not None:
            _report_discovery_progress(
                progress,
                current=vault_discovery_count,
                current_item=markdown_path.relative_to(vault).as_posix(),
            )
        if (
            not markdown_path.is_file()
            or markdown_path.is_symlink()
            or markdown_path.suffix.lower() != ".md"
        ):
            continue
        try:
            markdown_path.resolve().relative_to(vault)
        except (OSError, ValueError):
            continue
        markdown_files.append(markdown_path)

    if progress is not None:
        _report_discovery_progress(
            progress,
            current=vault_discovery_count,
            current_item="",
            complete=True,
        )

    source_discovery_count = 0

    def report_source_discovery(values: dict[str, Any]) -> None:
        nonlocal source_discovery_count
        source_discovery_count = int(values.get("current", 0))
        _report_discovery_progress(
            progress,
            current=vault_discovery_count + source_discovery_count,
            current_item=str(values.get("current_item", "")),
            complete=values.get("discovery_complete") is True,
        )

    plan_sources = _plan_source_files(
        source_root,
        allowed_project_slugs=_selected_project_slugs(root, selected_project_ids),
        progress=report_source_discovery if progress is not None else None,
    )
    discovery_total = vault_discovery_count + source_discovery_count
    link_markdown_capacity = len(markdown_files) if valid_resource_changes else 0
    total_work = len(markdown_files) + link_markdown_capacity + len(plan_sources)
    current_markdown: dict[str, list[str]] = {}
    for current, markdown_path in enumerate(markdown_files, start=1):
        conversation_id = _read_markdown_conversation_id(markdown_path)
        if conversation_id in previous_markdown:
            current_markdown.setdefault(conversation_id, []).append(
                markdown_path.relative_to(vault).as_posix()
            )
        _report_analysis_progress(
            progress,
            current=discovery_total + current,
            total=discovery_total + total_work,
            current_item=markdown_path.relative_to(vault).as_posix(),
        )

    managed_markdown_locations: list[dict[str, str]] = []
    managed_markdown_conflicts: list[dict[str, object]] = []
    for conversation_id in sorted(set(previous_markdown) | set(current_markdown)):
        previous_entries = sorted(
            previous_markdown.get(conversation_id, []),
            key=lambda item: item["source_path"],
        )
        current_paths = sorted(current_markdown.get(conversation_id, []))
        if len(previous_entries) > 1 or len(current_paths) > 1:
            managed_markdown_conflicts.append({
                "conversation_id": conversation_id,
                "previous_entries": previous_entries,
                "current_paths": current_paths,
            })
            continue
        if len(previous_entries) == 1 and len(current_paths) == 1:
            previous_entry = previous_entries[0]
            managed_markdown_locations.append({
                "conversation_id": conversation_id,
                "manifest_source_path": previous_entry["source_path"],
                "previous_destination_path": previous_entry["destination_path"],
                "current_destination_path": current_paths[0],
            })

    link_markdown_count = len(managed_markdown_locations) if valid_resource_changes else 0
    total_work = len(markdown_files) + link_markdown_count + len(plan_sources)
    if progress is not None and link_markdown_count < link_markdown_capacity:
        _report_analysis_progress(
            progress,
            current=discovery_total + len(markdown_files),
            total=discovery_total + total_work,
            current_item="",
        )
    if valid_resource_changes:
        for current, location in enumerate(managed_markdown_locations, start=1):
            markdown_relative = location["current_destination_path"]
            markdown_path = vault.joinpath(*PurePosixPath(markdown_relative).parts)
            try:
                markdown_content = markdown_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                markdown_content = None

            if markdown_content is not None:
                target_counts = _count_obsidian_targets(markdown_content, obsidian_target_trie)
                for old_reference, occurrences in target_counts.items():
                    for prepared_change in changes_by_reference[old_reference]:
                        change = prepared_change["change"]
                        prepared_change["entries"].append({
                            "conversation_id": location["conversation_id"],
                            "markdown_path": markdown_relative,
                            "resource_source_key": change["resource_source_key"],
                            "old_reference": old_reference,
                            "new_reference": prepared_change["new_reference"],
                            "occurrences": occurrences,
                        })

            _report_analysis_progress(
                progress,
                current=discovery_total + len(markdown_files) + current,
                total=discovery_total + total_work,
                current_item=markdown_relative,
            )

    markdown_link_update_plan = [
        entry
        for prepared_change in valid_resource_changes
        for entry in prepared_change["entries"]
    ]

    files = _plan_files(
        source_root,
        vault,
        previous,
        progress=progress,
        progress_offset=discovery_total + len(markdown_files) + link_markdown_count,
        progress_total=discovery_total + total_work,
        source_files=plan_sources,
    )
    counts = {status: sum(1 for item in files if item["status"] == status) for status in (
        "new", "unchanged", "modified", "conflict", "unmanaged",
    )}
    return {
        "source_root": str(source_root),
        "vault_path": str(vault),
        "manifest_path": str(manifest_path),
        "files": files,
        "counts": counts,
        "resource_path_changes": resource_path_changes,
        "resource_path_conflicts": resource_path_conflicts,
        "managed_markdown_locations": managed_markdown_locations,
        "managed_markdown_conflicts": managed_markdown_conflicts,
        "markdown_link_update_plan": markdown_link_update_plan,
        "markdown_link_update_count": sum(
            int(item["occurrences"]) for item in markdown_link_update_plan
        ),
    }


def update_managed_markdown_links(
    root: Path,
    vault_path: Path,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, object]:
    """Apply only the exact managed-link changes reported by the analysis plan."""
    analysis_current = 0
    analysis_total = 0

    def report_analysis_progress(values: dict[str, Any]) -> None:
        nonlocal analysis_current, analysis_total
        if values.get("discovery") is True:
            _emit_vault_progress(
                progress,
                int(values.get("current", 0)),
                0,
                str(values.get("current_item", "")),
                discovery=True,
                discovery_complete=values.get("discovery_complete") is True,
            )
            return
        try:
            analysis_current = int(values.get("current", 0))
            analysis_total = int(values.get("total", 0))
        except (TypeError, ValueError):
            analysis_current = analysis_total = 0
        # A grouped Markdown can only come from the Markdown set included in
        # analyze_vault_export's real-work total. Reserve that many units for
        # link application so the analysis progress cannot reach 100% early.
        _emit_vault_progress(
            progress,
            analysis_current,
            analysis_total * 2,
            str(values.get("current_item", "")),
        )

    analysis = analyze_vault_export(
        root,
        vault_path,
        progress=report_analysis_progress if progress is not None else None,
    )
    raw_plan = analysis.get("markdown_link_update_plan", [])
    plan = raw_plan if isinstance(raw_plan, list) else []
    grouped: dict[str, list[dict[str, object]]] = {}
    skipped: list[dict[str, str]] = []
    for item in plan:
        if not isinstance(item, dict):
            continue
        markdown_path = item.get("markdown_path")
        if not isinstance(markdown_path, str) or not markdown_path.strip():
            skipped.append({"markdown_path": str(markdown_path or ""), "reason": "invalid_markdown_path"})
            continue
        grouped.setdefault(markdown_path, []).append(item)

    vault = Path(vault_path).expanduser().resolve()
    updated_markdown_files = 0
    updated_links = 0
    total = len(grouped)
    if not total:
        _emit_vault_progress(progress, analysis_total, analysis_total, "")
    else:
        # Continue from the analysis count; the exact second-block size is now
        # known, and is no greater than the Markdown portion reserved above.
        _emit_vault_progress(progress, analysis_total, analysis_total + total, "")
    for current, (markdown_relative, updates) in enumerate(grouped.items(), start=1):
        try:
            relative = _safe_relative_path(markdown_relative)
        except ValueError:
            skipped.append({"markdown_path": markdown_relative, "reason": "unsafe_markdown_path"})
            _emit_vault_progress(progress, analysis_total + current, analysis_total + total, markdown_relative)
            continue

        markdown_path = vault.joinpath(*relative.parts)
        if relative.suffix.lower() != ".md" or markdown_path.is_symlink() or not markdown_path.is_file():
            skipped.append({"markdown_path": markdown_relative, "reason": "not_a_regular_markdown_file"})
            _emit_vault_progress(progress, analysis_total + current, analysis_total + total, markdown_relative)
            continue
        try:
            _ensure_safe_parent(vault, markdown_path.parent)
            markdown_path.resolve(strict=True).relative_to(vault)
        except (OSError, ValueError):
            skipped.append({"markdown_path": markdown_relative, "reason": "unsafe_markdown_path"})
            _emit_vault_progress(progress, analysis_total + current, analysis_total + total, markdown_relative)
            continue

        expected_ids = {item.get("conversation_id") for item in updates}
        actual_id = _read_markdown_conversation_id(markdown_path)
        if len(expected_ids) != 1 or actual_id not in expected_ids:
            skipped.append({"markdown_path": markdown_relative, "reason": "conversation_id_mismatch"})
            _emit_vault_progress(progress, analysis_total + current, analysis_total + total, markdown_relative)
            continue

        try:
            original_bytes = markdown_path.read_bytes()
            markdown = original_bytes.decode("utf-8")
        except (OSError, UnicodeError):
            skipped.append({"markdown_path": markdown_relative, "reason": "read_error"})
            _emit_vault_progress(progress, analysis_total + current, analysis_total + total, markdown_relative)
            continue

        updated_markdown = markdown
        file_link_count = 0
        occurrence_mismatch = False
        for item in updates:
            old_reference = item.get("old_reference")
            new_reference = item.get("new_reference")
            expected_occurrences = item.get("occurrences")
            if (
                not isinstance(old_reference, str)
                or not isinstance(new_reference, str)
                or not isinstance(expected_occurrences, int)
            ):
                occurrence_mismatch = True
                break
            updated_markdown, replaced = _replace_obsidian_target(
                updated_markdown,
                old_reference,
                new_reference,
            )
            if replaced != expected_occurrences:
                occurrence_mismatch = True
                break
            file_link_count += replaced

        if occurrence_mismatch:
            skipped.append({"markdown_path": markdown_relative, "reason": "unexpected_occurrence_count"})
            _emit_vault_progress(progress, analysis_total + current, analysis_total + total, markdown_relative)
            continue
        if not file_link_count:
            _emit_vault_progress(progress, analysis_total + current, analysis_total + total, markdown_relative)
            continue
        if (
            markdown_path.is_symlink()
            or not markdown_path.is_file()
            or _read_markdown_conversation_id(markdown_path) != actual_id
        ):
            skipped.append({"markdown_path": markdown_relative, "reason": "conversation_id_changed"})
            _emit_vault_progress(progress, analysis_total + current, analysis_total + total, markdown_relative)
            continue

        temporary_path = markdown_path.with_name(f".{markdown_path.name}.{uuid4().hex}.tmp")
        try:
            with temporary_path.open("xb") as temporary_file:
                temporary_file.write(updated_markdown.encode("utf-8"))
            temporary_path.replace(markdown_path)
        except OSError:
            skipped.append({"markdown_path": markdown_relative, "reason": "write_error"})
            _emit_vault_progress(progress, analysis_total + current, analysis_total + total, markdown_relative)
            continue
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

        updated_markdown_files += 1
        updated_links += file_link_count
        _emit_vault_progress(progress, analysis_total + current, analysis_total + total, markdown_relative)

    return {
        "status": "success",
        "updated_markdown_files": updated_markdown_files,
        "updated_links": updated_links,
        "skipped_markdown_files": skipped,
    }


def export_to_vault(
    root: Path,
    vault_path: Path,
    *,
    overwrite_managed: bool = False,
    selected_project_ids: set[str | None] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, object]:
    """Copy generated Obsidian files into a vault without overwriting vault edits."""
    source_root, vault, manifest_path, backup_path = _resolve_paths(root, vault_path)
    previous = _load_manifest(manifest_path)
    resource_source_keys = _load_export_resource_source_keys(source_root / "export_state.json")
    source_discovery_count = 0

    def report_source_discovery(values: dict[str, Any]) -> None:
        nonlocal source_discovery_count
        source_discovery_count = int(values.get("current", 0))
        _emit_vault_progress(
            progress,
            source_discovery_count,
            0,
            str(values.get("current_item", "")),
            discovery=True,
            discovery_complete=values.get("discovery_complete") is True,
        )

    files_to_plan = _plan_source_files(
        source_root,
        allowed_project_slugs=_selected_project_slugs(root, selected_project_ids),
        progress=report_source_discovery if progress is not None else None,
    )
    plan_total = len(files_to_plan)
    combined_total = plan_total + plan_total

    def report_plan_progress(values: dict[str, Any]) -> None:
        _emit_vault_progress(
            progress,
            source_discovery_count + int(values.get("current", 0)),
            source_discovery_count + combined_total,
            str(values.get("current_item", "")),
        )

    files = _plan_files(
        source_root,
        vault,
        previous,
        progress=report_plan_progress if progress is not None else None,
        progress_total=combined_total,
        source_files=files_to_plan,
    )
    combined_total = source_discovery_count + len(files_to_plan) + len(files)
    new_manifest = {source_path: dict(entry) for source_path, entry in previous.items()}
    copied = 0
    reserved = {str(item["destination_path"]).casefold() for item in files}

    total = combined_total
    if not total:
        _emit_vault_progress(progress, 0, 0, "")
    elif not plan_total:
        _emit_vault_progress(progress, total, total, "")
    for current, item in enumerate(files, start=1):
        source_relative = str(item["source_path"])
        destination_relative = str(item["destination_path"])
        source = source_root.joinpath(*PurePosixPath(source_relative).parts)
        destination = _vault_file(vault, destination_relative)
        status = str(item["status"])

        if status != "unchanged":
            if status == "modified" and overwrite_managed:
                destination_relative = previous[source_relative]["destination_path"]
                destination = _vault_file(vault, destination_relative)
                item["destination_path"] = destination_relative
            elif _path_occupied(destination):
                destination_relative = _unique_destination(vault, destination_relative, reserved)
                reserved.add(destination_relative.casefold())
                destination = _vault_file(vault, destination_relative)
                item["destination_path"] = destination_relative
            _ensure_safe_parent(vault, destination.parent)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied += 1

        destination_hash = _sha256(destination)
        manifest_entry = {
            "destination_path": destination_relative,
            "source_sha256": str(item["source_sha256"]),
            "destination_sha256": destination_hash,
        }
        if source.suffix.lower() == ".md":
            conversation_id = _read_markdown_conversation_id(source)
            if conversation_id:
                manifest_entry["conversation_id"] = conversation_id
        resource_source_key = resource_source_keys.get(source_relative)
        if resource_source_key:
            manifest_entry["resource_source_key"] = resource_source_key
        new_manifest[source_relative] = manifest_entry
        _emit_vault_progress(
            progress,
            source_discovery_count + plan_total + current,
            total,
            source_relative,
        )

    _write_manifest(manifest_path, backup_path, new_manifest)
    return {
        "status": "success",
        "vault_path": str(vault),
        "manifest_path": str(manifest_path),
        "backup_manifest_path": str(backup_path) if backup_path.exists() else None,
        "copied": copied,
        "unchanged": sum(1 for item in files if item["status"] == "unchanged"),
        "files": files,
    }


def export_to_vault_assisted(
    root: Path,
    vault_path: Path,
    *,
    api_key: str,
    overwrite_managed: bool = False,
    selected_project_ids: set[str | None] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, object]:
    """Copy an Obsidian export by project, waiting for indexing after each group."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("La API Key de Obsidian no puede estar vacía.")
    api_key = api_key.strip()

    source_root, vault, manifest_path, backup_path = _resolve_paths(root, vault_path)
    previous = _load_manifest(manifest_path)
    resource_source_keys = _load_export_resource_source_keys(source_root / "export_state.json")
    source_discovery_count = 0

    def report_source_discovery(values: dict[str, Any]) -> None:
        nonlocal source_discovery_count
        source_discovery_count = int(values.get("current", 0))
        _emit_vault_progress(
            progress,
            source_discovery_count,
            0,
            str(values.get("current_item", "")),
            discovery=True,
            discovery_complete=values.get("discovery_complete") is True,
        )

    files_to_plan = _plan_source_files(
        source_root,
        allowed_project_slugs=_selected_project_slugs(root, selected_project_ids),
        progress=report_source_discovery if progress is not None else None,
    )
    plan_total = len(files_to_plan)
    combined_total = plan_total + plan_total

    def report_plan_progress(values: dict[str, Any]) -> None:
        _emit_vault_progress(
            progress,
            source_discovery_count + int(values.get("current", 0)),
            source_discovery_count + combined_total,
            str(values.get("current_item", "")),
        )

    files = _plan_files(
        source_root,
        vault,
        previous,
        progress=report_plan_progress if progress is not None else None,
        progress_total=combined_total,
        source_files=files_to_plan,
    )
    combined_total = source_discovery_count + len(files_to_plan) + len(files)
    new_manifest = {source_path: dict(entry) for source_path, entry in previous.items()}
    reserved = {str(item["destination_path"]).casefold() for item in files}
    groups = _group_export_files_by_project(files)
    copied = 0
    completed_files = 0

    if not combined_total:
        _emit_vault_progress(progress, 0, 0, "")
    elif not plan_total:
        _emit_vault_progress(progress, combined_total, combined_total, "")

    for group_name, group_files in groups:
        metadata_before = _read_obsidian_metadata_cache(api_key)
        group_manifest_entries: dict[str, dict[str, str]] = {}
        group_copied = 0
        for item in group_files:
            source_relative = str(item["source_path"])
            destination_relative = str(item["destination_path"])
            source = source_root.joinpath(*PurePosixPath(source_relative).parts)
            destination = _vault_file(vault, destination_relative)
            status = str(item["status"])

            if status != "unchanged":
                if status == "modified" and overwrite_managed:
                    destination_relative = previous[source_relative]["destination_path"]
                    destination = _vault_file(vault, destination_relative)
                    item["destination_path"] = destination_relative
                elif _path_occupied(destination):
                    destination_relative = _unique_destination(vault, destination_relative, reserved)
                    reserved.add(destination_relative.casefold())
                    destination = _vault_file(vault, destination_relative)
                    item["destination_path"] = destination_relative
                _ensure_safe_parent(vault, destination.parent)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied += 1
                group_copied += 1

            manifest_entry = {
                "destination_path": destination_relative,
                "source_sha256": str(item["source_sha256"]),
                "destination_sha256": _sha256(destination),
            }
            if source.suffix.lower() == ".md":
                conversation_id = _read_markdown_conversation_id(source)
                if conversation_id:
                    manifest_entry["conversation_id"] = conversation_id
            resource_source_key = resource_source_keys.get(source_relative)
            if resource_source_key:
                manifest_entry["resource_source_key"] = resource_source_key
            group_manifest_entries[source_relative] = manifest_entry
            completed_files += 1
            _emit_vault_progress(
                progress,
                source_discovery_count + plan_total + completed_files,
                combined_total,
                source_relative,
            )

        if group_copied:
            _emit_vault_progress(
                progress,
                source_discovery_count + plan_total + completed_files,
                combined_total,
                group_name or ".",
            )
            _wait_for_obsidian_stability(
                api_key,
                metadata_before,
                monotonic(),
            )
        new_manifest.update(group_manifest_entries)
        if group_copied:
            _write_manifest(manifest_path, backup_path, new_manifest)

    return {
        "status": "success",
        "vault_path": str(vault),
        "manifest_path": str(manifest_path),
        "backup_manifest_path": str(backup_path) if backup_path.exists() else None,
        "copied": copied,
        "unchanged": sum(1 for item in files if item["status"] == "unchanged"),
        "files": files,
    }


def _group_export_files_by_project(
    files: list[dict[str, object]],
) -> list[tuple[str, list[dict[str, object]]]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for item in files:
        source_relative = PurePosixPath(str(item["source_path"]))
        group_name = source_relative.parts[0] if len(source_relative.parts) > 1 else ""
        groups.setdefault(group_name, []).append(item)
    return [
        (group_name, groups[group_name])
        for group_name in sorted(groups, key=lambda value: (value != "", value.casefold()))
    ]


def _read_obsidian_metadata_cache(api_key: str) -> dict[str, object]:
    request = urllib.request.Request(
        _OBSIDIAN_API_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="GET",
    )
    local_tls_context = ssl.create_default_context()
    local_tls_context.check_hostname = False
    local_tls_context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(
            request,
            timeout=_OBSIDIAN_CONNECTION_TIMEOUT_SECONDS,
            context=local_tls_context,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RuntimeError("La API Key de Obsidian no es válida.") from error
        raise RuntimeError(f"Local REST API respondió con el error HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise RuntimeError("Se agotó el tiempo de espera al conectar con Obsidian.") from error
        raise RuntimeError(
            "No se pudo conectar con Obsidian. Comprueba que esté abierto y que Local REST API esté activado."
        ) from error
    except TimeoutError as error:
        raise RuntimeError("Se agotó el tiempo de espera al conectar con Obsidian.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Local REST API devolvió una respuesta no válida.") from error

    if not isinstance(payload, dict) or (
        payload.get("status") != "OK"
        or payload.get("service") != "Obsidian Local REST API"
    ):
        raise RuntimeError("El servicio disponible no es Local REST API.")
    if payload.get("authenticated") is not True:
        raise RuntimeError("La API Key de Obsidian no es válida.")
    state = payload.get("state")
    metadata_cache = state.get("metadataCache") if isinstance(state, dict) else None
    if not isinstance(metadata_cache, dict):
        raise RuntimeError("Local REST API no permite acceder a state.metadataCache.")
    return metadata_cache


def _wait_for_obsidian_stability(
    api_key: str,
    metadata_before: dict[str, object],
    wait_started_at: float,
) -> None:
    group_wait_started_at = monotonic()
    last_metadata = metadata_before
    activity_observed = False
    stable_since: float | None = None
    while True:
        now = monotonic()
        if now - wait_started_at >= _OBSIDIAN_TOTAL_WAIT_SECONDS:
            raise RuntimeError("Se superó el tiempo máximo de espera de Obsidian.")
        if not activity_observed and now - group_wait_started_at >= _OBSIDIAN_ACTIVITY_TIMEOUT_SECONDS:
            raise RuntimeError("Obsidian no mostró actividad de indexación después de copiar el proyecto.")
        if activity_observed and stable_since is not None and now - stable_since >= _OBSIDIAN_STABILITY_SECONDS:
            return

        sleep(1)
        current_metadata = _read_obsidian_metadata_cache(api_key)
        now = monotonic()
        if current_metadata != last_metadata:
            activity_observed = True
            stable_since = now
            last_metadata = current_metadata


def _emit_vault_progress(
    progress: Callable[[dict[str, Any]], None] | None,
    current: int,
    total: int,
    current_item: str,
    *,
    discovery: bool = False,
    discovery_complete: bool = False,
) -> None:
    if progress is None:
        return
    values: dict[str, Any] = {
        "stage": "vault",
        "phase": "vault",
        "phase_label": "Sincronizar vault",
        "current": current,
        "total": total,
        "current_item": current_item,
    }
    if discovery:
        values["discovery"] = True
    if discovery_complete:
        values["discovery_complete"] = True
    progress(values)


def _resolve_paths(root: Path, vault_path: Path) -> tuple[Path, Path, Path, Path]:
    export_root = Path(root).expanduser().resolve() / "export"
    source_root = export_root / "obsidian"
    vault = Path(vault_path).expanduser().resolve()
    if export_root.is_symlink() or source_root.is_symlink():
        raise ValueError("La exportación de Obsidian no puede usar enlaces simbólicos.")
    if not source_root.is_dir():
        raise ValueError(f"No existe la exportación de Obsidian: {source_root}")
    if not vault.is_dir():
        raise ValueError(f"La vault de Obsidian no está disponible: {vault}")
    metadata_directory = vault / _MANIFEST_DIRECTORY
    if metadata_directory.is_symlink():
        raise ValueError("El directorio del manifiesto de la vault no puede ser un enlace simbólico.")
    manifest_path = metadata_directory / _MANIFEST_NAME
    backup_path = metadata_directory / _BACKUP_NAME
    if manifest_path.is_symlink() or backup_path.is_symlink():
        raise ValueError("Los manifiestos de la vault no pueden ser enlaces simbólicos.")
    if (manifest_path.exists() and not manifest_path.is_file()) or (backup_path.exists() and not backup_path.is_file()):
        raise ValueError("Las rutas del manifiesto deben ser archivos.")
    return source_root, vault, manifest_path, backup_path


def _load_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"No se pudo leer el manifiesto de la vault: {error}") from error
    entries = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        raise ValueError("El manifiesto de la vault tiene una estructura inválida.")

    result: dict[str, dict[str, str]] = {}
    for source_path, entry in entries.items():
        if not isinstance(source_path, str) or not isinstance(entry, dict):
            raise ValueError("El manifiesto de la vault contiene una entrada inválida.")
        _safe_relative_path(source_path)
        destination_path = entry.get("destination_path")
        source_hash = entry.get("source_sha256")
        destination_hash = entry.get("destination_sha256")
        if not all(isinstance(value, str) for value in (destination_path, source_hash, destination_hash)):
            raise ValueError("El manifiesto de la vault contiene datos incompletos.")
        _safe_relative_path(destination_path)
        has_resource_source_key = "resource_source_key" in entry
        resource_source_key = entry.get("resource_source_key")
        if has_resource_source_key and (
            not isinstance(resource_source_key, str) or not resource_source_key.strip()
        ):
            raise ValueError("El manifiesto de la vault contiene una identidad de recurso inválida.")
        normalized_entry = {
            "destination_path": destination_path,
            "source_sha256": source_hash,
            "destination_sha256": destination_hash,
        }
        if has_resource_source_key:
            normalized_entry["resource_source_key"] = resource_source_key
        has_conversation_id = "conversation_id" in entry
        conversation_id = entry.get("conversation_id")
        if has_conversation_id and (
            not isinstance(conversation_id, str) or not conversation_id.strip()
        ):
            raise ValueError("El manifiesto de la vault contiene un conversation_id inválido.")
        if has_conversation_id:
            normalized_entry["conversation_id"] = conversation_id
        result[source_path] = normalized_entry
    return result


def _load_export_resource_source_keys(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    resources = payload.get("resources") if isinstance(payload, dict) else None
    if not isinstance(resources, dict):
        return {}

    result: dict[str, str] = {}
    for source_key, entry in resources.items():
        if not isinstance(source_key, str) or not source_key.strip() or not isinstance(entry, dict):
            continue
        exported_path = entry.get("exported_path")
        if not isinstance(exported_path, str) or not exported_path.strip():
            continue
        try:
            safe_path = _safe_relative_path(exported_path)
        except ValueError:
            continue
        result[safe_path.as_posix()] = source_key
    return result


def _read_markdown_conversation_id(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as markdown_file:
            if markdown_file.readline().strip() != "---":
                return None
            conversation_id: str | None = None
            for line in markdown_file:
                line = line.strip()
                if line == "---":
                    return conversation_id
                key, separator, value = line.partition(":")
                if not separator or key.strip() != "conversation_id":
                    continue
                value = value.strip()
                if not value:
                    return None
                if value.startswith('"'):
                    try:
                        parsed = json.loads(value)
                    except json.JSONDecodeError:
                        return None
                    conversation_id = parsed if isinstance(parsed, str) else None
                elif value.startswith("'"):
                    if len(value) < 2 or not value.endswith("'"):
                        return None
                    conversation_id = value[1:-1].replace("''", "'")
                else:
                    conversation_id = value.partition(" #")[0].strip()
                if not conversation_id or not conversation_id.strip():
                    return None
    except (OSError, UnicodeError):
        return None
    return None


def _resource_markdown_reference(source_path: str) -> str | None:
    parts = PurePosixPath(source_path).parts
    if len(parts) < 2:
        return None
    return PurePosixPath(*parts[1:]).as_posix()


def _count_obsidian_target(markdown: str, target: str) -> int:
    return sum(1 for _ in _obsidian_target_pattern(target).finditer(markdown))


def _count_obsidian_targets(
    markdown: str,
    target_trie: dict[str, Any],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    last_match_end: dict[str, int] = {}
    for opening_match in re.finditer(r"(?=(!?\[\[))", markdown):
        opening = opening_match.group(1)
        link_start = opening_match.start(1)
        position = link_start + len(opening)
        node = target_trie
        while position < len(markdown) and markdown[position] in node:
            node = node[markdown[position]]
            position += 1
            target = node.get("")
            if target is None:
                continue

            if markdown.startswith("]]", position):
                match_end = position + 2
            elif markdown.startswith("|", position):
                closing_bracket = markdown.find("]", position + 1)
                if closing_bracket < 0 or not markdown.startswith("]]", closing_bracket):
                    continue
                match_end = closing_bracket + 2
            else:
                continue

            if link_start < last_match_end.get(target, -1):
                continue
            counts[target] = counts.get(target, 0) + 1
            last_match_end[target] = match_end
    return counts


def _build_obsidian_target_trie(targets_by_reference: dict[str, Any]) -> dict[str, Any]:
    trie: dict[str, Any] = {}
    for target in targets_by_reference:
        node = trie
        for character in target:
            node = node.setdefault(character, {})
        node[""] = target
    return trie


def _replace_obsidian_target(markdown: str, old_target: str, new_target: str) -> tuple[str, int]:
    pattern = _obsidian_target_pattern(old_target)
    occurrences = 0

    def replace_target(match: re.Match[str]) -> str:
        nonlocal occurrences
        occurrences += 1
        return f"{match.group(1)}{new_target}{match.group(2) or ''}{match.group(3)}"

    return pattern.sub(replace_target, markdown), occurrences


def _obsidian_target_pattern(target: str) -> re.Pattern[str]:
    return re.compile(rf"(!?\[\[){re.escape(target)}(\|[^\]]*)?(\]\])")


def _plan_files(
    source_root: Path,
    vault: Path,
    previous: dict[str, dict[str, str]],
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    progress_offset: int = 0,
    progress_total: int | None = None,
    source_files: list[Path] | None = None,
) -> list[dict[str, object]]:
    files_to_plan = source_files if source_files is not None else _plan_source_files(source_root)
    if progress_total is None:
        progress_total = progress_offset + len(files_to_plan)
    plans: list[dict[str, object]] = []
    reserved: set[str] = set()
    for index, source in enumerate(files_to_plan, start=1):
        source_relative = source.relative_to(source_root).as_posix()
        if source_relative.casefold() == "export_state.json" or source_relative.casefold().startswith(f"{_MANIFEST_DIRECTORY.casefold()}/"):
            continue
        source_hash = _sha256(source)
        old_entry = previous.get(source_relative)

        if old_entry is None:
            destination_relative = source_relative
            destination = _vault_file(vault, destination_relative)
            if _path_occupied(destination):
                status = "unmanaged"
                destination_relative = _unique_destination(vault, destination_relative, reserved)
            else:
                status = "new"
        else:
            destination_relative = old_entry["destination_path"]
            destination = _vault_file(vault, destination_relative)
            if _path_occupied(destination):
                destination_hash = _sha256(destination) if destination.is_file() and not destination.is_symlink() else None
                if destination_hash != old_entry["destination_sha256"]:
                    status = "conflict"
                    destination_relative = _unique_destination(vault, destination_relative, reserved)
                elif source_hash == old_entry["source_sha256"]:
                    status = "unchanged"
                else:
                    status = "modified"
                    destination_relative = _unique_destination(vault, destination_relative, reserved)
            else:
                status = "modified"

        normalized = destination_relative.casefold()
        if normalized in reserved:
            destination_relative = _unique_destination(vault, destination_relative, reserved)
            normalized = destination_relative.casefold()
            if status == "unchanged":
                status = "conflict"
        reserved.add(normalized)
        plans.append({
            "source_path": source_relative,
            "destination_path": destination_relative,
            "source_sha256": source_hash,
            "status": status,
        })
        _report_analysis_progress(
            progress,
            current=progress_offset + index,
            total=progress_total,
            current_item=source_relative,
        )
    return plans


def _plan_source_files(
    source_root: Path,
    allowed_project_slugs: set[str] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[Path]:
    sources: list[Path] = []
    for source in source_root.rglob("*"):
        sources.append(source)
        if progress is not None:
            progress({
                "current": len(sources),
                "total": 0,
                "current_item": source.relative_to(source_root).as_posix(),
                "discovery": True,
            })
    sources.sort()
    if any(path.is_symlink() for path in sources):
        raise ValueError("La exportación de Obsidian no puede contener enlaces simbólicos.")
    if progress is not None:
        progress({
            "current": len(sources),
            "total": 0,
            "current_item": "",
            "discovery": True,
            "discovery_complete": True,
        })
    return [
        path
        for path in sources
        if path.is_file()
        and path.relative_to(source_root).as_posix().casefold() != "export_state.json"
        and not path.relative_to(source_root).as_posix().casefold().startswith(
            f"{_MANIFEST_DIRECTORY.casefold()}/"
        )
        and (
            allowed_project_slugs is None
            or path.relative_to(source_root).parts[0] in allowed_project_slugs
        )
    ]


def _selected_project_slugs(
    root: Path,
    selected_project_ids: set[str | None] | None,
) -> set[str] | None:
    if selected_project_ids is None:
        return None
    try:
        catalog = json.loads((root / "process" / "index.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"No se pudo leer process/index.json: {error}") from error
    projects = catalog.get("projects") if isinstance(catalog, dict) else None
    if not isinstance(projects, list):
        raise ValueError("El catÃ¡logo de proyectos no tiene la estructura esperada.")
    project_data = []
    for project in projects:
        if not isinstance(project, dict):
            continue
        project_id = project.get("project_id")
        project_name = project.get("project_name")
        if not isinstance(project_id, str) or not isinstance(project_name, str):
            continue
        if project_id not in selected_project_ids and not (
            project_id == "unassigned" and None in selected_project_ids
        ):
            continue
        project_data.append((project_id, project_name))
    bases: dict[str, list[str]] = {}
    for project_id, project_name in project_data:
        base = slugify(project_name, limit=52) or "proyecto"
        bases.setdefault(base.casefold(), []).append(project_id)
    slugs: set[str] = set()
    for project_id, project_name in project_data:
        base = slugify(project_name, limit=52) or "proyecto"
        if len(bases[base.casefold()]) > 1:
            base = f"{base[:55]}--{hashlib.sha1(project_id.encode('utf-8')).hexdigest()[:8]}"
        slugs.add(base)
    return slugs


def _report_analysis_progress(
    progress: Callable[[dict[str, Any]], None] | None,
    *,
    current: int,
    total: int,
    current_item: str,
) -> None:
    if progress is None:
        return
    progress({
        "stage": "validate",
        "phase": "validate",
        "phase_label": "Validar y analizar vault",
        "current": current,
        "total": total,
        "current_item": current_item,
    })


def _report_discovery_progress(
    progress: Callable[[dict[str, Any]], None] | None,
    *,
    current: int,
    current_item: str,
    complete: bool = False,
) -> None:
    if progress is None:
        return
    values: dict[str, Any] = {
        "stage": "validate",
        "phase": "validate",
        "phase_label": "Validar y analizar vault",
        "current": current,
        "total": 0,
        "current_item": current_item,
        "discovery": True,
    }
    if complete:
        values["discovery_complete"] = True
    progress(values)


def _unique_destination(vault: Path, relative_path: str, reserved: set[str]) -> str:
    relative = _safe_relative_path(relative_path)
    path = PurePosixPath(*relative.parts[:-1])
    name = relative.name
    suffix = Path(name).suffix
    stem = name[:-len(suffix)] if suffix else name
    counter = 1
    while True:
        candidate_name = f"{stem}__{counter}{suffix}"
        candidate = (path / candidate_name).as_posix()
        target = _vault_file(vault, candidate)
        if candidate.casefold() not in reserved and not _path_occupied(target):
            return candidate
        counter += 1


def _safe_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("El manifiesto contiene una ruta relativa inválida.")
    relative = PurePosixPath(value)
    if not relative.parts or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("El manifiesto contiene una ruta fuera de la vault.")
    if relative.parts[0].casefold() == _MANIFEST_DIRECTORY.casefold():
        raise ValueError("La ruta del manifiesto está reservada dentro de la vault.")
    return relative


def _vault_file(vault: Path, relative_path: str) -> Path:
    relative = _safe_relative_path(relative_path)
    destination = vault.joinpath(*relative.parts)
    _ensure_safe_parent(vault, destination.parent)
    return destination


def _ensure_safe_parent(vault: Path, parent: Path) -> None:
    current = parent
    while current != vault:
        if current.is_symlink():
            raise ValueError("La ruta de destino de la vault no puede atravesar enlaces simbólicos.")
        if vault not in current.parents:
            raise ValueError("La ruta de destino está fuera de la vault.")
        current = current.parent


def _path_occupied(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(
    manifest_path: Path,
    backup_path: Path,
    entries: dict[str, dict[str, str]],
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path.exists():
        shutil.copy2(manifest_path, backup_path)
    temporary_path = manifest_path.with_name(f".{manifest_path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps({"version": 1, "files": entries}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(manifest_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
