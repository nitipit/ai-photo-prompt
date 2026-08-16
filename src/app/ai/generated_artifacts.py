"""Server-owned staging and atomic publication for generated PNG artifacts.

The Pi provider may write only inside a fresh attempt workspace.  This module
keeps provider paths transient: publication derives both the filesystem path
and browser URL from the server-owned attempt identity.
"""

from __future__ import annotations

import errno
import os
import threading
import uuid
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from stat import S_ISDIR, S_ISLNK, S_ISREG
from typing import Any
from urllib.parse import urlsplit

from app.domain.models import ImageArtifact

from .protocols import GenerationAttempt

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PROVIDER_OUTPUT_PATH = Path("output") / "generated.png"
_DEFAULT_MAX_BYTES = 20 * 1024 * 1024
_DEFAULT_MAX_WIDTH = 4096
_DEFAULT_MAX_HEIGHT = 4096
_DEFAULT_RECONCILIATION_ENTRIES = 10_000
_INFLATE_CHUNK_BYTES = 64 * 1024


class ArtifactStoreError(ValueError):
    """Base error for invalid artifact-store configuration or provider data."""


class ArtifactSecurityError(ArtifactStoreError):
    """The provider path or owned path violated a filesystem safety boundary."""


class ArtifactValidationError(ArtifactStoreError):
    """The provider file was not a supported, bounded PNG."""


class ArtifactReconciliationLimitError(ArtifactStoreError):
    """Startup reconciliation exceeded its configured filesystem-entry bound."""


@dataclass(frozen=True, slots=True)
class ArtifactReconciliation:
    """Bounded startup cleanup counts returned by :meth:`reconcile`.

    ``inspected_entries`` counts every directory entry examined below either
    owned root. Removed counts include only entries that were still safe and
    removable when cleanup ran. ``retained_public_artifacts`` counts referenced
    canonical public PNGs, while ``skipped_unsafe_entries`` counts malformed,
    special, or symlink-replaced entries that were not followed.
    """

    inspected_entries: int
    removed_private_workspaces: int
    removed_public_artifacts: int
    retained_public_artifacts: int
    skipped_unsafe_entries: int


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

    def cleanup_workspace(self, attempt: GenerationAttempt) -> None:
        """Remove only this attempt's private staging workspace, idempotently."""

        with self._lock:
            round_id, token = _validated_attempt_ids(attempt)
            workspace = self.private_root / round_id / token
            if _cleanup_path_is_safe(workspace, self.private_root):
                _remove_owned_tree(workspace)
            if _cleanup_path_is_safe(workspace.parent, self.private_root):
                _remove_empty_owned_dir(workspace.parent, self.private_root)

    def discard(self, attempt: GenerationAttempt) -> None:
        """Remove this attempt's private workspace and public artifact.

        Symlink replacements and non-directory replacements are left untouched;
        cleanup never follows them. Empty round directories are removed only
        after the token-scoped entries have been handled. The operation is
        idempotent and derives both paths from the exact attempt token.
        """

        with self._lock:
            round_id, token = _validated_attempt_ids(attempt)
            self.cleanup_workspace(attempt)
            final_path = self.published_root / round_id / f"{token}.png"
            if _cleanup_path_is_safe(final_path, self.published_root):
                _remove_owned_file(final_path)
            if _cleanup_path_is_safe(final_path.parent, self.published_root):
                _remove_empty_owned_dir(final_path.parent, self.published_root)

    cleanup = discard

    def reconcile(
        self,
        referenced_urls: Iterable[str],
        *,
        max_entries: int = _DEFAULT_RECONCILIATION_ENTRIES,
    ) -> ArtifactReconciliation:
        """Remove bounded startup leftovers without following symlinks.

        Input is a finite iterable of durable artifact URL strings. URLs outside
        this store's public prefix are ignored; URLs under the prefix must have
        the exact derived ``<round UUID>/<attempt UUID>.png`` shape. At most
        ``max_entries`` input references and, separately, filesystem entries
        across both roots may be inspected. Exceeding either bound raises
        :class:`ArtifactReconciliationLimitError` before deleting anything.

        Every removable canonical private attempt workspace is cleaned. A
        canonical public token PNG is removed only when its exact derived URL is
        absent from the input. Scanning pins no-follow directory descriptors and
        removal uses names relative to those descriptors, so pathname ancestor
        swaps cannot redirect work outside the opened roots. Symlinks and special
        entries are never followed. The returned counts describe this invocation;
        callers own obtaining a bounded durable URL snapshot and deciding whether
        a limit error should fail startup. This primitive performs no database or
        FastAPI lifecycle work.
        """

        bound = _positive_int(max_entries, "max_entries")
        references = _validated_referenced_paths(
            referenced_urls,
            self.public_prefix,
            max_references=bound,
        )
        with self._lock:
            scan = _ReconciliationScan(bound)
            private_root_fd = None
            public_root_fd = None
            try:
                private_root_fd = _open_pinned_directory(self.private_root)
                public_root_fd = _open_pinned_directory(self.published_root)
                private_plans = _plan_private_reconciliation(private_root_fd, scan)
                public_plans, retained = _plan_public_reconciliation(
                    public_root_fd,
                    references,
                    scan,
                )

                removed_private = sum(
                    _remove_planned_workspace(private_root_fd, plan, scan) for plan in private_plans
                )
                removed_public = sum(
                    _remove_planned_public_artifact(public_root_fd, plan, scan)
                    for plan in public_plans
                )
            finally:
                _close_descriptor(private_root_fd)
                _close_descriptor(public_root_fd)

            return ArtifactReconciliation(
                inspected_entries=scan.inspected,
                removed_private_workspaces=removed_private,
                removed_public_artifacts=removed_public,
                retained_public_artifacts=retained,
                skipped_unsafe_entries=scan.skipped,
            )

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


class _ReconciliationScan:
    def __init__(self, max_entries: int) -> None:
        self.max_entries = max_entries
        self.inspected = 0
        self.skipped = 0

    def inspect(self) -> None:
        self.inspected += 1
        if self.inspected > self.max_entries:
            raise ArtifactReconciliationLimitError(
                "artifact reconciliation exceeds the filesystem-entry bound"
            )

    def skip(self) -> None:
        self.skipped += 1


def _validated_referenced_paths(
    referenced_urls: Iterable[str],
    public_prefix: str,
    *,
    max_references: int,
) -> set[tuple[str, str]]:
    if isinstance(referenced_urls, (str, bytes)):
        raise TypeError("referenced_urls must be an iterable of URL strings")
    references: set[tuple[str, str]] = set()
    try:
        values = iter(referenced_urls)
    except TypeError as error:
        raise TypeError("referenced_urls must be an iterable of URL strings") from error
    prefix = f"{public_prefix}/"
    for count, value in enumerate(values, start=1):
        if count > max_references:
            raise ArtifactReconciliationLimitError(
                "artifact reconciliation exceeds the durable-reference bound"
            )
        if type(value) is not str or not value:
            raise ValueError("referenced artifact URLs must be non-empty strings")
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("referenced artifact URLs must be local paths without query data")
        if not parsed.path.startswith(prefix):
            continue
        parts = parsed.path.removeprefix(prefix).split("/")
        if len(parts) != 2 or not parts[1].endswith(".png"):
            raise ValueError("referenced generated artifact URL has an invalid shape")
        round_id = _canonical_uuid(parts[0], "referenced round ID")
        token = _canonical_uuid(parts[1].removesuffix(".png"), "referenced attempt token")
        if parts != [round_id, f"{token}.png"]:
            raise ValueError("referenced generated artifact URL is not canonical")
        references.add((round_id, f"{token}.png"))
    return references


@dataclass(frozen=True, slots=True)
class _PlannedNode:
    name: str
    identity: tuple[int, int, int]
    children: tuple[_PlannedNode, ...] | None


@dataclass(frozen=True, slots=True)
class _PlannedWorkspace:
    round_name: str
    round_identity: tuple[int, int, int]
    workspace: _PlannedNode


@dataclass(frozen=True, slots=True)
class _PlannedPublicArtifact:
    round_name: str
    round_identity: tuple[int, int, int]
    artifact: _PlannedNode


def _plan_private_reconciliation(
    root_fd: int | None,
    scan: _ReconciliationScan,
) -> list[_PlannedWorkspace]:
    plans: list[_PlannedWorkspace] = []
    if root_fd is None:
        return plans
    with os.scandir(root_fd) as rounds:
        for round_entry in rounds:
            scan.inspect()
            round_state = _entry_state(root_fd, round_entry.name)
            if (
                round_state is None
                or not _is_canonical_uuid(round_entry.name)
                or not S_ISDIR(round_state.st_mode)
            ):
                scan.skip()
                continue
            round_identity = _entry_identity(round_state)
            round_fd = _open_matching_directory(root_fd, round_entry.name, round_identity)
            if round_fd is None:
                scan.skip()
                continue
            try:
                with os.scandir(round_fd) as attempts:
                    for attempt_entry in attempts:
                        scan.inspect()
                        attempt_state = _entry_state(round_fd, attempt_entry.name)
                        if (
                            attempt_state is None
                            or not _is_canonical_uuid(attempt_entry.name)
                            or not S_ISDIR(attempt_state.st_mode)
                        ):
                            scan.skip()
                            continue
                        attempt_identity = _entry_identity(attempt_state)
                        workspace_fd = _open_matching_directory(
                            round_fd,
                            attempt_entry.name,
                            attempt_identity,
                        )
                        if workspace_fd is None:
                            scan.skip()
                            continue
                        try:
                            descendants = _plan_workspace_descendants(workspace_fd, scan)
                        finally:
                            os.close(workspace_fd)
                        plans.append(
                            _PlannedWorkspace(
                                round_name=round_entry.name,
                                round_identity=round_identity,
                                workspace=_PlannedNode(
                                    name=attempt_entry.name,
                                    identity=attempt_identity,
                                    children=descendants,
                                ),
                            )
                        )
            finally:
                os.close(round_fd)
    return plans


def _plan_workspace_descendants(
    directory_fd: int,
    scan: _ReconciliationScan,
) -> tuple[_PlannedNode, ...]:
    descendants: list[_PlannedNode] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            scan.inspect()
            state = _entry_state(directory_fd, entry.name)
            if state is None:
                continue
            identity = _entry_identity(state)
            if S_ISDIR(state.st_mode):
                child_fd = _open_matching_directory(directory_fd, entry.name, identity)
                if child_fd is None:
                    scan.skip()
                    continue
                try:
                    children = _plan_workspace_descendants(child_fd, scan)
                finally:
                    os.close(child_fd)
                descendants.append(_PlannedNode(entry.name, identity, children))
            elif S_ISREG(state.st_mode):
                descendants.append(_PlannedNode(entry.name, identity, None))
            else:
                scan.skip()
    return tuple(descendants)


def _plan_public_reconciliation(
    root_fd: int | None,
    references: set[tuple[str, str]],
    scan: _ReconciliationScan,
) -> tuple[list[_PlannedPublicArtifact], int]:
    plans: list[_PlannedPublicArtifact] = []
    retained = 0
    if root_fd is None:
        return plans, retained
    with os.scandir(root_fd) as rounds:
        for round_entry in rounds:
            scan.inspect()
            round_state = _entry_state(root_fd, round_entry.name)
            if (
                round_state is None
                or not _is_canonical_uuid(round_entry.name)
                or not S_ISDIR(round_state.st_mode)
            ):
                scan.skip()
                continue
            round_identity = _entry_identity(round_state)
            round_fd = _open_matching_directory(root_fd, round_entry.name, round_identity)
            if round_fd is None:
                scan.skip()
                continue
            try:
                with os.scandir(round_fd) as artifacts:
                    for artifact_entry in artifacts:
                        scan.inspect()
                        state = _entry_state(round_fd, artifact_entry.name)
                        token_name = artifact_entry.name.removesuffix(".png")
                        canonical = (
                            state is not None
                            and artifact_entry.name.endswith(".png")
                            and _is_canonical_uuid(token_name)
                            and S_ISREG(state.st_mode)
                        )
                        if not canonical:
                            scan.skip()
                            continue
                        key = (round_entry.name, artifact_entry.name)
                        if key in references:
                            retained += 1
                        else:
                            assert state is not None
                            plans.append(
                                _PlannedPublicArtifact(
                                    round_name=round_entry.name,
                                    round_identity=round_identity,
                                    artifact=_PlannedNode(
                                        artifact_entry.name,
                                        _entry_identity(state),
                                        None,
                                    ),
                                )
                            )
            finally:
                os.close(round_fd)
    return plans, retained


def _open_pinned_directory(path: Path) -> int | None:
    """Open an absolute directory one no-follow component at a time."""

    if not path.is_absolute():
        raise ArtifactSecurityError(f"owned root must be absolute: {path}")
    flags = _directory_open_flags()
    descriptor: int | None = None
    try:
        descriptor = os.open(path.anchor, flags)
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as error:
        _close_descriptor(descriptor)
        if error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            return None
        raise


def _directory_open_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", None)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if directory is None or no_follow is None:
        raise ArtifactSecurityError("artifact reconciliation requires no-follow directory opens")
    return os.O_RDONLY | directory | no_follow | getattr(os, "O_CLOEXEC", 0)


def _open_matching_directory(
    parent_fd: int,
    name: str,
    identity: tuple[int, int, int],
) -> int | None:
    if not _entry_matches(parent_fd, name, identity, directory=True):
        return None
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as error:
        if error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            return None
        raise
    if _entry_identity(os.fstat(descriptor)) != identity:
        os.close(descriptor)
        return None
    return descriptor


def _entry_state(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _entry_identity(state: os.stat_result) -> tuple[int, int, int]:
    return state.st_dev, state.st_ino, state.st_mode


def _entry_matches(
    parent_fd: int,
    name: str,
    identity: tuple[int, int, int],
    *,
    directory: bool,
) -> bool:
    """Match a planned inode; the pinned parent FD, not this check, provides containment."""

    state = _entry_state(parent_fd, name)
    if state is None or _entry_identity(state) != identity:
        return False
    return S_ISDIR(state.st_mode) if directory else S_ISREG(state.st_mode)


def _remove_planned_workspace(
    root_fd: int | None,
    plan: _PlannedWorkspace,
    scan: _ReconciliationScan,
) -> int:
    if root_fd is None:
        return 0
    round_fd = _open_matching_directory(root_fd, plan.round_name, plan.round_identity)
    if round_fd is None:
        scan.skip()
        return 0
    try:
        workspace_fd = _open_matching_directory(
            round_fd,
            plan.workspace.name,
            plan.workspace.identity,
        )
        if workspace_fd is None:
            scan.skip()
            return 0
        try:
            assert plan.workspace.children is not None
            _remove_planned_descendants(workspace_fd, plan.workspace.children, scan)
        finally:
            os.close(workspace_fd)
        removed = _rmdir_planned(round_fd, plan.workspace)
        if not removed:
            scan.skip()
            return 0
    finally:
        os.close(round_fd)
    _rmdir_planned_name(root_fd, plan.round_name, plan.round_identity)
    return 1


def _remove_planned_descendants(
    parent_fd: int,
    nodes: tuple[_PlannedNode, ...],
    scan: _ReconciliationScan,
) -> None:
    for node in nodes:
        if node.children is None:
            if not _unlink_planned(parent_fd, node):
                scan.skip()
            continue
        child_fd = _open_matching_directory(parent_fd, node.name, node.identity)
        if child_fd is None:
            scan.skip()
            continue
        try:
            _remove_planned_descendants(child_fd, node.children, scan)
        finally:
            os.close(child_fd)
        if not _rmdir_planned(parent_fd, node):
            scan.skip()


def _remove_planned_public_artifact(
    root_fd: int | None,
    plan: _PlannedPublicArtifact,
    scan: _ReconciliationScan,
) -> int:
    if root_fd is None:
        return 0
    round_fd = _open_matching_directory(root_fd, plan.round_name, plan.round_identity)
    if round_fd is None:
        scan.skip()
        return 0
    try:
        if not _unlink_planned(round_fd, plan.artifact):
            scan.skip()
            return 0
    finally:
        os.close(round_fd)
    _rmdir_planned_name(root_fd, plan.round_name, plan.round_identity)
    return 1


def _unlink_planned(parent_fd: int, node: _PlannedNode) -> bool:
    if not _entry_matches(parent_fd, node.name, node.identity, directory=False):
        return _entry_state(parent_fd, node.name) is None
    try:
        os.unlink(node.name, dir_fd=parent_fd)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _rmdir_planned(parent_fd: int, node: _PlannedNode) -> bool:
    return _rmdir_planned_name(parent_fd, node.name, node.identity)


def _rmdir_planned_name(
    parent_fd: int,
    name: str,
    identity: tuple[int, int, int],
) -> bool:
    if not _entry_matches(parent_fd, name, identity, directory=True):
        return _entry_state(parent_fd, name) is None
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _close_descriptor(descriptor: int | None) -> None:
    if descriptor is not None:
        os.close(descriptor)


def _is_canonical_uuid(value: str) -> bool:
    try:
        return _canonical_uuid(value, "filesystem entry") == value
    except ValueError:
        return False


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
    bit_depth = color_type = -1
    idat_payloads: list[bytes] = []
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
            idat_payloads.append(payload)
        elif chunk_type == b"IEND":
            if len(payload) != 0 or not seen_idat or offset != len(data):
                raise ArtifactValidationError("PNG IEND is invalid or not final")
            if color_type == 3 and not seen_plte:
                raise ArtifactValidationError("indexed PNG is missing its palette")
            _validate_png_raster(
                idat_payloads,
                width=width,
                height=height,
                bit_depth=bit_depth,
                color_type=color_type,
            )
            return PNGMetadata(width=width, height=height, byte_size=len(data))
        elif chunk_type[0] < 97:
            raise ArtifactValidationError(f"unsupported critical PNG chunk: {chunk_type!r}")
        elif chunk_type in {b"acTL", b"fcTL", b"fdAT"}:
            raise ArtifactValidationError("animated PNG is unsupported")
        elif seen_idat:
            idat_closed = True

    raise ArtifactValidationError("PNG is missing final IEND")


def _validate_png_raster(
    idat_payloads: Iterable[bytes],
    *,
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
) -> None:
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    row_data_bytes = (width * channels * bit_depth + 7) // 8
    scanline_bytes = row_data_bytes + 1
    expected_decoded_bytes = height * scanline_bytes
    payloads = tuple(idat_payloads)
    decoder = zlib.decompressobj()
    decoded_bytes = 0

    try:
        for payload_index, payload in enumerate(payloads):
            if decoder.eof and payload:
                raise ArtifactValidationError("PNG zlib stream has trailing data")
            source = payload
            while source:
                remaining = expected_decoded_bytes + 1 - decoded_bytes
                if remaining <= 0:
                    raise ArtifactValidationError("PNG raster exceeds the decoded-byte bound")
                output = decoder.decompress(source, min(_INFLATE_CHUNK_BYTES, remaining))
                source = decoder.unconsumed_tail
                _validate_png_filters(output, decoded_bytes, scanline_bytes)
                decoded_bytes += len(output)
                if decoded_bytes > expected_decoded_bytes:
                    raise ArtifactValidationError("PNG raster exceeds the decoded-byte bound")
                if decoder.eof:
                    if decoder.unused_data or source or any(payloads[payload_index + 1 :]):
                        raise ArtifactValidationError("PNG zlib stream has trailing data")
                    break
                if not source:
                    break
            if decoder.eof:
                break
    except zlib.error as error:
        raise ArtifactValidationError("PNG zlib stream is invalid") from error

    if not decoder.eof:
        raise ArtifactValidationError("PNG zlib stream is truncated")
    if decoder.unused_data:
        raise ArtifactValidationError("PNG zlib stream has trailing data")
    if decoded_bytes != expected_decoded_bytes:
        raise ArtifactValidationError("PNG raster scanline length is invalid")


def _validate_png_filters(output: bytes, decoded_offset: int, scanline_bytes: int) -> None:
    first_filter = (-decoded_offset) % scanline_bytes
    for index in range(first_filter, len(output), scanline_bytes):
        if output[index] > 4:
            raise ArtifactValidationError("PNG scanline filter is invalid")


__all__ = [
    "ArtifactPublication",
    "ArtifactReconciliation",
    "ArtifactReconciliationLimitError",
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
