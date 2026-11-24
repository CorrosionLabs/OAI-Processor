from __future__ import annotations
from dataclasses import dataclass
from hashlib import sha1
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Iterator
import warnings

from core.ids import slugify
from core.models import ProjectPackageManifest, StoreManifest, to_plain_data
from core.package_reader import ProjectPackageReader


@dataclass(frozen=True)
class ProjectPackageRepository:
    """Paths that make up an isolated project processing package."""

    package_root: Path
    project_id: str
    manifest_path: Path
    index_path: Path
    conversations_dir: Path
    resources_dir: Path

    def save_conversations(self, conversations: list[Any]) -> dict[tuple[str, str], str]:
        locations: dict[tuple[str, str], str] = {}
        for conversation in conversations:
            slug = conversation.project.slug if conversation.project and conversation.project.slug else "unassigned"
            digest = sha1(conversation.conversation_id.encode("utf-8")).hexdigest()[:8]
            filename = f"{slugify(conversation.conversation_id, limit=60)}__{digest}.json"
            path = self.conversations_dir / filename
            path.write_text(
                json.dumps(to_plain_data(conversation), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            locations[(slug, conversation.conversation_id)] = path.relative_to(self.package_root).as_posix()
        return locations

    def save_index_payload(self, payload: dict[str, Any]) -> None:
        self.index_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def save_manifest(self, manifest: ProjectPackageManifest) -> None:
        self.manifest_path.write_text(
            json.dumps(to_plain_data(manifest), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


class ConversationStoreRepository:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir.resolve()
        self.data_root = self.output_dir / "data"
        self.projects_dir = self.data_root / "projects"
        self.resources_dir = self.output_dir / "resources"
        self.manifest_path = self.data_root / "manifest.json"
        self.index_path = self.data_root / "index.json"

    def prepare(self, project_slugs: set[str]) -> None:
        if self.output_dir.name != "process" or self.output_dir.is_symlink():
            raise ValueError("La salida debe ser una carpeta process normal.")
        if self.output_dir.exists():
            if not self.output_dir.is_dir():
                raise ValueError(f"La ruta de salida no es una carpeta: {self.output_dir}")
            shutil.rmtree(self.output_dir)
        for slug in project_slugs | {"unassigned"}:
            (self.projects_dir / slug).mkdir(parents=True, exist_ok=True)

    @classmethod
    def prepare_selected_projects(
        cls, backup_root: Path, project_slugs: set[str]
    ) -> ConversationStoreRepository:
        """Create an isolated staging repository beside an existing process directory."""
        backup_root = backup_root.resolve(strict=True)
        process_root = backup_root / "process"
        if not backup_root.is_dir() or process_root.is_symlink() or not process_root.is_dir():
            raise ValueError("El backup debe contener una carpeta process normal.")
        if not isinstance(project_slugs, set) or any(
            not isinstance(slug, str) or slugify(slug, limit=80) != slug
            for slug in project_slugs
        ):
            raise ValueError("Los nombres de proyecto deben ser slugs válidos.")

        staging_root = Path(tempfile.mkdtemp(prefix=".process-staging-", dir=backup_root))
        if staging_root.resolve().parent != backup_root:
            raise ValueError("El área temporal debe estar dentro del backup.")
        repository = cls(staging_root)
        try:
            repository.projects_dir.mkdir(parents=True)
            repository.resources_dir.mkdir()
            for slug in project_slugs:
                project_dir = repository.projects_dir / slug
                if not project_dir.resolve().is_relative_to(repository.projects_dir):
                    raise ValueError("La carpeta del proyecto sale del área temporal.")
                project_dir.mkdir()
        except Exception:
            if staging_root.resolve().parent == backup_root:
                shutil.rmtree(staging_root)
            raise
        return repository

    @classmethod
    def prepare_project_package(
        cls, backup_root: Path, project_id: str | None
    ) -> ProjectPackageRepository:
        """Create an isolated temporary package for one stable project ID."""
        backup_root = backup_root.resolve(strict=True)
        if not backup_root.is_dir():
            raise ValueError("La raí­z del backup debe ser una carpeta.")
        if project_id is None:
            package_id = "unassigned"
        elif (
            not isinstance(project_id, str)
            or project_id == "unassigned"
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project_id)
        ):
            raise ValueError("El identificador del proyecto no es vÃ¡lido.")
        else:
            package_id = project_id

        staging_root = Path(tempfile.mkdtemp(prefix=".project-package-", dir=backup_root))
        try:
            staging_root = staging_root.resolve()
            if staging_root.parent != backup_root:
                raise ValueError("El Ã¡rea temporal debe estar dentro del backup.")
            package_root = staging_root / "process" / "projects" / package_id
            package_root = package_root.resolve()
            if not package_root.is_relative_to(staging_root):
                raise ValueError("La carpeta del paquete sale del Ã¡rea temporal.")
            manifest_path = package_root / "manifest.json"
            index_path = package_root / "index.json"
            conversations_dir = package_root / "conversations"
            resources_dir = package_root / "resources"
            for path in (manifest_path, index_path, conversations_dir, resources_dir):
                if not path.resolve().is_relative_to(package_root):
                    raise ValueError("La ruta del paquete sale del área temporal.")
            conversations_dir.mkdir(parents=True)
            resources_dir.mkdir()
            return ProjectPackageRepository(
                package_root=package_root,
                project_id=package_id,
                manifest_path=manifest_path,
                index_path=index_path,
                conversations_dir=conversations_dir,
                resources_dir=resources_dir,
            )
        except Exception:
            if staging_root.parent == backup_root:
                shutil.rmtree(staging_root, ignore_errors=True)
            raise

    @classmethod
    def install_project_package(cls, package_root: Path, backup_root: Path) -> Path:
        """Install a validated temporary project package without touching other projects."""
        backup_root = backup_root.resolve(strict=True)
        if not backup_root.is_dir() or backup_root.is_symlink():
            raise ValueError("La raÃ­z del backup debe ser una carpeta normal.")

        package_path = package_root.absolute()
        expected_projects_dir = package_path.parent
        expected_process_dir = expected_projects_dir.parent
        staging_root = expected_process_dir.parent
        if (
            package_path.is_symlink()
            or expected_projects_dir.is_symlink()
            or expected_process_dir.is_symlink()
            or staging_root.is_symlink()
            or staging_root.parent.resolve() != backup_root
            or not staging_root.name.startswith(".project-package-")
            or expected_projects_dir.name != "projects"
            or expected_process_dir.name != "process"
        ):
            raise ValueError("El paquete no procede de un Ã¡rea temporal vÃ¡lida.")
        try:
            package_path = package_path.resolve(strict=True)
        except OSError as error:
            raise ValueError("La carpeta temporal del paquete no existe.") from error
        if not package_path.is_dir() or not package_path.is_relative_to(staging_root.resolve()):
            raise ValueError("La carpeta temporal del paquete no es vÃ¡lida.")

        package_data = ProjectPackageReader(package_path).read()
        project_id = package_data["manifest"].get("project_id")
        if (
            not isinstance(project_id, str)
            or project_id != package_path.name
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project_id)
        ):
            raise ValueError("La identidad del paquete no es vÃ¡lida.")
        for path in package_path.rglob("*"):
            if path.is_symlink() or not path.resolve().is_relative_to(package_path):
                raise ValueError("El paquete contiene una ruta no permitida.")

        process_root = backup_root / "process"
        if process_root.is_symlink():
            raise ValueError("La carpeta process no puede ser un enlace simbÃ³lico.")
        if (process_root / "data").exists() or (process_root / "data").is_symlink():
            raise ValueError("No se puede instalar sobre la estructura process antigua.")
        projects_root = process_root / "projects"
        if projects_root.is_symlink():
            raise ValueError("La carpeta de proyectos no puede ser un enlace simbÃ³lico.")

        process_existed = process_root.exists()
        projects_existed = projects_root.exists()
        previous_package: Path | None = None
        destination = projects_root / project_id
        previous_moved = False
        package_moved = False
        try:
            projects_root.mkdir(parents=True, exist_ok=True)
            if not projects_root.resolve().is_relative_to(process_root.resolve()):
                raise ValueError("La carpeta de proyectos sale de process.")
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir():
                    raise ValueError("El paquete instalado existente no es una carpeta normal.")
                previous_package = Path(tempfile.mkdtemp(
                    prefix=f".{project_id}.previous-", dir=projects_root
                ))
                previous_package.rmdir()
                destination.replace(previous_package)
                previous_moved = True
            package_path.replace(destination)
            package_moved = True
            ProjectPackageReader(destination).read()
        except Exception as original_error:
            recovery_errors: list[str] = []
            if package_moved:
                if destination.is_symlink() or not destination.exists():
                    recovery_errors.append("no se encontrÃ³ el paquete nuevo para restaurar el staging")
                else:
                    try:
                        destination.replace(package_path)
                    except OSError as error:
                        recovery_errors.append(f"no se pudo devolver el paquete al staging: {error}")
            if package_moved and (
                package_path.is_symlink() or not package_path.is_dir()
            ):
                recovery_errors.append("no se confirmÃ³ la restauraciÃ³n del staging")
            if previous_moved:
                if destination.exists() or destination.is_symlink():
                    recovery_errors.append("no se puede restaurar el paquete anterior porque el destino estÃ¡ ocupado")
                elif previous_package is None or previous_package.is_symlink() or not previous_package.is_dir():
                    recovery_errors.append("no se encontrÃ³ el paquete anterior para restaurarlo")
                else:
                    try:
                        previous_package.replace(destination)
                    except OSError as error:
                        recovery_errors.append(f"no se pudo restaurar el paquete anterior: {error}")
                if destination.is_symlink() or not destination.is_dir():
                    recovery_errors.append("no se confirmÃ³ la restauraciÃ³n del paquete anterior")
            if not recovery_errors and not projects_existed and projects_root.exists() and not any(projects_root.iterdir()):
                try:
                    projects_root.rmdir()
                except OSError:
                    recovery_errors.append("no se pudo retirar la carpeta de proyectos creada durante la recuperaciÃ³n")
            if not recovery_errors and not process_existed and process_root.exists() and not any(process_root.iterdir()):
                try:
                    process_root.rmdir()
                except OSError:
                    recovery_errors.append("no se pudo retirar la carpeta process creada durante la recuperaciÃ³n")
            if recovery_errors:
                raise RuntimeError(
                    "La instalaciÃ³n fallÃ³ y no se pudo confirmar la recuperaciÃ³n: "
                    + "; ".join(recovery_errors)
                ) from original_error
            raise

        if previous_package is not None:
            try:
                shutil.rmtree(previous_package)
            except OSError as error:
                warnings.warn(
                    f"El paquete anterior no se pudo limpiar tras la instalaciÃ³n: {error}",
                    RuntimeWarning,
                )
        if staging_root.exists() and staging_root.parent == backup_root:
            try:
                shutil.rmtree(staging_root)
            except OSError as error:
                warnings.warn(
                    f"El staging no se pudo limpiar tras la instalaciÃ³n: {error}",
                    RuntimeWarning,
                )
        return destination

    @classmethod
    def build_project_catalog(cls, backup_root: Path) -> dict[str, Any]:
        """Build an in-memory catalog from installed project packages."""
        if backup_root.is_symlink():
            raise ValueError("La raÃ­z del backup no puede ser un enlace simbÃ³lico.")
        try:
            backup_root = backup_root.resolve(strict=True)
        except OSError as error:
            raise ValueError("La raÃ­z del backup no existe.") from error
        if not backup_root.is_dir():
            raise ValueError("La raÃ­z del backup debe ser una carpeta.")

        process_root = backup_root / "process"
        if process_root.is_symlink():
            raise ValueError("La carpeta process no puede ser un enlace simbÃ³lico.")
        if process_root.exists() and not process_root.is_dir():
            raise ValueError("La ruta process no es una carpeta.")
        if (process_root / "data").exists() or (process_root / "data").is_symlink():
            raise ValueError("No se puede catalogar la estructura process antigua.")
        projects_root = process_root / "projects"
        if projects_root.is_symlink():
            raise ValueError("La carpeta de proyectos no puede ser un enlace simbÃ³lico.")
        if not projects_root.exists():
            return {"schema_version": 1, "projects": []}
        if not projects_root.is_dir():
            raise ValueError("La ruta de proyectos no es una carpeta.")

        projects: list[dict[str, Any]] = []
        for package_root in sorted(projects_root.iterdir(), key=lambda path: path.name):
            if package_root.is_symlink() or not package_root.is_dir():
                raise ValueError("La carpeta de proyectos contiene una entrada no vÃ¡lida.")
            if re.fullmatch(r"\.[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.previous-.*", package_root.name):
                continue
            if not package_root.resolve().is_relative_to(projects_root.resolve()):
                raise ValueError("Un paquete sale de la carpeta de proyectos.")
            package_data = ProjectPackageReader(package_root).read()
            manifest = package_data["manifest"]
            project_id = manifest.get("project_id")
            if (
                not isinstance(project_id, str)
                or project_id != package_root.name
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project_id)
            ):
                raise ValueError("La identidad de un paquete no es vÃ¡lida.")
            for path in package_root.rglob("*"):
                if path.is_symlink() or not path.resolve().is_relative_to(package_root):
                    raise ValueError("Un paquete contiene una ruta no permitida.")

            project_name = manifest.get("project_name")
            processed_at = manifest.get("processed_at")
            statistics = {
                key: manifest.get(key)
                for key in (
                    "conversation_count",
                    "message_count",
                    "resource_count",
                    "materialized_resource_count",
                    "missing_resource_count",
                )
            }
            if (
                not isinstance(project_name, str)
                or not isinstance(processed_at, str)
                or any(isinstance(value, bool) or not isinstance(value, int) for value in statistics.values())
            ):
                raise ValueError("El manifiesto de un paquete no contiene los datos requeridos.")
            projects.append({
                "project_id": project_id,
                "project_name": project_name,
                "processed_at": processed_at,
                **statistics,
                "path": package_root.relative_to(process_root).as_posix(),
            })

        projects.sort(key=lambda project: project["project_id"])
        return {"schema_version": 1, "projects": projects}

    @classmethod
    def save_project_catalog(cls, backup_root: Path) -> Path:
        """Atomically save the in-memory project catalog without touching packages."""
        catalog = cls.build_project_catalog(backup_root)
        backup_root = backup_root.resolve(strict=True)
        process_root = backup_root / "process"
        if process_root.is_symlink():
            raise ValueError("La carpeta process no puede ser un enlace simbÃ³lico.")
        process_root.mkdir(parents=True, exist_ok=True)
        if not process_root.is_dir():
            raise ValueError("La ruta process no es una carpeta.")
        index_path = process_root / "index.json"
        if index_path.is_symlink():
            raise ValueError("El Ã­ndice global no puede ser un enlace simbÃ³lico.")

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".index-", suffix=".tmp", dir=process_root
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(catalog, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            if index_path.is_symlink():
                raise ValueError("El Ã­ndice global no puede ser un enlace simbÃ³lico.")
            temporary_path.replace(index_path)
        except Exception as original_error:
            if temporary_path is not None and temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError as cleanup_error:
                    raise RuntimeError(
                        "No se pudo eliminar el archivo temporal del catÃ¡logo."
                    ) from cleanup_error
            raise original_error
        return index_path

    def save_conversations(
        self,
        conversations: list[Any],
        progress: Callable[[int, int, str, str], None] | None = None,
    ) -> dict[tuple[str, str], str]:
        locations: dict[tuple[str, str], str] = {}
        total = len(conversations)
        for current, conversation in enumerate(conversations, start=1):
            slug = conversation.project.slug if conversation.project and conversation.project.slug else "unassigned"
            digest = sha1(conversation.conversation_id.encode("utf-8")).hexdigest()[:8]
            filename = f"{slugify(conversation.conversation_id, limit=60)}__{digest}.json"
            path = self.projects_dir / slug / filename
            if progress is not None:
                progress(current - 1, total, slug, filename)
            path.write_text(
                json.dumps(to_plain_data(conversation), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            locations[(slug, conversation.conversation_id)] = path.relative_to(self.output_dir).as_posix()
            if progress is not None:
                progress(current, total, slug, filename)
        return locations

    def save_manifest(self, manifest: StoreManifest) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(to_plain_data(manifest), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def save_index_payload(self, payload: dict[str, Any]) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def load_manifest_payload(self) -> dict[str, Any] | None:
        return self._load_json_file(self.manifest_path)

    def load_index_payload(self) -> dict[str, Any] | None:
        return self._load_json_file(self.index_path)

    def iter_conversation_payloads(self) -> Iterator[dict[str, Any]]:
        for path in sorted(self.projects_dir.glob("*/*.json")):
            payload = self._load_json_file(path)
            if payload is not None:
                yield payload

    @staticmethod
    def _load_json_file(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
