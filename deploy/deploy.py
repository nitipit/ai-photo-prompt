#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["cyclopts==3.24.0"]
# ///
"""Local image deployment with a deliberately small Cyclopts surface."""

from __future__ import annotations

import hashlib
import json
import platform
import shlex
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cyclopts import App

ROOT = Path(__file__).resolve().parents[1]
REMOTE_IMAGE = "/tmp/photo-prompt-image.tar"
SERVICE = "photo-prompt.service"
DEPLOY_ROOT = "~/photo-prompt"
DEPLOY_TAG = "localhost/photo-prompt:deploy"
HEALTH_TIMEOUT_SECONDS = 60.0
HEALTH_POLL_SECONDS = 2.0

_HEALTH_SCRIPT = (
    "import json,urllib.request;"
    "value=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3));"
    "print(json.dumps({'ready':value.get('ready'),"
    "'active_generation_count':value.get('active_generation_count')},separators=(',',':')))"
)
_CONFIG_CHECK_SCRIPT = "from app.config import main; raise SystemExit(main())"


class DeployError(RuntimeError):
    """Raised for a failed local or mocked remote deployment boundary."""


class RemoteHealthUnavailable(DeployError):
    """Raised when a temporary container-local health command cannot run."""


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
    """Run exactly one safely serialized command through the remote shell."""

    return run_command(("ssh", host, shlex.join(list(args))), check=check)


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_remote_root(host: str, remote_root: str) -> str:
    """Resolve the operator's home-relative root on the remote host."""

    if remote_root.startswith("/"):
        return remote_root.rstrip("/") or "/"
    script = "\n".join(
        (
            f"root={shlex.quote(remote_root)}",
            'case "$root" in',
            '  "~") root=$HOME ;;',
            '  "~/"*) root=$HOME/${root#\\~} ;;',
            "  /*) ;;",
            "  *) printf 'deployment root must be absolute or home-relative\\n' >&2; exit 2 ;;",
            "esac",
            "printf '%s' \"$root\"",
        )
    )
    result = remote(host, ("sh", "-c", script))
    resolved = result.stdout.strip()
    if not resolved or not resolved.startswith("/") or "\n" in result.stdout:
        raise DeployError("remote deployment root was not an absolute path")
    return resolved.rstrip("/") or "/"


def _remote_sha256(host: str, path: str, expected: str) -> None:
    result = remote(host, ("sha256sum", path))
    fields = result.stdout.split()
    if not fields or fields[0] != expected:
        raise DeployError(f"remote SHA-256 verification failed for {path}")


def _remote_health(host: str) -> dict[str, object]:
    """Read only the non-sensitive health projection inside the app container."""

    try:
        result = remote(
            host,
            ("podman", "exec", "photo-prompt", "/opt/venv/bin/python", "-c", _HEALTH_SCRIPT),
            check=False,
        )
    except DeployError as error:
        raise RemoteHealthUnavailable("remote health command was unavailable") from error
    if result.returncode != 0:
        raise RemoteHealthUnavailable("remote health command was unavailable")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise DeployError("remote health response is not JSON") from error
    if not isinstance(value, dict):
        raise DeployError("remote health response is invalid")
    ready = value.get("ready")
    active = value.get("active_generation_count")
    if type(ready) is not bool or type(active) is not int or active < 0:
        raise DeployError("remote health response is invalid")
    return {"ready": ready, "active_generation_count": active}


def _require_idle(value: dict[str, object]) -> None:
    active = value.get("active_generation_count")
    if type(active) is not int or active < 0:
        raise DeployError("remote health did not report active generation count")
    if active > 0:
        raise DeployError("deployment aborted while generation is active")


def _wait_until_ready(host: str) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    last_unavailable: RemoteHealthUnavailable | None = None
    while True:
        try:
            health = _remote_health(host)
        except RemoteHealthUnavailable as error:
            last_unavailable = error
        else:
            if health["ready"] is True:
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(HEALTH_POLL_SECONDS, remaining))
    failure = DeployError("remote service did not become ready before the health deadline")
    if last_unavailable is not None:
        raise failure from last_unavailable
    raise failure


def _remote_validate_active_config(host: str, remote_root: str, image_tag: str) -> None:
    config_path = f"{remote_root}/app.toml"
    remote(
        host,
        (
            "podman",
            "run",
            "--rm",
            "--pull=never",
            "--entrypoint",
            "/opt/venv/bin/python",
            "--env",
            "PHOTO_PROMPT_CONFIG=/etc/photo-prompt/app.toml",
            "--env",
            "PYTHONPATH=/app/src",
            "--volume",
            f"{config_path}:/etc/photo-prompt/app.toml:ro",
            image_tag,
            "-c",
            _CONFIG_CHECK_SCRIPT,
        ),
    )


def _cleanup_remote_image(host: str) -> None:
    try:
        remote(host, ("rm", "-f", REMOTE_IMAGE), check=False)
    except Exception:
        pass


def deploy(*, host: str, remote_root: str = DEPLOY_ROOT) -> None:
    """Build, transfer, validate, switch, and health-check one local image."""

    commit = preflight()
    run_command(("deno", "task", "app:build"))
    image_tag = f"localhost/photo-prompt:{commit}"
    run_command(("podman", "build", "--pull=never", "-f", "Containerfile", "-t", image_tag, "."))

    with tempfile.TemporaryDirectory(prefix="photo-prompt-deploy-") as temporary:
        image = Path(temporary) / "photo-prompt-image.tar"
        run_command(("podman", "save", "--output", str(image), image_tag))
        image_sha = _sha256(image)
        transfer_attempted = True
        try:
            run_command(("scp", str(image), f"{host}:{REMOTE_IMAGE}"))
            _remote_sha256(host, REMOTE_IMAGE, image_sha)
            remote(host, ("podman", "load", "--input", REMOTE_IMAGE))
            resolved_root = _resolve_remote_root(host, remote_root)
            _remote_validate_active_config(host, resolved_root, image_tag)
            _require_idle(_remote_health(host))
            remote(host, ("podman", "tag", image_tag, DEPLOY_TAG))
            remote(host, ("systemctl", "--user", "restart", SERVICE))
            _wait_until_ready(host)
        finally:
            if transfer_attempted:
                _cleanup_remote_image(host)


app = App(name="photo-prompt-deploy", help="Photo Prompt image deployment")
app.command(deploy)


@app.command
def status(*, host: str) -> None:
    """Print service status and the non-sensitive readiness health projection."""

    service = remote(host, ("systemctl", "--user", "is-active", SERVICE)).stdout.strip()
    health = _remote_health(host)
    print(json.dumps({"service": service, "health": health}, ensure_ascii=False))


if __name__ == "__main__":
    app()
