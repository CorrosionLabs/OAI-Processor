"""Read and validate an isolated project processing package."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Any


class ProjectPackageReadError(Exception):
    """The project package is missing or internally inconsistent."""


class ProjectPackageReader:
    def __init__(self, package_root: Path) -> None:
        self.package_root = package_root

    def read(self) -> dict[str, Any]:
        if self.package_root.is_symlink():
            raise ProjectPackageReadError("La carpeta del paquete no puede ser un enlace simbÃ³lico.")
        try:
            package_root = self.package_root.resolve(strict=True)
        except OSError as error:
            raise ProjectPackageReadError("La carpeta del paquete no existe.") from error
        if not package_root.is_dir():
            raise ProjectPackageReadError("La ruta del paquete no es una carpeta.")

        manifest = self._read_object(package_root / "manifest.json", "manifest.json")
        index = self._read_object(package_root / "index.json", "index.json")
        for key in ("projects", "conversations", "resources"):
            if not isinstance(index.get(key), list):
                raise ProjectPackageReadError(f"index.json debe contener una lista '{key}'.")

        conversations = [
            {
                "entry": entry,
                "path": self._resolve_file(
                    package_root,
                    entry,
                    "path",
                    "conversaciÃ³n",
                ),
            }
            for entry in index["conversations"]
            if self._validate_entry(entry, "conversaciÃ³n")
        ]
        resource_root = package_root / "resources"
        if resource_root.is_symlink():
            raise ProjectPackageReadError("La carpeta resources no puede ser un enlace simbÃ³lico.")
        resources = []
        for entry in index["resources"]:
            self._validate_entry(entry, "recurso")
            if "relative_resource_path" not in entry:
                raise ProjectPackageReadError(
                    "La entrada de recurso no contiene relative_resource_path."
                )
            relative_resource_path = entry["relative_resource_path"]
            resources.append({
                "entry": entry,
                "path": None if relative_resource_path is None else self._resolve_file(
                    resource_root,
                    entry,
                    "relative_resource_path",
                    "recurso",
                ),
            })
        return {
            "manifest": manifest,
            "index": index,
            "conversations": conversations,
            "resources": resources,
        }

    @staticmethod
    def _read_object(path: Path, label: str) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ProjectPackageReadError(f"Falta {label} en el paquete.") from error
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProjectPackageReadError(f"No se puede leer {label} como JSON.") from error
        if not isinstance(payload, dict):
            raise ProjectPackageReadError(f"{label} debe contener un objeto JSON.")
        return payload

    @staticmethod
    def _validate_entry(entry: Any, kind: str) -> bool:
        if not isinstance(entry, dict):
            raise ProjectPackageReadError(f"La entrada de {kind} no es un objeto JSON.")
        return True

    @staticmethod
    def _resolve_file(base: Path, entry: dict[str, Any], key: str, kind: str) -> Path:
        relative_path = entry.get(key)
        if not isinstance(relative_path, str) or not relative_path:
            raise ProjectPackageReadError(f"La entrada de {kind} no contiene una ruta '{key}' vÃ¡lida.")
        windows_path = PureWindowsPath(relative_path)
        if (
            Path(relative_path).is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or windows_path.root
        ):
            raise ProjectPackageReadError(f"La ruta de {kind} debe ser relativa: {relative_path}")
        base_root = base.resolve()
        resolved = (base_root / Path(relative_path)).resolve()
        if not resolved.is_relative_to(base_root):
            raise ProjectPackageReadError(f"La ruta de {kind} sale del paquete: {relative_path}")
        if not resolved.is_file():
            raise ProjectPackageReadError(f"La ruta de {kind} no es un archivo: {relative_path}")
        return resolved
