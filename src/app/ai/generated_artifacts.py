"""Server-owned staging and atomic publication for generated PNG artifacts.

The Pi provider may write only inside a fresh attempt workspace.  This module
keeps provider paths transient: publication derives both the filesystem path
and browser URL from the server-owned attempt identity.
"""

from __future__ import annotations

import os
import threading
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from stat import S_ISDIR, S_ISLNK, S_ISREG
from typing import Any

from app.domain.models import ImageArtifact

from .protocols import GenerationAttempt

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PROVIDER_OUTPUT_PATH = Path("output") / "generated.png"
_DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_DEFAULT_MAX_WIDTH = 4096
_DEFAULT_MAX_HEIGHT = 4096


class ArtifactStoreError(ValueError):
    """Base error for invalid artifact-store configuration or provider data."""


class ArtifactSecurityError(ArtifactStoreError):
    """The provider path or owned path violated a filesystem safety boundary."""


class ArtifactValidationError(ArtifactStoreError):
    """The provider file was not a supported, bounded PNG."""


@dataclass(frozen=True, slots=True)
class PNGMetadata:
    """Validated metadata extracted from a supported PNG container."""

    width: int
    height: int
    byte_size: int


# A descriptive alias for callers that do not need to mention the file format.
ImageMetadata = PNGMetadata


@dataclass(frozen=True, slots=True)
class ArtifactWorkspace:
    """Server-owned workspace and fixed relative output path for one attempt."""

    workspace: Path
    relative_output_path: str
    staged_path: Path


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    """The persisted domain artifact plus its server-owned filesystem path."""

    artifact: ImageArtifact
    path: Path
    url: str

    @property
    def filesystem_path(self) -> Path:
        """Return the final path without exposing the provider's staging path."""

        return self.path

    @property
    def final_path(self) -> Path:
        """Backward-friendly name for the same server-owned final path."""

        return self.path


# The name describes the operation more directly at integration boundaries.
ArtifactPublication = PublishedArtifact


class GeneratedArtifactStore:
    """Stage one generation attempt and publish one validated PNG atomically.

    All methods are synchronous filesystem units.  Async callers should invoke
    them through ``asyncio.to_thread`` so no transaction or partial filesystem
    operation crosses an await boundary.
    """

    def __init__(
        self,
        private_root: Path | str,
        published_root: Path | str,
        *,
        public_prefix: str = "/generated/",
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_width: int = _DEFAULT_MAX_WIDTH,
        max_height: int = _DEFAULT_MAX_HEIGHT,
        max_dimensions: tuple[int, int] | None = None,
    ) -> None:
        self.private_root = _normalise_root(private_root, "private_root")
        self.published_root = _normalise_root(published_root, "published_root")
        _validate_distinct_roots(self.private_root, self.published_root)
        self.public_prefix = _normalise_public_prefix(public_prefix)
        if max_dimensions is not None:
            if max_width != _DEFAULT_MAX_WIDTH or max_height != _DEFAULT_MAX_HEIGHT:
                raise ValueError("use max_dimensions or max_width/max_height, not both")
            if type(max_dimensions) is not tuple or len(max_dimensions) != 2:
                raise ValueError("max_dimensions must be a (width, height) tuple")
            max_width, max_height = max_dimensions
        self.max_bytes = _positive_int(max_bytes, "max_bytes")
        self.max_width = _positive_int(max_width, "max_width")
        self.max_height = _positive_int(max_height, "max_height")
        self.max_dimensions = (self.max_width, self.max_height)
        self._lock = threading.RLock()

    def workspace_for(self, attempt: GenerationAttempt) -> Path:
        """Return the server-derived private workspace for ``attempt``."""

        round_id, token = _validated_attempt_ids(attempt)
        return self.private_root / round_id / token

    # Explicit aliases make the staging boundary easy to discover at call sites.
    attempt_workspace = workspace_for

    def provider_destination(self, attempt: GenerationAttempt) -> Path:
        """Return the fixed relative destination exposed to the provider."""

        return self.workspace_for(attempt) / PROVIDER_OUTPUT_PATH

    def prepare(self, attempt: GenerationAttempt) -> Path:
        """Create a fresh workspace and return its fixed provider destination.

        An existing token directory is never reused, even when it is empty.
        """

        with self._lock:
            workspace = self.workspace_for(attempt)
            _ensure_directory_tree(self.private_root)
            round_dir = self.private_root / workspace.relative_to(self.private_root).parts[0]
            _ensure_child_directory(round_dir)
            _refuse_existing(workspace, "attempt workspace")
            workspace.mkdir()
            _assert_directory(workspace, "attempt workspace")
            output_dir = workspace / PROVIDER_OUTPUT_PATH.parent
            output_dir.mkdir()
            _assert_directory(output_dir, "provider output directory")
            return output_dir / PROVIDER_OUTPUT_PATH.name

    prepare_attempt = prepare

    def prepare_workspace(self, attempt: GenerationAttempt) -> ArtifactWorkspace:
        """Prepare and describe the fixed provider destination for a pipeline."""

        staged_path = self.prepare(attempt)
        workspace = self.workspace_for(attempt)
        return ArtifactWorkspace(
            workspace=workspace,
            relative_output_path=PROVIDER_OUTPUT_PATH.as_posix(),
            staged_path=staged_path,
        )

    def publish(
        self,
        attempt: GenerationAttempt,
        provider_path: str | os.PathLike[str],
    ) -> PublishedArtifact:
        """Validate and atomically publish a provider-returned relative PNG path."""

        with self._lock:
            workspace = self.workspace_for(attempt)
            round_id, token = _validated_attempt_ids(attempt)
            self._validate_workspace(workspace)
            relative_path = _validated_provider_relative_path(provider_path)
            source = workspace / relative_path
            data, metadata = self._read_provider_png(source, workspace)

            _ensure_directory_tree(self.published_root)
            published_round = self.published_root / round_id
            _ensure_child_directory(published_round)
            final_path = published_round / f"{token}.png"
            _refuse_existing(final_path, "published artifact")

            temporary = published_round / f".{token}.{uuid.uuid4().hex}.tmp"
            try:
                _write_fsync_file(temporary, data)
                # Check again after the copy so a pre-existing final artifact is
                # never intentionally replaced.  The store lock serializes its
                # own publishers; os.replace keeps the final name atomic.
                _refuse_existing(final_path, "published artifact")
                os.replace(temporary, final_path)
                _fsync_directory(published_round)
            except BaseException:
                _remove_owned_file(temporary)
                raise

            url = f"{self.public_prefix}/{round_id}/{token}.png"
            artifact = ImageArtifact(
                url=url,
                mime_type="image/png",
                provider="codex-imagegen",
                width=metadata.width,
                height=metadata.height,
            )
            return PublishedArtifact(artifact=artifact, path=final_path, url=url)

    def resolve_public(self, attempt: GenerationAttempt) -> Path:
        """Safely resolve an existing owned public artifact path."""

        with self._lock:
            final_path = self._published_path(attempt)
            _assert_contained_existing(final_path, self.published_root, "published artifact")
            _assert_regular_nonsymlink(final_path, "published artifact")
            return final_path

    resolve_published = resolve_public

    def read_public(self, attempt: GenerationAttempt) -> bytes:
        """Read and revalidate an owned public PNG without following symlinks."""

        with self._lock:
            final_path = self._published_path(attempt)
            data, _ = self._read_png_file(final_path, self.published_root)
            return data

    read_published = read_public

    def read(self, published: PublishedArtifact) -> bytes:
        """Read a publication returned by this store after containment checks."""

        if not isinstance(published, PublishedArtifact):
            raise TypeError("published must be a PublishedArtifact")
        with self._lock:
            data, _ = self._read_png_file(published.final_path, self.published_root)
            return data

    def discard(self, attempt: GenerationAttempt) -> None:
        """Remove only this attempt's workspace and published artifact.

        Symlink replacements and non-directory replacements are left untouched;
        cleanup never follows them.  Empty round directories are removed only
        after the token-scoped entries have been handled.
        """

        with self._lock:
            round_id, token = _validated_attempt_ids(attempt)
            workspace = self.private_root / round_id / token
            final_path = self.published_root / round_id / f"{token}.png"
            if _cleanup_path_is_safe(workspace, self.private_root):
                _remove_owned_tree(workspace)
            if _cleanup_path_is_safe(final_path, self.published_root):
                _remove_owned_file(final_path)
            if _cleanup_path_is_safe(workspace.parent, self.private_root):
                _remove_empty_owned_dir(workspace.parent, self.private_root)
            if _cleanup_path_is_safe(final_path.parent, self.published_root):
                _remove_empty_owned_dir(final_path.parent, self.published_root)

    cleanup = discard

    def _published_path(self, attempt: GenerationAttempt) -> Path:
        round_id, token = _validated_attempt_ids(attempt)
        return self.published_root / round_id / f"{token}.png"

    def _validate_workspace(self, workspace: Path) -> None:
        _assert_contained_existing(workspace, self.private_root, "attempt workspace")
        _assert_directory(workspace, "attempt workspace")

    def _read_provider_png(
        self,
        source: Path,
        workspace: Path,
    ) -> tuple[bytes, PNGMetadata]:
        _assert_contained_existing(source, workspace, "provider artifact")
        return self._read_png_file(source, workspace)

    def _read_png_file(self, source: Path, containment_root: Path) -> tuple[bytes, PNGMetadata]:
        _assert_contained_existing(source, containment_root, "PNG artifact")
        _assert_regular_nonsymlink(source, "PNG artifact")
        expected = os.lstat(source)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as error:
            raise ArtifactSecurityError(f"could not open PNG artifact safely: {source}") from error
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_mode) != (
                expected.st_dev,
                expected.st_ino,
                expected.st_mode,
            ):
                raise ArtifactSecurityError("PNG artifact changed while it was being opened")
            if not S_ISREG(opened.st_mode):
                raise ArtifactValidationError("PNG artifact is not a regular file")
            if opened.st_size <= 0:
                raise ArtifactValidationError("PNG artifact is empty")
            if opened.st_size > self.max_bytes:
                raise ArtifactValidationError("PNG artifact exceeds the byte bound")
            content = bytearray()
            while True:
                remaining = self.max_bytes + 1 - len(content)
                if remaining <= 0:
                    raise ArtifactValidationError("PNG artifact exceeds the byte bound")
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > self.max_bytes:
                    raise ArtifactValidationError("PNG artifact exceeds the byte bound")
        except ArtifactStoreError:
            raise
        except OSError as error:
            raise ArtifactValidationError("could not read PNG artifact") from error
        finally:
            os.close(descriptor)
        data = bytes(content)
        return data, _parse_png(data, self.max_bytes, self.max_width, self.max_height)


def _normalise_root(value: Path | str, name: str) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as error:
        raise TypeError(f"{name} must be a filesystem path") from error
    if isinstance(raw, bytes):
        raise TypeError(f"{name} must be a text filesystem path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if S_ISLNK(existing.st_mode):
            raise ArtifactSecurityError(f"{name} must not be a symlink")
        if not S_ISDIR(existing.st_mode):
            raise ArtifactStoreError(f"{name} must be a directory")
    return path.resolve(strict=False)


def _validate_distinct_roots(private_root: Path, published_root: Path) -> None:
    if private_root == published_root:
        raise ValueError("private_root and published_root must be different directories")
    if private_root.is_relative_to(published_root) or published_root.is_relative_to(private_root):
        raise ValueError("private_root and published_root must not contain one another")


def _normalise_public_prefix(value: str) -> str:
    if type(value) is not str or not value:
        raise ValueError("public_prefix must be a non-empty URL path")
    if not value.startswith("/") or value.startswith("//"):
        raise ValueError("public_prefix must be an absolute URL path")
    if "\\" in value or "\x00" in value or "?" in value or "#" in value:
        raise ValueError("public_prefix contains an unsafe URL character")
    parts = [part for part in value.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("public_prefix must contain a URL path segment")
    return "/" + "/".join(parts)


def _positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _validated_attempt_ids(attempt: GenerationAttempt) -> tuple[str, str]:
    if not isinstance(attempt, GenerationAttempt):
        raise TypeError("attempt must be a GenerationAttempt")
    return (
        _canonical_uuid(attempt.round_id, "round_id"),
        _canonical_uuid(attempt.attempt_token, "attempt_token"),
    )


def _canonical_uuid(value: str, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{name} must be a UUID string") from error
    if parsed.version not in {1, 3, 4, 5}:
        raise ValueError(f"{name} must be a versioned UUID")
    return str(parsed)


def _ensure_directory_tree(root: Path) -> None:
    try:
        state = os.lstat(root)
    except FileNotFoundError:
        root.mkdir(parents=True, exist_ok=True)
        state = os.lstat(root)
    if S_ISLNK(state.st_mode) or not S_ISDIR(state.st_mode):
        raise ArtifactSecurityError(f"owned root is not a directory: {root}")


def _ensure_child_directory(path: Path) -> None:
    try:
        state = os.lstat(path)
    except FileNotFoundError:
        path.mkdir()
        state = os.lstat(path)
    if S_ISLNK(state.st_mode) or not S_ISDIR(state.st_mode):
        raise ArtifactSecurityError(f"owned directory is not a safe directory: {path}")


def _assert_directory(path: Path, label: str) -> None:
    try:
        state = os.lstat(path)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"{label} does not exist: {path}") from error
    if S_ISLNK(state.st_mode) or not S_ISDIR(state.st_mode):
        raise ArtifactSecurityError(f"{label} is not a safe directory: {path}")


def _refuse_existing(path: Path, label: str) -> None:
    try:
        state = os.lstat(path)
    except FileNotFoundError:
        return
    if S_ISLNK(state.st_mode):
        raise ArtifactSecurityError(f"{label} is a symlink: {path}")
    raise FileExistsError(f"{label} already exists: {path}")


def _validated_provider_relative_path(value: str | os.PathLike[str]) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as error:
        raise TypeError("provider path must be a relative text path") from error
    if isinstance(raw, bytes) or not raw:
        raise ValueError("provider path must be a non-empty relative path")
    if "\x00" in raw or "\\" in raw:
        raise ArtifactSecurityError("provider path contains an unsafe character")
    windows = PureWindowsPath(raw)
    if windows.is_absolute() or windows.drive or windows.root:
        raise ArtifactSecurityError("provider path must be relative")
    if any(part in {".", ".."} for part in raw.split("/")):
        raise ArtifactSecurityError("provider path contains traversal")
    path = Path(raw)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise ArtifactSecurityError("provider path contains traversal")
    return path


def _assert_contained_existing(path: Path, root: Path, label: str) -> None:
    _assert_no_symlink_components(path, root, label)
    try:
        path.relative_to(root)
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        raise ArtifactSecurityError(f"{label} is outside its owned root: {path}") from error


def _assert_no_symlink_components(path: Path, root: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ArtifactSecurityError(f"{label} is outside its owned root: {path}") from error
    current = root
    _assert_not_symlink(current, label)
    for part in relative.parts:
        current /= part
        try:
            _assert_not_symlink(current, label)
        except FileNotFoundError:
            raise


def _assert_not_symlink(path: Path, label: str) -> None:
    state = os.lstat(path)
    if S_ISLNK(state.st_mode):
        raise ArtifactSecurityError(f"{label} contains a symlink: {path}")


def _assert_regular_nonsymlink(path: Path, label: str) -> None:
    state = os.lstat(path)
    if S_ISLNK(state.st_mode) or not S_ISREG(state.st_mode):
        raise ArtifactValidationError(f"{label} is not a regular non-symlink file")


def _write_fsync_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _cleanup_path_is_safe(path: Path, root: Path) -> bool:
    """Return false when cleanup would have to traverse a symlink."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    try:
        if S_ISLNK(os.lstat(current).st_mode):
            return False
    except FileNotFoundError:
        return True
    for part in relative.parts:
        current /= part
        try:
            state = os.lstat(current)
        except FileNotFoundError:
            return True
        if S_ISLNK(state.st_mode):
            return False
    return True


def _remove_owned_file(path: Path) -> None:
    try:
        state = os.lstat(path)
    except FileNotFoundError:
        return
    if S_ISLNK(state.st_mode) or not S_ISREG(state.st_mode):
        return
    path.unlink()


def _remove_owned_tree(path: Path) -> None:
    try:
        state = os.lstat(path)
    except FileNotFoundError:
        return
    if S_ISLNK(state.st_mode) or not S_ISDIR(state.st_mode):
        return
    for entry in os.scandir(path):
        child = Path(entry.path)
        child_state = os.lstat(child)
        if S_ISLNK(child_state.st_mode):
            continue
        if S_ISDIR(child_state.st_mode):
            _remove_owned_tree(child)
            try:
                child.rmdir()
            except OSError:
                pass
        elif S_ISREG(child_state.st_mode):
            child.unlink()
    try:
        path.rmdir()
    except OSError:
        pass


def _remove_empty_owned_dir(path: Path, root: Path) -> None:
    if path == root:
        return
    try:
        relative = path.relative_to(root)
    except ValueError:
        return
    if len(relative.parts) != 1:
        return
    try:
        state = os.lstat(path)
    except FileNotFoundError:
        return
    if S_ISLNK(state.st_mode) or not S_ISDIR(state.st_mode):
        return
    try:
        path.rmdir()
    except OSError:
        pass


def _parse_png(data: bytes, max_bytes: int, max_width: int, max_height: int) -> PNGMetadata:
    if len(data) > max_bytes:
        raise ArtifactValidationError("PNG artifact exceeds the byte bound")
    if len(data) < len(PNG_SIGNATURE) + 12 or data[:8] != PNG_SIGNATURE:
        raise ArtifactValidationError("PNG signature is invalid")

    offset = len(PNG_SIGNATURE)
    seen_ihdr = False
    seen_plte = False
    seen_idat = False
    idat_closed = False
    width = height = 0
    color_type = -1
    while offset < len(data):
        if len(data) - offset < 12:
            raise ArtifactValidationError("PNG chunk is truncated")
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        if not all(65 <= value <= 122 and not (90 < value < 97) for value in chunk_type):
            raise ArtifactValidationError("PNG chunk type is invalid")
        payload_start = offset + 8
        payload_end = payload_start + length
        crc_end = payload_end + 4
        if crc_end > len(data):
            raise ArtifactValidationError("PNG chunk payload is truncated")
        payload = data[payload_start:payload_end]
        expected_crc = int.from_bytes(data[payload_end:crc_end], "big")
        actual_crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise ArtifactValidationError("PNG chunk CRC is invalid")
        offset = crc_end

        if chunk_type == b"IHDR":
            if seen_ihdr or payload_start != len(PNG_SIGNATURE) + 8 or len(payload) != 13:
                raise ArtifactValidationError("PNG IHDR is invalid or out of order")
            seen_ihdr = True
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            bit_depth = payload[8]
            color_type = payload[9]
            if width <= 0 or height <= 0:
                raise ArtifactValidationError("PNG dimensions must be positive")
            if width > max_width or height > max_height:
                raise ArtifactValidationError("PNG dimensions exceed the configured bound")
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if color_type not in valid_depths or bit_depth not in valid_depths[color_type]:
                raise ArtifactValidationError("PNG color type and bit depth are unsupported")
            if payload[10:] != b"\x00\x00\x00":
                raise ArtifactValidationError(
                    "PNG compression, filter, or interlace is unsupported"
                )
        elif not seen_ihdr:
            raise ArtifactValidationError("PNG must begin with IHDR")
        elif chunk_type == b"PLTE":
            if seen_plte or seen_idat or len(payload) == 0 or len(payload) % 3:
                raise ArtifactValidationError("PNG palette is invalid or out of order")
            seen_plte = True
        elif chunk_type == b"IDAT":
            if idat_closed:
                raise ArtifactValidationError("PNG IDAT chunks are not consecutive")
            seen_idat = True
        elif chunk_type == b"IEND":
            if len(payload) != 0 or not seen_idat or offset != len(data):
                raise ArtifactValidationError("PNG IEND is invalid or not final")
            if color_type == 3 and not seen_plte:
                raise ArtifactValidationError("indexed PNG is missing its palette")
            return PNGMetadata(width=width, height=height, byte_size=len(data))
        elif chunk_type[0] < 97:
            raise ArtifactValidationError(f"unsupported critical PNG chunk: {chunk_type!r}")
        elif chunk_type in {b"acTL", b"fcTL", b"fdAT"}:
            raise ArtifactValidationError("animated PNG is unsupported")
        elif seen_idat:
            idat_closed = True

    raise ArtifactValidationError("PNG is missing final IEND")


__all__ = [
    "ArtifactPublication",
    "ArtifactWorkspace",
    "ArtifactSecurityError",
    "ArtifactStoreError",
    "ArtifactValidationError",
    "GeneratedArtifactStore",
    "GenerationAttempt",
    "ImageArtifact",
    "ImageMetadata",
    "PNGMetadata",
    "PNG_SIGNATURE",
    "PROVIDER_OUTPUT_PATH",
    "PublishedArtifact",
]
