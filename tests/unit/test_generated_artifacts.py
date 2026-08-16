from __future__ import annotations

import errno
import os
import struct
import zlib
from pathlib import Path
from uuid import uuid4

import pytest

import app.ai.generated_artifacts as artifact_module
from app.ai.generated_artifacts import (
    PROVIDER_OUTPUT_PATH,
    ArtifactReconciliationLimitError,
    ArtifactSecurityError,
    ArtifactValidationError,
    GeneratedArtifactStore,
    GenerationAttempt,
)


def attempt() -> GenerationAttempt:
    return GenerationAttempt(round_id=str(uuid4()), attempt_token=str(uuid4()))


def chunk(name: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + name
        + payload
        + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)
    )


def png_with_idat(
    idat_payloads: list[bytes],
    width: int = 1,
    height: int = 1,
    *,
    ihdr_tail: bytes = b"\x08\x06\x00\x00\x00",
) -> bytes:
    ihdr = struct.pack(">II", width, height) + ihdr_tail
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + b"".join(chunk(b"IDAT", payload) for payload in idat_payloads)
        + chunk(b"IEND", b"")
    )


def png(width: int = 1, height: int = 1, *, ihdr_tail: bytes = b"\x08\x06\x00\x00\x00") -> bytes:
    bit_depth = ihdr_tail[0]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ihdr_tail[1]]
    row_bytes = (width * channels * bit_depth + 7) // 8
    raster = b"".join(b"\x00" + bytes(row_bytes) for _ in range(height))
    return png_with_idat([zlib.compress(raster)], width, height, ihdr_tail=ihdr_tail)


def prepared_store(
    tmp_path: Path, **kwargs: object
) -> tuple[GeneratedArtifactStore, GenerationAttempt, Path]:
    store = GeneratedArtifactStore(tmp_path / "private", tmp_path / "published", **kwargs)
    current = attempt()
    destination = store.prepare(current)
    return store, current, destination


def test_prepare_uses_server_owned_uuid_workspace_and_fixed_destination(tmp_path: Path) -> None:
    store, current, destination = prepared_store(tmp_path)

    assert destination == (
        tmp_path / "private" / current.round_id / current.attempt_token / PROVIDER_OUTPUT_PATH
    )
    assert destination.parent.is_dir()
    assert not (tmp_path / "private" / "player-name").exists()

    with pytest.raises(FileExistsError):
        store.prepare(current)


def test_publish_validates_png_and_returns_domain_artifact_without_provider_path(
    tmp_path: Path,
) -> None:
    store, current, destination = prepared_store(tmp_path, max_width=8, max_height=8)
    data = png(3, 4)
    destination.write_bytes(data)

    published = store.publish(current, "output/generated.png")

    expected_path = tmp_path / "published" / current.round_id / f"{current.attempt_token}.png"
    assert published.path == expected_path
    assert published.filesystem_path == expected_path
    assert published.url == f"/generated/{current.round_id}/{current.attempt_token}.png"
    assert published.artifact.url == published.url
    assert published.artifact.mime_type == "image/png"
    assert published.artifact.provider == "codex-imagegen"
    assert published.artifact.width == 3
    assert published.artifact.height == 4
    assert published.path.read_bytes() == data
    assert "generated.png" not in published.artifact.dict()


def test_publish_refuses_final_overwrite_and_read_helper_is_owned(tmp_path: Path) -> None:
    store, current, destination = prepared_store(tmp_path)
    destination.write_bytes(png())
    first = store.publish(current, PROVIDER_OUTPUT_PATH)

    destination.write_bytes(png(2, 2))
    with pytest.raises(FileExistsError):
        store.publish(current, PROVIDER_OUTPUT_PATH)
    assert store.resolve_public(current) == first.path
    assert store.read_public(current) == first.path.read_bytes()


@pytest.mark.parametrize(
    "provider_path",
    [
        "../outside.png",
        "output/../../outside.png",
        "/tmp/outside.png",
        "C:/outside.png",
        "output\\generated.png",
        "./output/generated.png",
    ],
)
def test_provider_path_must_be_relative_without_traversal(
    tmp_path: Path, provider_path: str
) -> None:
    store, current, destination = prepared_store(tmp_path)
    destination.write_bytes(png())

    with pytest.raises((ArtifactSecurityError, ValueError)):
        store.publish(current, provider_path)


def test_resolved_containment_rejects_provider_symlink(tmp_path: Path) -> None:
    store, current, destination = prepared_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "generated.png"
    sentinel.write_bytes(png())
    destination.write_bytes(png())
    destination.unlink()
    destination.parent.rmdir()
    destination.parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactSecurityError):
        store.publish(current, PROVIDER_OUTPUT_PATH)
    assert sentinel.read_bytes() == png()
    assert not (tmp_path / "published").exists()


@pytest.mark.parametrize(
    "kind",
    ["empty", "directory", "not-png", "truncated", "bad-crc", "invalid-ihdr"],
)
def test_invalid_provider_files_are_rejected(tmp_path: Path, kind: str) -> None:
    store, current, destination = prepared_store(tmp_path)
    if kind == "empty":
        destination.write_bytes(b"")
    elif kind == "directory":
        destination.write_bytes(png())
        destination.unlink()
        destination.mkdir()
    elif kind == "not-png":
        destination.write_bytes(b"not a png")
    elif kind == "truncated":
        destination.write_bytes(png()[:-3])
    elif kind == "bad-crc":
        data = bytearray(png())
        data[29] ^= 1
        destination.write_bytes(data)
    else:
        destination.write_bytes(png(0, 1))

    with pytest.raises((ArtifactValidationError, ValueError)):
        store.publish(current, PROVIDER_OUTPUT_PATH)
    assert not (tmp_path / "published").exists()


@pytest.mark.parametrize(
    ("kind", "compressed"),
    [
        ("corrupt-zlib", b"not-zlib"),
        ("truncated-deflate", zlib.compress(b"\x00\x00\x00\x00\x00")[:-1]),
        ("short-scanline", zlib.compress(b"\x00\x00\x00\x00")),
        ("long-scanline", zlib.compress(b"\x00\x00\x00\x00\x00\x00")),
        ("invalid-filter", zlib.compress(b"\x05\x00\x00\x00\x00")),
        ("oversized-inflate", zlib.compress(b"\x00" * 1_000_000)),
        (
            "trailing-zlib-stream",
            zlib.compress(b"\x00\x00\x00\x00\x00") + zlib.compress(b"extra"),
        ),
    ],
)
def test_compressed_raster_must_be_complete_exact_and_bounded(
    tmp_path: Path,
    kind: str,
    compressed: bytes,
) -> None:
    store, current, destination = prepared_store(tmp_path / kind)
    destination.write_bytes(png_with_idat([compressed]))

    with pytest.raises(ArtifactValidationError):
        store.publish(current, PROVIDER_OUTPUT_PATH)


def test_consecutive_idat_chunks_form_one_valid_zlib_stream(tmp_path: Path) -> None:
    compressed = zlib.compress(b"\x00\x00\x00\x00\x00")
    store, current, destination = prepared_store(tmp_path)
    destination.write_bytes(png_with_idat([compressed[:3], compressed[3:]]))

    published = store.publish(current, PROVIDER_OUTPUT_PATH)

    assert published.artifact.width == 1
    assert published.artifact.height == 1


def test_byte_and_dimension_bounds_are_enforced(tmp_path: Path) -> None:
    data = png(3, 1)
    store, current, destination = prepared_store(
        tmp_path / "bytes", max_bytes=len(data) - 1, max_width=8, max_height=8
    )
    destination.write_bytes(data)
    with pytest.raises(ArtifactValidationError):
        store.publish(current, PROVIDER_OUTPUT_PATH)

    store, current, destination = prepared_store(
        tmp_path / "dimensions", max_bytes=10_000, max_width=2, max_height=2
    )
    destination.write_bytes(png(3, 1))
    with pytest.raises(ArtifactValidationError):
        store.publish(current, PROVIDER_OUTPUT_PATH)


def test_workspace_cleanup_retains_public_artifact(tmp_path: Path) -> None:
    store, current, destination = prepared_store(tmp_path)
    destination.write_bytes(png())
    published = store.publish(current, PROVIDER_OUTPUT_PATH)

    store.cleanup_workspace(current)
    store.cleanup_workspace(current)

    assert not store.workspace_for(current).exists()
    assert published.final_path.exists()


def test_discard_only_removes_this_attempt_and_empty_owned_dirs(tmp_path: Path) -> None:
    store = GeneratedArtifactStore(tmp_path / "private", tmp_path / "published")
    first = attempt()
    second = GenerationAttempt(round_id=first.round_id, attempt_token=str(uuid4()))
    first_destination = store.prepare(first)
    second_destination = store.prepare(second)
    first_destination.write_bytes(png())
    second_destination.write_bytes(png(2, 2))
    store.publish(first, PROVIDER_OUTPUT_PATH)
    store.publish(second, PROVIDER_OUTPUT_PATH)

    store.discard(first)

    assert not store.workspace_for(first).exists()
    assert not (tmp_path / "published" / first.round_id / f"{first.attempt_token}.png").exists()
    assert second_destination.exists()
    assert store.resolve_public(second).exists()
    assert (tmp_path / "private" / first.round_id).is_dir()
    assert (tmp_path / "published" / first.round_id).is_dir()

    store.discard(second)
    assert not (tmp_path / "private" / first.round_id).exists()
    assert not (tmp_path / "published" / first.round_id).exists()


def test_discard_does_not_follow_replacement_symlinks(tmp_path: Path) -> None:
    store, current, destination = prepared_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    workspace = store.workspace_for(current)
    destination.parent.rmdir()
    workspace.rmdir()
    workspace.symlink_to(outside, target_is_directory=True)
    store.discard(current)

    assert workspace.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_reconcile_removes_private_and_only_unreferenced_public_tokens(
    tmp_path: Path,
) -> None:
    store = GeneratedArtifactStore(tmp_path / "private", tmp_path / "published")
    referenced = attempt()
    orphan = attempt()
    referenced_destination = store.prepare(referenced)
    orphan_destination = store.prepare(orphan)
    referenced_destination.write_bytes(png())
    orphan_destination.write_bytes(png())
    referenced_public = store.publish(referenced, PROVIDER_OUTPUT_PATH)
    orphan_public = store.publish(orphan, PROVIDER_OUTPUT_PATH)

    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    nested_link = store.workspace_for(orphan) / "outside-link"
    nested_link.symlink_to(outside)
    unsafe_token = attempt()
    unsafe_round = store.published_root / unsafe_token.round_id
    unsafe_round.mkdir(parents=True)
    unsafe_public = unsafe_round / f"{unsafe_token.attempt_token}.png"
    unsafe_public.symlink_to(outside)

    result = store.reconcile([referenced_public.url, "/assets/challenges/fake.webp"])

    assert result.removed_private_workspaces == 1
    assert result.removed_public_artifacts == 1
    assert result.retained_public_artifacts == 1
    assert result.skipped_unsafe_entries >= 1
    assert referenced_public.final_path.exists()
    assert not orphan_public.final_path.exists()
    assert unsafe_public.is_symlink()
    assert outside.read_bytes() == png()
    assert store.workspace_for(orphan).is_dir()
    assert nested_link.is_symlink()


def test_reconcile_preserves_noncanonical_private_quarantine_residue(tmp_path: Path) -> None:
    store, current, _ = prepared_store(tmp_path)
    quarantine = store.workspace_for(current) / (
        f"{artifact_module._QUARANTINE_PREFIX}{uuid4().hex}"
    )
    quarantine.write_bytes(b"preserve mismatched capture")

    first = store.reconcile([])
    second = store.reconcile([])

    assert first.removed_private_workspaces == 0
    assert second.removed_private_workspaces == 0
    assert first.skipped_unsafe_entries >= 1
    assert second.skipped_unsafe_entries >= 1
    assert quarantine.read_bytes() == b"preserve mismatched capture"


@pytest.mark.parametrize("replacement", ["workspace", "round-ancestor"])
def test_reconcile_revalidates_planned_paths_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    store, current, destination = prepared_store(tmp_path)
    destination.write_bytes(png())
    workspace = store.workspace_for(current)
    round_directory = workspace.parent
    sentinel_content = b"outside sentinel"
    original_public_plan = artifact_module._plan_public_reconciliation
    if replacement == "workspace":
        moved = tmp_path / "moved-workspace"
        outside = tmp_path / "outside-workspace"
        sentinel = outside / PROVIDER_OUTPUT_PATH
    else:
        moved = tmp_path / "moved-round"
        outside = tmp_path / "outside-round"
        sentinel = outside / current.attempt_token / PROVIDER_OUTPUT_PATH

    def replace_after_private_plan(root, references, scan):
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(sentinel_content)
        if replacement == "workspace":
            workspace.rename(moved)
            workspace.symlink_to(outside, target_is_directory=True)
        else:
            round_directory.rename(moved)
            round_directory.symlink_to(outside, target_is_directory=True)
        return original_public_plan(root, references, scan)

    monkeypatch.setattr(
        artifact_module,
        "_plan_public_reconciliation",
        replace_after_private_plan,
    )

    result = store.reconcile([])

    assert sentinel.read_bytes() == sentinel_content
    assert result.removed_private_workspaces == 0
    assert result.skipped_unsafe_entries >= 1
    if replacement == "workspace":
        assert workspace.is_symlink()
    else:
        assert round_directory.is_symlink()


def test_reconcile_limit_fails_before_mutation(tmp_path: Path) -> None:
    store, current, destination = prepared_store(tmp_path)
    destination.write_bytes(png())
    published = store.publish(current, PROVIDER_OUTPUT_PATH)

    with pytest.raises(ArtifactReconciliationLimitError):
        store.reconcile([], max_entries=1)

    assert store.workspace_for(current).exists()
    assert published.final_path.exists()


def test_reconcile_reference_limit_fails_before_filesystem_mutation(tmp_path: Path) -> None:
    store, current, destination = prepared_store(tmp_path)
    destination.write_bytes(png())
    published = store.publish(current, PROVIDER_OUTPUT_PATH)
    references = [
        f"/generated/{uuid4()}/{uuid4()}.png",
        f"/generated/{uuid4()}/{uuid4()}.png",
    ]

    with pytest.raises(ArtifactReconciliationLimitError, match="durable-reference bound"):
        store.reconcile(references, max_entries=1)

    assert store.workspace_for(current).exists()
    assert published.final_path.exists()


def test_reconcile_closes_private_root_when_public_root_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GeneratedArtifactStore(tmp_path / "private", tmp_path / "published")
    private_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    calls = 0

    def fail_second_root_open(path: Path) -> int:
        nonlocal calls
        del path
        calls += 1
        if calls == 1:
            return private_fd
        raise PermissionError("public root denied")

    try:
        monkeypatch.setattr(artifact_module, "_open_pinned_directory", fail_second_root_open)
        with pytest.raises(PermissionError, match="public root denied"):
            store.reconcile([])

        with pytest.raises(OSError) as closed:
            os.fstat(private_fd)
        assert closed.value.errno == errno.EBADF
    finally:
        try:
            os.close(private_fd)
        except OSError:
            pass


def test_reconcile_missing_or_symlink_roots_never_follows_them(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    public_root = tmp_path / "published"
    store = GeneratedArtifactStore(private_root, public_root)

    missing = store.reconcile([])

    assert missing.inspected_entries == 0
    assert not private_root.exists()
    assert not public_root.exists()

    outside_private = tmp_path / "outside-private"
    outside_public = tmp_path / "outside-public"
    outside_private.mkdir()
    outside_public.mkdir()
    private_sentinel = outside_private / "sentinel.txt"
    public_sentinel = outside_public / "sentinel.txt"
    private_sentinel.write_text("keep private", encoding="utf-8")
    public_sentinel.write_text("keep public", encoding="utf-8")
    private_root.symlink_to(outside_private, target_is_directory=True)
    public_root.symlink_to(outside_public, target_is_directory=True)

    replaced = store.reconcile([])

    assert replaced.inspected_entries == 0
    assert private_sentinel.read_text(encoding="utf-8") == "keep private"
    assert public_sentinel.read_text(encoding="utf-8") == "keep public"


def test_reconcile_postcheck_round_swap_cannot_redirect_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(20):
        iteration = tmp_path / str(index)
        store, current, destination = prepared_store(iteration)
        destination.write_bytes(png())
        published = store.publish(current, PROVIDER_OUTPUT_PATH)
        store.cleanup_workspace(current)
        round_directory = published.final_path.parent
        moved_round = iteration / "moved-round"
        outside_round = iteration / "outside-round"
        outside_round.mkdir()
        sentinel = outside_round / published.final_path.name
        sentinel.write_bytes(b"outside sentinel")
        original_entry_matches = artifact_module._entry_matches
        swapped = False

        def swap_after_artifact_check(
            parent_fd,
            name,
            identity,
            *,
            directory,
            original=original_entry_matches,
            artifact_name=published.final_path.name,
            owned_round=round_directory,
            moved=moved_round,
            outside=outside_round,
        ):
            nonlocal swapped
            matched = original(parent_fd, name, identity, directory=directory)
            if matched and not directory and name == artifact_name and not swapped:
                owned_round.rename(moved)
                owned_round.symlink_to(outside, target_is_directory=True)
                swapped = True
            return matched

        with monkeypatch.context() as patch:
            patch.setattr(artifact_module, "_entry_matches", swap_after_artifact_check)
            result = store.reconcile([])

        assert swapped is True
        assert result.removed_public_artifacts == 1
        assert sentinel.read_bytes() == b"outside sentinel"
        assert not (moved_round / published.final_path.name).exists()


def test_reconcile_atomic_quarantine_preserves_postmatch_public_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(20):
        iteration = tmp_path / str(index)
        store, current, destination = prepared_store(iteration)
        destination.write_bytes(png())
        published = store.publish(current, PROVIDER_OUTPUT_PATH)
        store.cleanup_workspace(current)
        planned_identity = artifact_module._entry_identity(
            os.stat(published.final_path, follow_symlinks=False)
        )
        replacement_source = iteration / "replacement-sentinel"
        replacement_source.write_bytes(b"replacement sentinel")
        replacement_identity = artifact_module._entry_identity(
            os.stat(replacement_source, follow_symlinks=False)
        )
        moved_round = iteration / "moved-round"
        original_rename = artifact_module._rename_noreplace
        planned_survivor_name = "planned-survivor.png"
        restore_conflict = index % 2 == 1
        swapped = False

        def substitute_at_atomic_capture(
            parent_fd: int,
            source: str,
            destination_name: str,
            *,
            artifact_name: str = published.final_path.name,
            moved: Path = moved_round,
            replacement: Path = replacement_source,
            survivor_name: str = planned_survivor_name,
            rename_noreplace=original_rename,
            conflict: bool = restore_conflict,
        ) -> None:
            nonlocal swapped
            if source == artifact_name and not swapped:
                pinned_round = Path(os.readlink(f"/proc/self/fd/{parent_fd}"))
                pinned_round.rename(moved)
                os.rename(
                    source,
                    survivor_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                os.rename(replacement, source, dst_dir_fd=parent_fd)
                swapped = True
                rename_noreplace(parent_fd, source, destination_name)
                if conflict:
                    descriptor = os.open(
                        source,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    try:
                        os.write(descriptor, b"canonical blocker")
                    finally:
                        os.close(descriptor)
                return
            rename_noreplace(parent_fd, source, destination_name)

        with monkeypatch.context() as patch:
            patch.setattr(
                artifact_module,
                "_rename_noreplace",
                substitute_at_atomic_capture,
            )
            result = store.reconcile([])

        assert swapped is True
        assert result.removed_public_artifacts == 0
        assert result.skipped_unsafe_entries >= 1
        assert (
            artifact_module._entry_identity(
                os.stat(moved_round / planned_survivor_name, follow_symlinks=False)
            )
            == planned_identity
        )
        quarantined = [
            entry
            for entry in moved_round.iterdir()
            if entry.name.startswith(artifact_module._QUARANTINE_PREFIX)
        ]
        if restore_conflict:
            assert (moved_round / published.final_path.name).read_bytes() == b"canonical blocker"
            assert len(quarantined) == 1
            assert (
                artifact_module._entry_identity(os.stat(quarantined[0], follow_symlinks=False))
                == replacement_identity
            )
        else:
            assert not quarantined
            restored = moved_round / published.final_path.name
            assert (
                artifact_module._entry_identity(os.stat(restored, follow_symlinks=False))
                == replacement_identity
            )
            assert restored.read_bytes() == b"replacement sentinel"


@pytest.mark.parametrize("substitution", ["leaf", "empty-directory", "workspace"])
def test_reconcile_atomic_quarantine_preserves_postmatch_private_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
) -> None:
    for index in range(20):
        iteration = tmp_path / substitution / str(index)
        store, current, destination = prepared_store(iteration)
        workspace = store.workspace_for(current)
        if substitution == "leaf":
            destination.write_bytes(b"planned leaf")
            planned = destination
        elif substitution == "empty-directory":
            planned = workspace / "planned-empty-directory"
            planned.mkdir()
        else:
            destination.write_bytes(b"planned workspace leaf")
            planned = workspace
        planned_identity = artifact_module._entry_identity(os.stat(planned, follow_symlinks=False))

        replacement_source = iteration / "replacement-sentinel"
        if substitution == "leaf":
            replacement_source.write_bytes(b"replacement sentinel")
        else:
            replacement_source.mkdir()
        replacement_identity = artifact_module._entry_identity(
            os.stat(replacement_source, follow_symlinks=False)
        )
        moved_container = iteration / "moved-pinned-directory"
        original_rename = artifact_module._rename_noreplace
        planned_survivor_name = "planned-survivor"
        swapped = False
        restored_parent: Path | None = None

        def substitute_at_atomic_capture(
            parent_fd: int,
            source: str,
            destination_name: str,
            *,
            target_name: str = planned.name,
            replacement: Path = replacement_source,
            moved: Path = moved_container,
            survivor_name: str = planned_survivor_name,
            rename_noreplace=original_rename,
        ) -> None:
            nonlocal restored_parent, swapped
            if source == target_name and not swapped:
                pinned_parent = Path(os.readlink(f"/proc/self/fd/{parent_fd}"))
                pinned_container = pinned_parent.parent if substitution == "leaf" else pinned_parent
                pinned_container.rename(moved)
                restored_parent = moved / "output" if substitution == "leaf" else moved
                os.rename(
                    source,
                    survivor_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                os.rename(replacement, source, dst_dir_fd=parent_fd)
                swapped = True
            rename_noreplace(parent_fd, source, destination_name)

        with monkeypatch.context() as patch:
            patch.setattr(
                artifact_module,
                "_rename_noreplace",
                substitute_at_atomic_capture,
            )
            result = store.reconcile([])

        assert swapped is True
        assert restored_parent is not None
        assert result.removed_private_workspaces == 0
        assert result.skipped_unsafe_entries >= 1
        planned_survivor = restored_parent / planned_survivor_name
        replacement_survivor = restored_parent / planned.name
        assert (
            artifact_module._entry_identity(os.stat(planned_survivor, follow_symlinks=False))
            == planned_identity
        )
        assert (
            artifact_module._entry_identity(os.stat(replacement_survivor, follow_symlinks=False))
            == replacement_identity
        )
        assert not any(
            entry.name.startswith(artifact_module._QUARANTINE_PREFIX)
            for entry in moved_container.rglob("*")
        )


@pytest.mark.parametrize("replacement", ["workspace", "round-ancestor"])
def test_reconcile_mid_recursion_swap_cannot_redirect_scan_or_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    for index in range(20):
        iteration = tmp_path / replacement / str(index)
        store, current, destination = prepared_store(iteration)
        destination.write_bytes(png())
        workspace = store.workspace_for(current)
        round_directory = workspace.parent
        outside = iteration / "outside"
        if replacement == "workspace":
            moved = iteration / "moved-workspace"
            sentinel = outside / PROVIDER_OUTPUT_PATH
        else:
            moved = iteration / "moved-round"
            sentinel = outside / current.attempt_token / PROVIDER_OUTPUT_PATH
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b"outside sentinel")
        original_entry_state = artifact_module._entry_state
        swapped = False

        def swap_during_recursive_scan(
            parent_fd,
            name,
            original=original_entry_state,
            owned_workspace=workspace,
            owned_round=round_directory,
            moved_entry=moved,
            outside_entry=outside,
        ):
            nonlocal swapped
            if name == PROVIDER_OUTPUT_PATH.name and not swapped:
                if replacement == "workspace":
                    owned_workspace.rename(moved_entry)
                    owned_workspace.symlink_to(outside_entry, target_is_directory=True)
                else:
                    owned_round.rename(moved_entry)
                    owned_round.symlink_to(outside_entry, target_is_directory=True)
                swapped = True
            return original(parent_fd, name)

        with monkeypatch.context() as patch:
            patch.setattr(artifact_module, "_entry_state", swap_during_recursive_scan)
            result = store.reconcile([])

        assert swapped is True
        assert result.removed_private_workspaces == 0
        assert result.skipped_unsafe_entries >= 1
        assert sentinel.read_bytes() == b"outside sentinel"


def test_copy_failure_leaves_no_partial_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, current, destination = prepared_store(tmp_path)
    destination.write_bytes(png())
    final = tmp_path / "published" / current.round_id / f"{current.attempt_token}.png"

    def fail_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        raise OSError("simulated copy/publish failure")

    monkeypatch.setattr("app.ai.generated_artifacts.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.publish(current, PROVIDER_OUTPUT_PATH)

    assert not final.exists()
    assert not list(final.parent.glob(".*.tmp"))


def test_configuration_and_attempt_identifiers_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        GeneratedArtifactStore(tmp_path / "same", tmp_path / "same")
    with pytest.raises(ValueError):
        GeneratedArtifactStore(tmp_path / "private", tmp_path / "published", max_bytes=0)
    with pytest.raises(ValueError):
        GeneratedArtifactStore(
            tmp_path / "private", tmp_path / "published", public_prefix="/generated/../x"
        )

    store = GeneratedArtifactStore(tmp_path / "private", tmp_path / "published")
    with pytest.raises(ValueError):
        store.prepare(GenerationAttempt(round_id="not-a-uuid", attempt_token=str(uuid4())))


def test_pipeline_workspace_publish_and_read_support_poc_dimensions(tmp_path: Path) -> None:
    store = GeneratedArtifactStore(tmp_path / "private", tmp_path / "published")
    current = attempt()
    workspace = store.prepare_workspace(current)
    workspace.staged_path.write_bytes(png(1672, 941))

    published = store.publish(current, workspace.relative_output_path)

    assert workspace.workspace == store.workspace_for(current)
    assert workspace.relative_output_path == PROVIDER_OUTPUT_PATH.as_posix()
    assert (published.artifact.width, published.artifact.height) == (1672, 941)
    assert store.read(published) == published.final_path.read_bytes()
