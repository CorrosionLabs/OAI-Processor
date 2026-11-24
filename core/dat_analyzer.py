from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any

try:
    import magic  # type: ignore
except ImportError:
    magic = None

try:
    import filetype  # type: ignore
except ImportError:
    filetype = None


@dataclass
class DatAnalysis:
    mime_type: str
    file_type: str
    probable_extension: str
    sha256: str
    detector_used: str


SIGNATURES: list[tuple[bytes, int, str, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", 0, "image/png", "PNG", ".png"),
    (b"\xff\xd8\xff", 0, "image/jpeg", "JPEG", ".jpg"),
    (b"GIF87a", 0, "image/gif", "GIF", ".gif"),
    (b"GIF89a", 0, "image/gif", "GIF", ".gif"),
    (b"%PDF-", 0, "application/pdf", "PDF", ".pdf"),
    (b"PK\x03\x04", 0, "application/zip", "ZIP", ".zip"),
    (b"PK\x05\x06", 0, "application/zip", "ZIP", ".zip"),
    (b"PK\x07\x08", 0, "application/zip", "ZIP", ".zip"),
    (b"ID3", 0, "audio/mpeg", "MP3", ".mp3"),
    (b"RIFF", 0, "audio/wav", "WAV", ".wav"),
    (b"OggS", 0, "audio/ogg", "OGG", ".ogg"),
    (b"ftyp", 4, "video/mp4", "MP4", ".mp4"),
    (b"SQLite format 3\x00", 0, "application/x-sqlite3", "SQLite", ".sqlite"),
    (b"Rar!\x1a\x07\x00", 0, "application/vnd.rar", "RAR", ".rar"),
    (b"Rar!\x1a\x07\x01\x00", 0, "application/vnd.rar", "RAR", ".rar"),
    (b"7z\xbc\xaf\x27\x1c", 0, "application/x-7z-compressed", "7Z", ".7z"),
]

TYPE_LABELS = {
    "application/json": "JSON",
    "application/octet-stream": "Desconocido",
    "application/pdf": "PDF",
    "application/vnd.rar": "RAR",
    "application/x-7z-compressed": "7Z",
    "application/x-rar-compressed": "RAR",
    "application/x-sqlite3": "SQLite",
    "application/zip": "ZIP",
    "audio/mpeg": "MP3",
    "audio/ogg": "OGG",
    "audio/wav": "WAV",
    "image/gif": "GIF",
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
    "text/plain": "Texto",
    "video/mp4": "MP4",
}


class HashCache:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self._data = self._load()
        self._dirty = False

    def get_sha256(self, file_path: Path) -> str:
        stat = file_path.stat()
        cache_key = str(file_path.resolve())
        cached = self._data.get(cache_key)
        if (
            isinstance(cached, dict)
            and cached.get("size_bytes") == stat.st_size
            and cached.get("modified_time_ns") == stat.st_mtime_ns
            and isinstance(cached.get("sha256"), str)
        ):
            return cached["sha256"]

        sha256 = self._calculate_sha256(file_path)
        self._data[cache_key] = {
            "size_bytes": stat.st_size,
            "modified_time_ns": stat.st_mtime_ns,
            "sha256": sha256,
        }
        self._dirty = True
        return sha256

    def save(self) -> None:
        if not self._dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self._dirty = False

    def _load(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _calculate_sha256(file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class DatAnalyzer:
    def __init__(self, hash_cache: HashCache) -> None:
        self.hash_cache = hash_cache

    def analyze(self, file_path: Path) -> DatAnalysis:
        file_head = self._read_head(file_path)
        sha256 = self.hash_cache.get_sha256(file_path)

        detected = (
            self._detect_with_python_magic(file_path)
            or self._detect_with_filetype(file_head)
            or self._detect_with_signatures(file_head)
            or self._detect_as_json(file_path, file_head)
            or self._detect_as_text(file_head)
            or self._detect_with_mimetypes(file_path)
            or ("application/octet-stream", "Desconocido", ".bin", "unknown")
        )

        mime_type, file_type, probable_extension, detector_used = detected
        return DatAnalysis(
            mime_type=mime_type,
            file_type=file_type,
            probable_extension=probable_extension,
            sha256=sha256,
            detector_used=detector_used,
        )

    @staticmethod
    def _read_head(file_path: Path, size: int = 8192) -> bytes:
        with file_path.open("rb") as handle:
            return handle.read(size)

    def _detect_with_python_magic(
        self,
        file_path: Path,
    ) -> tuple[str, str, str, str] | None:
        if magic is None:
            return None

        try:
            mime_type = magic.from_file(str(file_path), mime=True)
        except (OSError, AttributeError):
            return None

        if not mime_type:
            return None

        file_type = TYPE_LABELS.get(mime_type, self._label_from_mime(mime_type))
        probable_extension = mimetypes.guess_extension(mime_type) or self._extension_for_type(
            file_type
        )
        return mime_type, file_type, probable_extension, "python-magic"

    def _detect_with_filetype(self, file_head: bytes) -> tuple[str, str, str, str] | None:
        if filetype is None:
            return None

        kind = filetype.guess(file_head)
        if kind is None:
            return None

        mime_type = kind.mime
        file_type = TYPE_LABELS.get(mime_type, kind.extension.upper())
        probable_extension = f".{kind.extension.lower()}"
        return mime_type, file_type, probable_extension, "filetype"

    @staticmethod
    def _detect_with_signatures(file_head: bytes) -> tuple[str, str, str, str] | None:
        for signature, offset, mime_type, file_type, extension in SIGNATURES:
            end = offset + len(signature)
            if file_head[offset:end] == signature:
                if file_type == "WAV" and file_head[8:12] != b"WAVE":
                    continue
                return mime_type, file_type, extension, "magic-number"
        return None

    def _detect_as_json(
        self,
        file_path: Path,
        file_head: bytes,
    ) -> tuple[str, str, str, str] | None:
        stripped = file_head.lstrip()
        if not stripped or stripped[:1] not in (b"{", b"["):
            return None

        try:
            json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

        return "application/json", "JSON", ".json", "json-parse"

    @staticmethod
    def _detect_as_text(file_head: bytes) -> tuple[str, str, str, str] | None:
        if not file_head:
            return "application/octet-stream", "Vacio", ".bin", "empty-file"

        try:
            decoded = file_head.decode("utf-8")
        except UnicodeDecodeError:
            return None

        sample = decoded[:512]
        if any(ord(char) < 9 or 13 < ord(char) < 32 for char in sample):
            return None

        return "text/plain", "Texto", ".txt", "text-heuristic"

    def _detect_with_mimetypes(self, file_path: Path) -> tuple[str, str, str, str] | None:
        mime_type, _ = mimetypes.guess_type(file_path.name)
        if not mime_type:
            return None

        file_type = TYPE_LABELS.get(mime_type, self._label_from_mime(mime_type))
        probable_extension = mimetypes.guess_extension(mime_type) or self._extension_for_type(
            file_type
        )
        return mime_type, file_type, probable_extension, "mimetypes"

    @staticmethod
    def _label_from_mime(mime_type: str) -> str:
        subtype = mime_type.split("/")[-1]
        cleaned = subtype.replace("x-", "").replace("vnd.", "").replace("-", " ")
        return cleaned.upper()

    @staticmethod
    def _extension_for_type(file_type: str) -> str:
        fallback = {
            "7z": ".7z",
            "desconocido": ".bin",
            "gif": ".gif",
            "jpeg": ".jpg",
            "json": ".json",
            "mp3": ".mp3",
            "mp4": ".mp4",
            "ogg": ".ogg",
            "pdf": ".pdf",
            "png": ".png",
            "rar": ".rar",
            "sqlite": ".sqlite",
            "texto": ".txt",
            "vacio": ".bin",
            "wav": ".wav",
            "webp": ".webp",
            "zip": ".zip",
        }
        return fallback.get(file_type.lower(), ".bin")
