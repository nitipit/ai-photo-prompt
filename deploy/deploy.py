#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["cyclopts==3.24.0"]
# ///
"""Local archive/image deployment with a deliberately small Cyclopts surface."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cyclopts import App

ROOT = Path(__file__).resolve().parents[1]
REMOTE_ARCHIVE = "/tmp/photo-prompt-deploy.tar.gz"
REMOTE_IMAGE = "/tmp/photo-prompt-image.tar"
SERVICE = "photo-prompt.service"


class DeployError(RuntimeError):
    """Raised for a failed local or mocked remote deployment boundary."""


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def run_command(args: Sequence[str], *, cwd: Path = ROOT, check: bool = True) -> CommandResult:
    """Run one subprocess; tests replace this boundary and perform no remote work."""

    completed = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    result = CommandResult(completed.stdout, completed.stderr, completed.returncode)
    if check and result.returncode != 0:
        raise DeployError(f"command failed: {args[0]}")
    return result


def remote(host: str, args: Sequence[str], *, check: bool = True) -> CommandResult:
    return run_command(("ssh", host, *args), check=check)


def git_output(*args: str) -> str:
    return run_command(("git", *args)).stdout.strip()


def preflight() -> str:
    if platform.machine() != "x86_64":
        raise DeployError("deployment requires x86_64")
    if git_output("branch", "--show-current") != "main":
        raise DeployError("deployment requires a clean main branch")
    if git_output("status", "--porcelain"):
        raise DeployError("deployment requires a clean main worktree")
    head = git_output("rev-parse", "HEAD")
    origin = git_output("rev-parse", "origin/main")
    if head != origin:
        raise DeployError("deployment requires HEAD == origin/main")
    return head


def build_archive(commit: str, output: Path) -> str:
    """Build the source archive locally and return its SHA-256."""

    with tarfile.open(output, "w:gz") as archive:
        for name in ("Containerfile", "pyproject.toml", "uv.lock", "src", "conf", "deploy", "dist"):
            path = ROOT / name
            if path.exists():
                archive.add(path, arcname=name, recursive=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    (output.parent / "commit.txt").write_text(commit + "\n", encoding="utf-8")
    return digest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _remote_health(host: str) -> dict[str, object]:
    result = remote(host, ("curl", "--fail", "--silent", "http://127.0.0.1:8000/health"))
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise DeployError("remote health response is not JSON") from error
    if not isinstance(value, dict):
        raise DeployError("remote health response is invalid")
    return value


def _require_idle(value: dict[str, object]) -> None:
    active = value.get("active_generation_count")
    if type(active) is not int or active < 0:
        raise DeployError("remote health did not report active generation count")
    if active > 0:
        raise DeployError("deployment aborted while generation is active")


def _remote_state_backup(host: str, backup_name: str) -> None:
    remote(
        host,
        (
            "podman",
            "volume",
            "export",
            "photo-prompt-state",
            f"--output=/var/lib/photo-prompt/backups/{backup_name}.tar",
        ),
    )
    remote(
        host,
        (
            "sh",
            "-c",
            "find /var/lib/photo-prompt/backups -name 'state-*.tar' -type f -printf '%T@ %p\\n' "
            "| sort -nr | tail -n +3 | cut -d' ' -f2- | xargs -r rm -f",
        ),
    )


def _remote_restore(host: str, backup_name: str) -> None:
    remote(host, ("podman", "volume", "rm", "--force", "photo-prompt-state"), check=False)
    remote(host, ("podman", "volume", "create", "photo-prompt-state"))
    remote(
        host,
        (
            "podman",
            "volume",
            "import",
            "photo-prompt-state",
            f"/var/lib/photo-prompt/backups/{backup_name}.tar",
        ),
    )


def deploy(*, host: str, remote_root: str = "/var/lib/photo-prompt") -> None:
    """Build, transfer, load, and health-check one local image deployment."""

    commit = preflight()
    run_command(("uv", "run", "photo-prompt-config-check"))
    run_command(("deno", "task", "app:build"))
    image_tag = f"localhost/photo-prompt:{commit}"
    deploy_tag = "localhost/photo-prompt:deploy"
    rollback_tag = "localhost/photo-prompt:rollback"
    run_command(("podman", "build", "--pull=never", "-f", "Containerfile", "-t", image_tag, "."))

    with tempfile.TemporaryDirectory(prefix="photo-prompt-deploy-") as temporary:
        directory = Path(temporary)
        archive = directory / "photo-prompt-deploy.tar.gz"
        image = directory / "photo-prompt-image.tar"
        archive_sha = build_archive(commit, archive)
        run_command(("podman", "save", "--output", str(image), image_tag))
        image_sha = _sha256(image)
        run_command(("scp", str(archive), f"{host}:{REMOTE_ARCHIVE}"))
        run_command(("scp", str(image), f"{host}:{REMOTE_IMAGE}"))
        remote(host, ("sha256sum", REMOTE_ARCHIVE))
        remote(host, ("sha256sum", REMOTE_IMAGE))
        remote(
            host,
            (
                "sh",
                "-c",
                f"test \"$(sha256sum {REMOTE_ARCHIVE} | cut -d' ' -f1)\" = {archive_sha}",
            ),
        )
        remote(
            host,
            (
                "sh",
                "-c",
                f"test \"$(sha256sum {REMOTE_IMAGE} | cut -d' ' -f1)\" = {image_sha}",
            ),
        )

        health = _remote_health(host)
        _require_idle(health)
        backup_name = f"state-{int(time.time())}"
        _remote_state_backup(host, backup_name)
        try:
            remote(host, ("podman", "tag", deploy_tag, rollback_tag), check=False)
            remote(host, ("podman", "load", "--input", REMOTE_IMAGE))
            remote(host, ("podman", "tag", image_tag, deploy_tag))
            remote(host, ("systemctl", "--user", "restart", SERVICE))
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                health = _remote_health(host)
                if health.get("ready") is True:
                    break
                time.sleep(2)
            else:
                raise DeployError("remote service did not become ready")
            remote(host, ("rm", "-f", REMOTE_ARCHIVE, REMOTE_IMAGE))
        except Exception:
            remote(host, ("podman", "tag", rollback_tag, deploy_tag), check=False)
            _remote_restore(host, backup_name)
            remote(host, ("systemctl", "--user", "restart", SERVICE), check=False)
            raise


app = App(name="photo-prompt-deploy", help="Photo Prompt archive deployment")
app.command(deploy)


@app.command
def status(*, host: str) -> None:
    """Print service status and the non-sensitive readiness health projection."""

    service = remote(host, ("systemctl", "--user", "is-active", SERVICE)).stdout.strip()
    health = _remote_health(host)
    print(json.dumps({"service": service, "health": health}, ensure_ascii=False))


if __name__ == "__main__":
    app()
