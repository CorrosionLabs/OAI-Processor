from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import secrets
import shutil
import ssl
import sqlite3
import string
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import urllib.error
import urllib.request
from uuid import uuid4
import zipfile

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from core.backup_scanner import CONVERSATION_PATTERN, BackupScanner, BackupValidationError
from core.conversation_extractor import ConversationExtractor, ProjectReference
from core.exporter import ExportError, export_html, export_obsidian
from core.ids import slugify
from core.manifest_reader import read_json_file, read_optional_json
from core.paths import get_settings_file
from core.vault_exporter import (
    analyze_vault_export,
    export_to_vault,
    export_to_vault_assisted,
    update_managed_markdown_links,
)
from web.jobs import JobManager, WebError


WEB_ROOT = Path(__file__).resolve().parent
EXPORT_PROFILE_BOOLEAN_FIELDS = (
    "overwrite_managed",
    "convert_png",
    "convert_wav",
    "preserve_transparent_png",
    "convert_m4a",
    "include_conversation_audio",
    "include_user_attachments",
    "include_project_tag",
    "custom_roles",
)
EXPORT_PROFILE_STRING_FIELDS = ("user_role", "assistant_role")
EXPORT_PROFILE_FIELDS = EXPORT_PROFILE_BOOLEAN_FIELDS + EXPORT_PROFILE_STRING_FIELDS
_OBSIDIAN_API_KEY_SETTINGS_FIELD = "obsidian_api_key"
_SELECTED_PROJECT_IDS_SETTINGS_FIELD = "selected_project_ids"
_OBSIDIAN_API_KEY_PADDING_LENGTH = 40
_OBSIDIAN_API_KEY_PADDING_ALPHABET = string.ascii_letters + string.digits


def available_languages() -> dict[str, dict[str, str]]:
    languages: dict[str, dict[str, str]] = {}
    for path in sorted((WEB_ROOT / "i18n").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        name = payload.get("language.name") if isinstance(payload, dict) else None
        if isinstance(name, str) and name.strip():
            languages[path.stem] = {"code": path.stem, "name": name}
    return languages


def _create_vault_backup_zip(vault_path: Path, backup_folder: Path, update) -> Path:
    temporary_path: Path | None = None
    destination_path: Path | None = None
    destination_existed = False
    backup_completed = False

    try:
        files = sorted(path for path in vault_path.rglob("*") if path.is_file())
        files_with_sizes = [(path, path.stat().st_size) for path in files]
        total_size = sum(size for _, size in files_with_sizes)
        progress_total = total_size if total_size else len(files_with_sizes)
        current = 0
        processed_files = 0

        filename = f"OAI-Processor_backup_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.zip"
        destination_path = backup_folder / filename
        destination_existed = destination_path.exists()
        if destination_existed:
            raise WebError(f"Ya existe un backup con el nombre {filename}.")

        def report(percent: int, current_item: str = "") -> None:
            update(
                stage="backup",
                phase="backup",
                phase_label="Creando backup",
                current=current,
                total=progress_total,
                percent=percent,
                current_item=current_item,
            )

        report(0)
        with tempfile.NamedTemporaryFile(
            prefix=".oai-processor-backup-",
            suffix=".zip",
            dir=vault_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            for source_path, expected_size in files_with_sizes:
                archive_path = source_path.relative_to(vault_path).as_posix()
                file_bytes = 0
                with source_path.open("rb") as source, archive.open(
                    archive_path, mode="w", force_zip64=True
                ) as destination:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        if file_bytes + len(chunk) > expected_size:
                            raise OSError(f"El archivo cambió durante el backup: {archive_path}")
                        destination.write(chunk)
                        file_bytes += len(chunk)
                        if total_size:
                            current += len(chunk)
                            percent = min(99, current * 100 // total_size)
                        report(percent if total_size else 0, archive_path)

                if file_bytes != expected_size:
                    raise OSError(f"El archivo cambió durante el backup: {archive_path}")
                processed_files += 1
                if total_size:
                    percent = min(99, current * 100 // total_size)
                else:
                    current = processed_files
                    percent = min(99, current * 100 // progress_total) if progress_total else 0
                report(percent, archive_path)

        shutil.move(str(temporary_path), str(destination_path))
        temporary_path = None
        current = progress_total
        report(100, destination_path.name)
        backup_completed = True
        return destination_path
    except WebError:
        raise
    except Exception as error:
        raise WebError(f"No se pudo crear el backup ZIP de la vault: {error}") from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        if (
            not backup_completed
            and not destination_existed
            and destination_path is not None
            and destination_path.exists()
        ):
            destination_path.unlink()


def _load_last_path(field: str) -> Path | None:
    try:
        saved = _read_settings(get_settings_file())
        path = Path(saved[field]).expanduser().resolve()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None
    return path if path.is_dir() else None


def _save_last_path(field: str, path: Path) -> None:
    settings_path = get_settings_file()
    settings = _read_settings(settings_path)
    settings[field] = str(path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _write_settings(settings_path, settings)


def load_last_backup_root() -> Path | None:
    return _load_last_path("backup_root")


def save_last_backup_root(root: Path) -> None:
    _save_last_path("backup_root", root)


def analyze_backup_inventory(root: Path) -> dict[str, object]:
    """Compare a downloaded backup with its first inventory, without processing it."""
    downloader_path = root / "oai_downloader.sqlite3"
    if not downloader_path.is_file():
        raise ValueError("No se encontró oai_downloader.sqlite3 en el backup.")

    with closing(sqlite3.connect(f"{downloader_path.as_uri()}?mode=ro", uri=True)) as downloader:
        downloader.execute("BEGIN")
        project_rows = downloader.execute(
            "SELECT id, name FROM projects ORDER BY id"
        ).fetchall()
        conversation_rows = downloader.execute(
            "SELECT id, project_id, remote_updated_at FROM conversations "
            "WHERE download_status = ? ORDER BY id",
            ("downloaded",),
        ).fetchall()

    projects = {project_id: name for project_id, name in project_rows}
    for _, project_id, _ in conversation_rows:
        if project_id is not None and project_id not in projects:
            projects[project_id] = project_id
    project_rows = sorted(projects.items())
    inventory_hash = sha256(json.dumps(
        [project_rows, conversation_rows], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    current_conversations = {
        conversation_id: (project_id, remote_updated_at)
        for conversation_id, project_id, remote_updated_at in conversation_rows
    }
    conversations_by_project: dict[str | None, list[tuple[str, str | None]]] = {}
    for conversation_id, (project_id, remote_updated_at) in current_conversations.items():
        conversations_by_project.setdefault(project_id, []).append(
            (conversation_id, remote_updated_at)
        )

    with closing(sqlite3.connect(root / "oai_processor.sqlite3")) as processor:
        with processor:
            processor.execute("BEGIN IMMEDIATE")
            processor.execute(
                "CREATE TABLE IF NOT EXISTS backup_inventory_reference ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), created_at TEXT NOT NULL)"
            )
            processor.execute(
                "CREATE TABLE IF NOT EXISTS backup_inventory_projects ("
                "project_id TEXT PRIMARY KEY, name TEXT NOT NULL)"
            )
            processor.execute(
                "CREATE TABLE IF NOT EXISTS backup_inventory_conversations ("
                "conversation_id TEXT PRIMARY KEY, project_id TEXT, remote_updated_at TEXT)"
            )
            processor.execute(
                "CREATE TABLE IF NOT EXISTS backup_inventory_analyses ("
                "inventory_hash TEXT PRIMARY KEY, analyzed_at TEXT NOT NULL)"
            )
            processor.execute(
                "CREATE TABLE IF NOT EXISTS backup_inventory_analysis_projects ("
                "inventory_hash TEXT NOT NULL, group_key TEXT NOT NULL, "
                "project_id TEXT, name TEXT NOT NULL, state TEXT NOT NULL, "
                "conversation_count INTEGER NOT NULL, "
                "new_conversation_count INTEGER NOT NULL, "
                "modified_conversation_count INTEGER NOT NULL, "
                "PRIMARY KEY (inventory_hash, group_key))"
            )

            existing_analysis = processor.execute(
                "SELECT 1 FROM backup_inventory_analyses WHERE inventory_hash = ?",
                (inventory_hash,),
            ).fetchone()
            if existing_analysis:
                rows = processor.execute(
                    "SELECT project_id, name, state, conversation_count, "
                    "new_conversation_count, modified_conversation_count "
                    "FROM backup_inventory_analysis_projects "
                    "WHERE inventory_hash = ? ORDER BY group_key",
                    (inventory_hash,),
                ).fetchall()
            else:
                has_reference = processor.execute(
                    "SELECT 1 FROM backup_inventory_reference WHERE id = 1"
                ).fetchone() is not None
                reference_projects = dict(processor.execute(
                    "SELECT project_id, name FROM backup_inventory_projects"
                )) if has_reference else {}
                reference_conversations = {
                    conversation_id: (project_id, remote_updated_at)
                    for conversation_id, project_id, remote_updated_at in processor.execute(
                        "SELECT conversation_id, project_id, remote_updated_at "
                        "FROM backup_inventory_conversations"
                    )
                } if has_reference else {}

                rows_to_save = []
                for project_id, name in [*project_rows, (None, "unassigned")]:
                    group_key = f"project:{project_id}" if project_id is not None else "unassigned"
                    grouped = conversations_by_project.get(project_id, [])
                    new_count = sum(
                        conversation_id not in reference_conversations
                        for conversation_id, _ in grouped
                    )
                    modified_count = sum(
                        conversation_id in reference_conversations
                        and reference_conversations[conversation_id] != (project_id, remote_updated_at)
                        for conversation_id, remote_updated_at in grouped
                    )
                    if not has_reference or (project_id is not None and project_id not in reference_projects):
                        state = "new"
                    elif new_count or modified_count or (
                        project_id is not None and reference_projects[project_id] != name
                    ):
                        state = "modified"
                    else:
                        state = "unchanged"
                    rows_to_save.append((
                        inventory_hash, group_key, project_id, name, state,
                        len(grouped), new_count, modified_count,
                    ))

                analyzed_at = datetime.now(timezone.utc).isoformat()
                if not has_reference:
                    processor.execute(
                        "INSERT INTO backup_inventory_reference (id, created_at) VALUES (1, ?)",
                        (analyzed_at,),
                    )
                    processor.executemany(
                        "INSERT INTO backup_inventory_projects (project_id, name) VALUES (?, ?)",
                        project_rows,
                    )
                    processor.executemany(
                        "INSERT INTO backup_inventory_conversations "
                        "(conversation_id, project_id, remote_updated_at) VALUES (?, ?, ?)",
                        conversation_rows,
                    )
                processor.execute(
                    "INSERT INTO backup_inventory_analyses (inventory_hash, analyzed_at) "
                    "VALUES (?, ?)",
                    (inventory_hash, analyzed_at),
                )
                processor.executemany(
                    "INSERT INTO backup_inventory_analysis_projects "
                    "(inventory_hash, group_key, project_id, name, state, "
                    "conversation_count, new_conversation_count, modified_conversation_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows_to_save,
                )
                rows = [row[2:] for row in sorted(rows_to_save, key=lambda row: row[1])]

    selected_project_ids = _read_selected_project_ids(root)
    return {
        "inventory_hash": inventory_hash,
        "selected_project_ids": (
            list(selected_project_ids)
            if selected_project_ids is not None
            else None
        ),
        "projects": [
            {
                "project_id": project_id,
                "name": name,
                "state": state,
                "conversation_count": conversation_count,
                "new_conversation_count": new_count,
                "modified_conversation_count": modified_count,
            }
            for project_id, name, state, conversation_count, new_count, modified_count in rows
        ],
    }


def load_last_vault_path() -> Path | None:
    return _load_last_path("vault_path")


def save_last_vault_path(path: Path) -> None:
    _save_last_path("vault_path", path)


def load_last_html_path() -> Path | None:
    return _load_last_path("html_path")


def save_last_html_path(path: Path) -> None:
    _save_last_path("html_path", path)


def _read_export_profiles(
    path: Path, *, include_legacy_defaults: bool = True
) -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"No se pudo leer export_profiles.json: {error}") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "profiles"}
        or type(payload.get("version")) is not int
        or payload.get("version") != 1
        or not isinstance(payload.get("profiles"), dict)
    ):
        raise ValueError("export_profiles.json tiene una estructura inválida.")

    profiles: dict[str, dict[str, object]] = {}
    legacy_fields = set(EXPORT_PROFILE_FIELDS) - {"include_user_attachments"}
    for name, profile in payload["profiles"].items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(profile, dict)
            or set(profile) not in (set(EXPORT_PROFILE_FIELDS), legacy_fields)
        ):
            raise ValueError("export_profiles.json contiene un perfil inválido.")
        if any(
            type(profile.get(field)) is not bool
            for field in EXPORT_PROFILE_BOOLEAN_FIELDS
            if field != "include_user_attachments" or field in profile
        ):
            raise ValueError(f"El perfil {name!r} contiene opciones que no son booleanas.")
        if any(not isinstance(profile.get(field), str) for field in EXPORT_PROFILE_STRING_FIELDS):
            raise ValueError(f"El perfil {name!r} contiene nombres de rol inválidos.")
        if profile["custom_roles"] and (
            not profile["user_role"].strip() or not profile["assistant_role"].strip()
        ):
            raise ValueError(f"El perfil {name!r} tiene nombres de rol vacíos.")
        profiles[name] = {
            field: profile[field]
            for field in EXPORT_PROFILE_FIELDS
            if field in profile
        }
        if include_legacy_defaults:
            profiles[name].setdefault("include_user_attachments", False)
    return profiles


def _write_export_profiles(path: Path, profiles: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps({"version": 1, "profiles": profiles}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _read_settings(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"No se pudo leer settings.json: {error}") from error
    if not isinstance(settings, dict):
        raise ValueError("settings.json tiene una estructura inválida.")
    return settings


def _write_settings(path: Path, settings: dict[str, object]) -> None:
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _read_selected_project_ids(root: Path) -> set[str | None] | None:
    settings = _read_settings(root / "settings.json")
    value = settings.get(_SELECTED_PROJECT_IDS_SETTINGS_FIELD)
    if not isinstance(value, list):
        return None
    if any(project_id is not None and not isinstance(project_id, str) for project_id in value):
        return None
    return set(value)


def _obfuscate_obsidian_api_key(api_key: str) -> str:
    prefix = "".join(
        secrets.choice(_OBSIDIAN_API_KEY_PADDING_ALPHABET)
        for _ in range(_OBSIDIAN_API_KEY_PADDING_LENGTH)
    )
    suffix = "".join(
        secrets.choice(_OBSIDIAN_API_KEY_PADDING_ALPHABET)
        for _ in range(_OBSIDIAN_API_KEY_PADDING_LENGTH)
    )
    return f"{prefix}{api_key}{suffix}"[::-1]


def _recover_obsidian_api_key(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("El token recordado de Obsidian no es válido.")
    restored = value[::-1]
    if len(restored) <= _OBSIDIAN_API_KEY_PADDING_LENGTH * 2:
        raise ValueError("El token recordado de Obsidian no es válido.")
    api_key = restored[
        _OBSIDIAN_API_KEY_PADDING_LENGTH:-_OBSIDIAN_API_KEY_PADDING_LENGTH
    ]
    if not api_key:
        raise ValueError("El token recordado de Obsidian no es válido.")
    return api_key


def create_app() -> FastAPI:
    app = FastAPI(title="OAI-Processor")
    app.state.jobs = JobManager()
    saved_backup_root = load_last_backup_root()
    if saved_backup_root is not None:
        app.state.backup_root = str(saved_backup_root)
    saved_vault_path = load_last_vault_path()
    if saved_vault_path is not None:
        app.state.vault_path = str(saved_vault_path)
    saved_html_path = load_last_html_path()
    if saved_html_path is not None:
        app.state.html_path = str(saved_html_path)

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )

    app.mount(
        "/static",
        StaticFiles(directory=WEB_ROOT / "static"),
        name="static",
    )

    templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        response = await call_next(request)

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'; "
            "base-uri 'self'"
        )

        return response

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, lang: str = "es"):
        backup_root = getattr(app.state, "backup_root", None)
        vault_path = getattr(app.state, "vault_path", None)
        html_path = getattr(app.state, "html_path", None)
        languages = available_languages()
        if lang in languages:
            language = lang
        elif "es" in languages:
            language = "es"
        elif languages:
            language = next(iter(languages))
        else:
            language = "es"
        translations = json.loads(
            (WEB_ROOT / "i18n" / f"{language}.json").read_text(encoding="utf-8")
        )
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "backup_root": backup_root,
                "process_root": str(Path(backup_root) / "process") if backup_root else None,
                "vault_path": vault_path,
                "html_path": html_path,
                "language": language,
                "languages": list(languages.values()),
                "translations": translations,
            },
        )

    @app.post("/api/connect")
    def connect_backup():
        picker = WEB_ROOT / "folder_picker.py"
        try:
            result = subprocess.run(
                [sys.executable, str(picker), str(Path.home())],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=300,
                check=True,
            )
            backup_root = json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            return JSONResponse({"detail": "El selector de carpetas tardó demasiado."}, status_code=504)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return JSONResponse({"detail": "No se pudo abrir el selector de carpetas."}, status_code=500)

        if not backup_root:
            return {"cancelled": True}

        root = Path(backup_root).resolve()
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta seleccionada ya no está disponible."}, status_code=400)
        process_root = root / "process"
        try:
            process_root.mkdir(exist_ok=True)
        except OSError as error:
            return JSONResponse({"detail": f"No se pudo crear la carpeta process: {error}"}, status_code=400)

        try:
            save_last_backup_root(root)
        except OSError:
            return JSONResponse({"detail": "No se pudo guardar la carpeta seleccionada."}, status_code=500)
        app.state.backup_root = str(root)
        return {"cancelled": False, "backup_root": str(root), "process_root": str(process_root)}

    @app.post("/api/select-folder")
    def select_folder(payload: dict | None = None):
        picker = WEB_ROOT / "folder_picker.py"
        try:
            result = subprocess.run(
                [sys.executable, str(picker), str(Path.home())],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=300,
                check=True,
            )
            selected_path = json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            return JSONResponse({"detail": "El selector de carpetas tardó demasiado."}, status_code=504)
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
            return JSONResponse({"detail": "No se pudo abrir el selector de carpetas."}, status_code=500)

        if not selected_path:
            return {"cancelled": True}

        root = Path(selected_path).resolve()
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta seleccionada ya no está disponible."}, status_code=400)
        if isinstance(payload, dict) and payload.get("purpose") == "html":
            try:
                save_last_html_path(root)
            except OSError as error:
                return JSONResponse(
                    {"detail": f"No se pudo guardar la ruta de la carpeta HTML: {error}"},
                    status_code=500,
                )
            app.state.html_path = str(root)
        return {"cancelled": False, "path": str(root)}

    @app.post("/api/clean")
    def clean_backup(payload: dict):
        delete_exports = payload.get("delete_exports")
        if not isinstance(delete_exports, bool):
            return JSONResponse(
                {"detail": "delete_exports debe ser true o false."},
                status_code=400,
            )

        deleted: list[str] = []
        try:
            with app.state.jobs.exclusive():
                backup_root = getattr(app.state, "backup_root", None)
                if not backup_root:
                    return JSONResponse(
                        {"detail": "Selecciona primero la carpeta del backup."},
                        status_code=400,
                    )

                root = Path(backup_root)
                if not root.is_dir():
                    return JSONResponse(
                        {"detail": "La carpeta del backup ya no está disponible."},
                        status_code=400,
                    )

                directories = ["process"]
                if delete_exports:
                    directories.append("export")
                for name in directories:
                    path = root / name
                    if path.exists():
                        shutil.rmtree(path)
                        deleted.append(name)

                for name in (
                    "oai_processor.sqlite3",
                    "oai_processor.sqlite3-wal",
                    "oai_processor.sqlite3-shm",
                ):
                    path = root / name
                    if path.exists():
                        path.unlink()
                        deleted.append(name)

                app.state.jobs.clear()
                return {"deleted": deleted, "delete_exports": delete_exports}
        except WebError as error:
            if str(error) == "busy":
                return JSONResponse(
                    {"detail": "Hay una operación en curso; inténtalo de nuevo cuando termine."},
                    status_code=409,
                )
            raise
        except OSError as error:
            return JSONResponse(
                {"detail": f"No se pudo completar la limpieza: {error}", "deleted": deleted},
                status_code=500,
            )

    @app.post("/api/analyze")
    def start_backup_analysis():
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)

        root = Path(backup_root)
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta del backup ya no está disponible."}, status_code=400)
        if not (root / "oai_downloader.sqlite3").is_file():
            return JSONResponse({"detail": "No se encontró oai_downloader.sqlite3 en el backup."}, status_code=400)

        def operation(update, _should_stop):
            update(phase="analysis")
            try:
                return analyze_backup_inventory(root)
            except (ValueError, OSError, sqlite3.Error) as error:
                raise WebError(str(error)) from error

        try:
            job = app.state.jobs.start("analyze", operation)
        except WebError:
            return JSONResponse({"detail": "Ya hay una operación en curso."}, status_code=409)
        return JSONResponse(job, status_code=202)

    @app.get("/api/analyze/status")
    def backup_analysis_status():
        return app.state.jobs.snapshot("analyze")

    @app.get("/api/process/projects")
    def list_processing_projects():
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)

        root = Path(backup_root)
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta del backup ya no está disponible."}, status_code=400)

        try:
            source_root = root / "backup"
            if not source_root.is_dir():
                source_root = root

            extractor = ConversationExtractor()
            conversations = []
            detected_projects = []
            documents_found = False

            conversations_dir = source_root / "conversations"
            if conversations_dir.is_dir():
                for path in sorted(conversations_dir.glob("*.json")):
                    documents_found = True
                    for source in BackupScanner._iter_conversations(read_json_file(path)):
                        conversations.append(SimpleNamespace(project=None, messages=[]))

            used_slugs = {"unassigned"}
            projects_dir = source_root / "projects"
            if projects_dir.is_dir():
                for folder in sorted(projects_dir.iterdir()):
                    if not folder.is_dir():
                        continue
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
                        source_path=(folder / "project.json").relative_to(root).as_posix(),
                        slug=slug,
                    )
                    detected_projects.append(project)
                    for path in sorted((folder / "conversations").glob("*.json")):
                        documents_found = True
                        for source in BackupScanner._iter_conversations(read_json_file(path)):
                            conversations.append(SimpleNamespace(project=project, messages=[]))

            if not documents_found:
                for path in sorted(source_root.glob("conversations*.json")):
                    if not CONVERSATION_PATTERN.match(path.name):
                        continue
                    documents_found = True
                    for source in BackupScanner._iter_conversations(read_json_file(path)):
                        conversations.append(SimpleNamespace(
                            project=extractor._find_project_reference(source),
                            messages=[],
                        ))

            if not documents_found:
                raise BackupValidationError("No se encontraron conversaciones JSON en el backup seleccionado.")

            projects = BackupScanner._assign_projects(conversations, detected_projects)
        except (BackupValidationError, OSError, UnicodeError, ValueError) as error:
            return JSONResponse({"detail": f"No se pudo leer el backup: {error}"}, status_code=400)

        counts: dict[str, int] = {}
        for conversation in conversations:
            slug = conversation.project.slug if conversation.project else "unassigned"
            counts[slug] = counts.get(slug, 0) + 1

        try:
            selected_project_ids = _read_selected_project_ids(root)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)

        return {
            "selected_project_ids": (
                list(selected_project_ids) if selected_project_ids is not None else None
            ),
            "projects": [
            {
                "project_id": project.project_id,
                "name": project.project_name,
                "slug": project.slug,
                "conversation_count": counts.get(project.slug, 0),
            }
            for project in projects
        ] + [{
            "project_id": None,
            "name": "unassigned",
            "slug": "unassigned",
            "conversation_count": counts.get("unassigned", 0),
            }],
        }

    @app.get("/api/process/check")
    def check_processing():
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)

        root = Path(backup_root)
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta del backup ya no está disponible."}, status_code=400)

        downloader_path = root / "oai_downloader.sqlite3"
        if not downloader_path.is_file():
            return JSONResponse({"detail": "No se encontró oai_downloader.sqlite3 en el backup."}, status_code=400)

        try:
            with closing(sqlite3.connect(f"{downloader_path.as_uri()}?mode=ro", uri=True)) as downloader:
                conversations = downloader.execute(
                    "SELECT id, remote_updated_at FROM conversations "
                    "WHERE download_status = ?",
                    ("downloaded",),
                ).fetchall()

            with closing(sqlite3.connect(root / "oai_processor.sqlite3")) as processor:
                processor.execute(
                    "CREATE TABLE IF NOT EXISTS processed_conversations ("
                    "conversation_id TEXT PRIMARY KEY, "
                    "processed_remote_updated_at TEXT, "
                    "processed_at TEXT, "
                    "status TEXT)"
                )
                processed = dict(processor.execute(
                    "SELECT conversation_id, processed_remote_updated_at "
                    "FROM processed_conversations"
                ).fetchall())
        except sqlite3.Error as error:
            return JSONResponse(
                {"detail": f"No se pudo comprobar el estado del backup: {error}"},
                status_code=500,
            )

        new_count = sum(conversation_id not in processed for conversation_id, _ in conversations)
        updated_count = sum(
            conversation_id in processed and remote_updated_at != processed[conversation_id]
            for conversation_id, remote_updated_at in conversations
        )
        total = len(conversations)
        current_count = total - new_count - updated_count
        state = "new" if not processed else "updated" if new_count or updated_count else "current"
        manifest_path = root / "process" / "data" / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            summary_fields = (
                "project_count",
                "conversation_count",
                "message_count",
                "resource_count",
                "materialized_resource_count",
                "missing_resource_count",
            )
            processed_summary = (
                {field: manifest[field] for field in summary_fields}
                if isinstance(manifest, dict)
                else None
            )
        except (OSError, ValueError, TypeError, KeyError):
            processed_summary = None

        return {
            "state": state,
            "total": total,
            "new": new_count,
            "updated": updated_count,
            "current": current_count,
            "processed_summary": processed_summary,
        }

    @app.post("/api/process")
    def start_processing(payload: dict):
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)

        root = Path(backup_root)
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta del backup ya no estÃ¡ disponible."}, status_code=400)
        project_ids = payload.get("project_ids") if isinstance(payload, dict) else None
        if not isinstance(project_ids, list) or not project_ids:
            return JSONResponse(
                {"detail": "project_ids debe ser una lista no vacÃ­a."}, status_code=400
            )
        if any(
            project_id is not None
            and (
                not isinstance(project_id, str)
                or not project_id.strip()
                or project_id != project_id.strip()
            )
            for project_id in project_ids
        ):
            return JSONResponse({"detail": "project_ids contiene un identificador invÃ¡lido."}, status_code=400)
        if len(set(project_ids)) != len(project_ids):
            return JSONResponse({"detail": "project_ids no puede contener duplicados."}, status_code=400)

        try:
            source_root = root / "backup"
            if not source_root.is_dir():
                source_root = root
            available_project_ids: set[str | None] = {None}
            projects_dir = source_root / "projects"
            if projects_dir.is_dir():
                for folder in projects_dir.iterdir():
                    if not folder.is_dir():
                        continue
                    project_data = read_optional_json(folder / "project.json")
                    raw_id = project_data.get("id") if isinstance(project_data, dict) else None
                    if isinstance(raw_id, str) and raw_id.strip():
                        available_project_ids.add(raw_id.strip())
        except (OSError, UnicodeError, ValueError) as error:
            return JSONResponse({"detail": f"No se pudo leer el backup: {error}"}, status_code=400)
        unknown_project_ids = [
            project_id for project_id in project_ids if project_id not in available_project_ids
        ]
        if unknown_project_ids:
            return JSONResponse(
                {"detail": "project_ids contiene un identificador desconocido."}, status_code=400
            )

        try:
            settings = _read_settings(root / "settings.json")
            settings[_SELECTED_PROJECT_IDS_SETTINGS_FIELD] = project_ids
            _write_settings(root / "settings.json", settings)
        except (ValueError, OSError) as error:
            return JSONResponse(
                {"detail": f"No se pudo guardar la selección de proyectos: {error}"},
                status_code=500,
            )

        def operation(update, _should_stop):
            totals = {
                "project_count": 0,
                "conversation_count": 0,
                "message_count": 0,
                "resource_count": 0,
                "materialized_resource_count": 0,
                "missing_resource_count": 0,
            }
            scanner = BackupScanner(root, root / "process")
            total_projects = len(project_ids)
            try:
                for project_index, project_id in enumerate(project_ids):
                    def progress(progress_payload: dict, *, current_project: int = project_index) -> None:
                        payload_with_total = dict(progress_payload)
                        payload_with_total["percent"] = round(
                            (
                                current_project
                                + float(progress_payload.get("percent", 0)) / 100
                            ) / total_projects * 100,
                            2,
                        )
                        update(**payload_with_total)

                    result = scanner.process_project_package(project_id, progress=progress)
                    totals["project_count"] += 1
                    for key in (
                        "conversation_count",
                        "message_count",
                        "resource_count",
                        "materialized_resource_count",
                        "missing_resource_count",
                    ):
                        totals[key] += result[key]
            except BackupValidationError as error:
                raise WebError(f"backup_invalido: {error}") from error
            return totals

        try:
            job = app.state.jobs.start(
                "process", operation, log_path=root / "process" / "logs" / "jobs_errors.log"
            )
        except WebError:
            return JSONResponse({"detail": "Ya hay un procesamiento en curso."}, status_code=409)
        return JSONResponse(job, status_code=202)

    @app.get("/api/process/status")
    def processing_status():
        return app.state.jobs.snapshot("process")

    @app.post("/api/obsidian/check")
    def check_obsidian_connection(payload: dict):
        api_key = payload.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            return JSONResponse(
                {"valid": False, "detail": "La API Key de Obsidian no puede estar vacía."},
                status_code=400,
            )
        api_key = api_key.strip()

        request = urllib.request.Request(
            "https://127.0.0.1:27124/",
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
                timeout=5,
                context=local_tls_context,
            ) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                detail = "La API Key de Obsidian no es válida."
                status_code = 401
            elif error.code == 429:
                detail = "El plugin ha bloqueado temporalmente las comprobaciones por demasiados intentos fallidos."
                status_code = 429
            else:
                detail = f"El plugin Local REST API respondió con el error HTTP {error.code}."
                status_code = 502
            return JSONResponse({"valid": False, "detail": detail}, status_code=status_code)
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                detail = "Se agotó el tiempo de espera al conectar con Obsidian."
                status_code = 504
            else:
                detail = "No se pudo conectar con Obsidian. Comprueba que esté abierto y que Local REST API esté activado."
                status_code = 503
            return JSONResponse({"valid": False, "detail": detail}, status_code=status_code)
        except TimeoutError:
            return JSONResponse(
                {"valid": False, "detail": "Se agotó el tiempo de espera al conectar con Obsidian."},
                status_code=504,
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JSONResponse(
                {"valid": False, "detail": "El plugin Local REST API devolvió una respuesta no válida."},
                status_code=502,
            )

        if not isinstance(response_payload, dict) or (
            response_payload.get("status") != "OK"
            or response_payload.get("service") != "Obsidian Local REST API"
        ):
            return JSONResponse(
                {"valid": False, "detail": "El servicio disponible no es el plugin Local REST API esperado."},
                status_code=502,
            )
        if response_payload.get("authenticated") is not True:
            return JSONResponse(
                {"valid": False, "detail": "La API Key de Obsidian no es válida."},
                status_code=401,
            )
        state = response_payload.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("metadataCache"), dict):
            return JSONResponse(
                {"valid": False, "detail": "El plugin no permite acceder a state.metadataCache."},
                status_code=502,
            )
        return JSONResponse({"valid": True})

    @app.get("/api/obsidian/api-key")
    def get_remembered_obsidian_api_key():
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)
        root = Path(backup_root)
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta del backup no está disponible."}, status_code=400)

        settings_path = root / "settings.json"
        try:
            settings = _read_settings(settings_path)
            if _OBSIDIAN_API_KEY_SETTINGS_FIELD not in settings:
                return JSONResponse({"remembered": False})
            api_key = _recover_obsidian_api_key(settings[_OBSIDIAN_API_KEY_SETTINGS_FIELD])
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        return JSONResponse({"remembered": True, "api_key": api_key})

    @app.post("/api/obsidian/api-key")
    def save_remembered_obsidian_api_key(payload: dict):
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)
        root = Path(backup_root)
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta del backup no está disponible."}, status_code=400)

        api_key = payload.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            return JSONResponse({"detail": "La API Key de Obsidian no puede estar vacía."}, status_code=400)

        settings_path = root / "settings.json"
        try:
            settings = _read_settings(settings_path)
            settings[_OBSIDIAN_API_KEY_SETTINGS_FIELD] = _obfuscate_obsidian_api_key(api_key.strip())
            _write_settings(settings_path, settings)
        except (ValueError, OSError) as error:
            return JSONResponse({"detail": f"No se pudo guardar el token de Obsidian: {error}"}, status_code=500)
        return JSONResponse({"remembered": True})

    @app.delete("/api/obsidian/api-key")
    def delete_remembered_obsidian_api_key():
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)
        root = Path(backup_root)
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta del backup no está disponible."}, status_code=400)

        settings_path = root / "settings.json"
        try:
            settings = _read_settings(settings_path)
            if _OBSIDIAN_API_KEY_SETTINGS_FIELD not in settings:
                return JSONResponse({"remembered": False})
            del settings[_OBSIDIAN_API_KEY_SETTINGS_FIELD]
            if settings:
                _write_settings(settings_path, settings)
            else:
                settings_path.unlink()
        except (ValueError, OSError) as error:
            return JSONResponse({"detail": f"No se pudo eliminar el token de Obsidian: {error}"}, status_code=500)
        return JSONResponse({"remembered": False})

    @app.get("/api/export/profiles")
    def list_export_profiles():
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)
        root = Path(backup_root)
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta del backup no está disponible."}, status_code=400)

        profiles_path = root / "export_profiles.json"
        if not profiles_path.exists():
            return JSONResponse({"profiles": []})
        try:
            profiles = _read_export_profiles(profiles_path)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        return JSONResponse({
            "profiles": [
                {"name": name, **profiles[name]}
                for name in sorted(profiles, key=str.casefold)
            ]
        })

    @app.post("/api/export/profiles")
    def save_export_profile(payload: dict):
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)
        root = Path(backup_root)
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta del backup no está disponible."}, status_code=400)

        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse({"detail": "El nombre del perfil no puede estar vacío."}, status_code=400)
        name = name.strip()

        profile: dict[str, object] = {}
        for field in EXPORT_PROFILE_BOOLEAN_FIELDS:
            value = payload.get(field)
            if type(value) is not bool:
                return JSONResponse({"detail": f"La opción {field} debe ser booleana."}, status_code=400)
            profile[field] = value
        for field in EXPORT_PROFILE_STRING_FIELDS:
            value = payload.get(field)
            if not isinstance(value, str):
                return JSONResponse({"detail": f"El campo {field} debe ser texto."}, status_code=400)
            profile[field] = value

        if profile["custom_roles"]:
            profile["user_role"] = str(profile["user_role"]).strip()
            profile["assistant_role"] = str(profile["assistant_role"]).strip()
            if not profile["user_role"] or not profile["assistant_role"]:
                return JSONResponse(
                    {"detail": "Los nombres personalizados de Usuario y Asistente no pueden estar vacíos."},
                    status_code=400,
                )

        profiles_path = root / "export_profiles.json"
        try:
            profiles = (
                _read_export_profiles(
                    profiles_path, include_legacy_defaults=False
                )
                if profiles_path.exists()
                else {}
            )
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)

        profiles[name] = profile
        try:
            _write_export_profiles(profiles_path, profiles)
        except OSError as error:
            return JSONResponse(
                {"detail": f"No se pudieron guardar los perfiles de exportación: {error}"},
                status_code=500,
            )
        return JSONResponse({"status": "success", "profile": {"name": name, **profile}})

    @app.delete("/api/export/profiles")
    def delete_export_profile(payload: dict):
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)
        root = Path(backup_root)
        if not root.is_dir():
            return JSONResponse({"detail": "La carpeta del backup no está disponible."}, status_code=400)

        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse({"detail": "El nombre del perfil no puede estar vacío."}, status_code=400)
        name = name.strip()

        profiles_path = root / "export_profiles.json"
        if not profiles_path.exists():
            return JSONResponse({"detail": "El perfil no existe."}, status_code=404)
        try:
            profiles = _read_export_profiles(
                profiles_path, include_legacy_defaults=False
            )
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        if name not in profiles:
            return JSONResponse({"detail": "El perfil no existe."}, status_code=404)

        del profiles[name]
        try:
            _write_export_profiles(profiles_path, profiles)
        except OSError as error:
            return JSONResponse(
                {"detail": f"No se pudieron guardar los perfiles de exportación: {error}"},
                status_code=500,
            )
        return JSONResponse({"status": "success", "deleted": name})

    @app.post("/api/export")
    def start_export(payload: dict):
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)
        export_format = payload.get("format")
        if export_format not in ("obsidian", "html"):
            return JSONResponse({"detail": "Formato de exportación no disponible."}, status_code=400)

        custom_roles = payload.get("custom_roles") is True
        user_role = payload.get("user_role")
        assistant_role = payload.get("assistant_role")
        if custom_roles:
            if (
                not isinstance(user_role, str)
                or not user_role.strip()
                or not isinstance(assistant_role, str)
                or not assistant_role.strip()
            ):
                return JSONResponse(
                    {"detail": "Los nombres personalizados de Usuario y Asistente no pueden estar vacíos."},
                    status_code=400,
                )
            user_role = user_role.strip()
            assistant_role = assistant_role.strip()
        else:
            user_role = "Usuario"
            assistant_role = "Asistente"

        if export_format == "obsidian":
            vault_path = payload.get("vault_path")
            if not isinstance(vault_path, str) or not vault_path.strip():
                return JSONResponse({"detail": "Selecciona una vault de Obsidian."}, status_code=400)
            vault_path = Path(vault_path).expanduser().resolve()
            if not vault_path.exists() or not vault_path.is_dir():
                return JSONResponse({"detail": "La vault de Obsidian no está disponible."}, status_code=400)
            try:
                save_last_vault_path(vault_path)
            except OSError as error:
                return JSONResponse(
                    {"detail": f"No se pudo guardar la ruta de la vault de Obsidian: {error}"},
                    status_code=500,
                )
            app.state.vault_path = str(vault_path)
            destination_path = vault_path
        else:
            html_path = payload.get("html_path")
            if not isinstance(html_path, str) or not html_path.strip():
                return JSONResponse({"detail": "Selecciona una carpeta de destino HTML."}, status_code=400)
            html_path = Path(html_path).expanduser().resolve()
            if not html_path.exists() or not html_path.is_dir():
                return JSONResponse({"detail": "La carpeta de destino HTML no está disponible."}, status_code=400)
            try:
                save_last_html_path(html_path)
            except OSError as error:
                return JSONResponse(
                    {"detail": f"No se pudo guardar la ruta de la carpeta HTML: {error}"},
                    status_code=500,
                )
            app.state.html_path = str(html_path)
            destination_path = html_path

        root = Path(backup_root)
        try:
            selected_project_ids = _read_selected_project_ids(root)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        process_root = root / "process"
        if not (process_root / "index.json").is_file():
            return JSONResponse({"detail": "Procesa primero el backup seleccionado."}, status_code=400)

        backup_before = payload.get("backup_before") is True

        def operation(update, _should_stop):
            try:
                if backup_before:
                    backup_folder_value = payload.get("backup_folder")
                    if not isinstance(backup_folder_value, str) or not backup_folder_value.strip():
                        raise WebError("Selecciona una carpeta válida para guardar el backup ZIP.")
                    try:
                        backup_folder = Path(backup_folder_value).expanduser().resolve()
                    except (OSError, RuntimeError, ValueError) as error:
                        raise WebError(f"La carpeta del backup ZIP no es válida: {error}") from error
                    if not backup_folder.is_dir():
                        raise WebError("La carpeta seleccionada para el backup ZIP no está disponible.")
                    _create_vault_backup_zip(destination_path, backup_folder, update)

                exporter = export_obsidian if export_format == "obsidian" else export_html
                result = exporter(
                    root,
                    convert_png=payload.get("convert_png") is True,
                    convert_wav=payload.get("convert_wav") is True,
                    preserve_transparent_png=payload.get("preserve_transparent_png") is True,
                    convert_m4a=payload.get("convert_m4a") is True,
                    include_conversation_audio=payload.get("include_conversation_audio") is True,
                    include_user_attachments=payload.get("include_user_attachments") is True,
                    include_project_tag=payload.get("include_project_tag") is True,
                    custom_roles=custom_roles,
                    user_role=user_role,
                    assistant_role=assistant_role,
                    selected_project_ids=selected_project_ids,
                    progress=lambda values: update(**values),
                )
                if export_format == "html":
                    intermediate_root = Path(result["output_root"])
                    try:
                        if intermediate_root.resolve() != html_path:
                            shutil.copytree(intermediate_root, html_path, dirs_exist_ok=True)
                    except (OSError, shutil.Error) as error:
                        raise WebError(f"No se pudo copiar la exportación HTML al destino: {error}") from error
                    result["destination_path"] = str(html_path)
                    return result
                try:
                    vault_analysis = analyze_vault_export(
                        root,
                        vault_path,
                        selected_project_ids=selected_project_ids,
                        progress=lambda values: update(**values),
                    )
                except (ValueError, OSError) as error:
                    raise WebError(str(error)) from error
                result["vault_analysis"] = vault_analysis
                result["vault_pending"] = True
                return result
            except ExportError as error:
                raise WebError(str(error)) from error

        try:
            job = app.state.jobs.start("export", operation)
        except WebError:
            return JSONResponse({"detail": "Ya hay una operación en curso."}, status_code=409)
        return JSONResponse(job, status_code=202)

    @app.get("/api/export/status")
    def export_status():
        return app.state.jobs.snapshot("export")

    @app.post("/api/export/vault")
    def complete_vault_export(payload: dict):
        backup_root = getattr(app.state, "backup_root", None)
        if not backup_root:
            return JSONResponse({"detail": "Selecciona primero la carpeta del backup."}, status_code=400)

        vault_path = payload.get("vault_path")
        if not isinstance(vault_path, str) or not vault_path.strip():
            return JSONResponse({"detail": "Selecciona una vault de Obsidian."}, status_code=400)
        vault_path = Path(vault_path).expanduser().resolve()
        if not vault_path.exists() or not vault_path.is_dir():
            return JSONResponse({"detail": "La vault de Obsidian no está disponible."}, status_code=400)

        root = Path(backup_root)
        if not root.exists() or not root.is_dir():
            return JSONResponse({"detail": "La carpeta del backup no está disponible."}, status_code=400)

        try:
            selected_project_ids = _read_selected_project_ids(root)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)

        update_markdown_links = payload.get("update_markdown_links") is True
        overwrite_managed = payload.get("overwrite_managed") is True
        assisted = payload.get("assisted") is True
        api_key = payload.get("api_key")
        if assisted and (not isinstance(api_key, str) or not api_key.strip()):
            return JSONResponse({"detail": "La API Key de Obsidian no puede estar vacía."}, status_code=400)
        if isinstance(api_key, str):
            api_key = api_key.strip()

        def operation(update, _should_stop):
            link_update_result = None
            if update_markdown_links:
                try:
                    link_update_result = update_managed_markdown_links(
                        root,
                        vault_path,
                        progress=lambda values: update(**values),
                    )
                except (ValueError, OSError) as error:
                    raise WebError(str(error)) from error

            if assisted:
                try:
                    vault_result = export_to_vault_assisted(
                        root,
                        vault_path,
                        api_key=api_key,
                        overwrite_managed=overwrite_managed,
                        selected_project_ids=selected_project_ids,
                        progress=lambda values: update(**values),
                    )
                except (ValueError, OSError, RuntimeError) as error:
                    raise WebError(str(error)) from error
            else:
                try:
                    vault_result = export_to_vault(
                        root,
                        vault_path,
                        overwrite_managed=overwrite_managed,
                        selected_project_ids=selected_project_ids,
                        progress=lambda values: update(**values),
                    )
                except (ValueError, OSError) as error:
                    raise WebError(str(error)) from error

            return {
                "status": "success",
                "link_update": link_update_result,
                "vault_export": vault_result,
            }

        try:
            job = app.state.jobs.start("vault", operation)
        except WebError:
            return JSONResponse({"detail": "Ya hay una operación en curso."}, status_code=409)
        return JSONResponse(job, status_code=202)

    @app.get("/api/export/vault/status")
    def vault_export_status():
        return app.state.jobs.snapshot("vault")

    return app


app = create_app()
