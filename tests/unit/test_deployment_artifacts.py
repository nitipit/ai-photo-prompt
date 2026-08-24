from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
DEPLOY = ROOT / "deploy"


@pytest.fixture
def deploy_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("photo_prompt_deploy", DEPLOY / "deploy.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_container_contract_builds_assets_in_builder_and_excludes_runtime_state() -> None:
    containerfile = (ROOT / "Containerfile").read_text(encoding="utf-8")
    ignore = (ROOT / ".containerignore").read_text(encoding="utf-8")
    assert containerfile.count("FROM registry.fedoraproject.org/fedora:44@sha256:") == 2
    assert "RUN deno task app:build" in containerfile
    assert "COPY --from=builder /app/dist ./dist" in containerfile
    assert "COPY dist ./dist" not in containerfile
    assert '"--workers", "1"' in containerfile
    assert "conf/app.toml" in ignore
    for excluded in (".git", ".agents", "data", "discord-webhook", "*.secret"):
        assert excluded in ignore
    assert "caddy" not in containerfile.lower()


def test_quadlets_parse_through_noninstalling_generator(tmp_path: Path) -> None:
    generator = Path("/usr/libexec/podman/quadlet")
    if not generator.is_file():
        pytest.skip("Podman Quadlet generator is unavailable")
    source = tmp_path / "quadlets"
    source.mkdir()
    for path in DEPLOY.glob("*.container"):
        shutil.copy2(path, source / path.name)
    for path in DEPLOY.glob("*.network"):
        shutil.copy2(path, source / path.name)
    for path in DEPLOY.glob("*.pod"):
        shutil.copy2(path, source / path.name)
    for path in DEPLOY.glob("*.volume"):
        shutil.copy2(path, source / path.name)
    env = {**os.environ, "QUADLET_UNIT_DIRS": str(source)}
    result = subprocess.run(
        [str(generator), "-dryrun", "-user"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "photo-prompt.service" in result.stdout
    assert "photo-prompt-pod.service" in result.stdout


def test_deploy_script_exposes_only_status_and_deploy_without_recovery_commands() -> None:
    result = subprocess.run(
        ["uv", "run", "--script", str(DEPLOY / "deploy.py"), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "status" in result.stdout
    assert "deploy" in result.stdout
    assert "setup-host" not in result.stdout
    assert "pi-login" not in result.stdout
    script = (DEPLOY / "deploy.py").read_text(encoding="utf-8")
    assert "sha256sum" in script
    assert "DEPLOY_TAG" in script
    assert "REMOTE_IMAGE" in script
    assert "photo-prompt-deploy.tar.gz" not in script
    assert "tarfile" not in script
    assert '"podman", "volume"' not in script


def test_remote_uses_one_shlex_command_for_nested_shell_arguments(
    deploy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(args: tuple[str, ...], *, check: bool = True, **_: Any) -> Any:
        calls.append(args)
        return deploy_module.CommandResult()

    monkeypatch.setattr(deploy_module, "run_command", fake_run)
    nested_script = 'printf \'%s\\n\' "$1"; test "$2" = "a b; $(touch nope)"'
    remote_args = ("sh", "-c", nested_script, "sh", "a b; $(touch nope)", "quote'\"$x")

    deploy_module.remote("kiosk-host", remote_args)

    assert len(calls) == 1
    assert calls[0][:2] == ("ssh", "kiosk-host")
    assert len(calls[0]) == 3
    assert shlex.split(calls[0][2]) == list(remote_args)
    assert "$(touch nope)" in calls[0][2]


def test_home_relative_root_is_resolved_on_remote_host(
    deploy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_remote(_host: str, args: tuple[str, ...], **_: Any) -> Any:
        calls.append(args)
        return deploy_module.CommandResult("/home/operator/photo-prompt")

    monkeypatch.setattr(deploy_module, "remote", fake_remote)
    assert (
        deploy_module._resolve_remote_root("kiosk-host", "~/photo-prompt")
        == "/home/operator/photo-prompt"
    )
    assert calls[0][:2] == ("sh", "-c")
    assert "root='~/photo-prompt'" in calls[0][2]


def test_remote_health_executes_container_local_projection(
    deploy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_remote(host: str, args: tuple[str, ...], *, check: bool = True) -> Any:
        calls.append(args)
        return deploy_module.CommandResult('{"ready":true,"active_generation_count":0}')

    monkeypatch.setattr(deploy_module, "remote", fake_remote)
    assert deploy_module._remote_health("kiosk-host") == {
        "ready": True,
        "active_generation_count": 0,
    }
    assert calls[0][:5] == (
        "podman",
        "exec",
        "photo-prompt",
        "/opt/venv/bin/python",
        "-c",
    )
    assert "curl" not in calls[0][-1]
    assert "127.0.0.1:8000/health" in calls[0][-1]


def test_health_malformed_success_is_error_but_exec_failure_is_tolerated_type(
    deploy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        deploy_module,
        "remote",
        lambda *_args, **_kwargs: deploy_module.CommandResult("not-json"),
    )
    with pytest.raises(deploy_module.DeployError, match="not JSON"):
        deploy_module._remote_health("kiosk-host")

    monkeypatch.setattr(
        deploy_module,
        "remote",
        lambda *_args, **_kwargs: deploy_module.CommandResult(returncode=1),
    )
    with pytest.raises(deploy_module.RemoteHealthUnavailable):
        deploy_module._remote_health("kiosk-host")


def test_readiness_poll_tolerates_temporary_exec_and_non_ready_results(
    deploy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    values: list[Any] = [
        deploy_module.RemoteHealthUnavailable("not ready yet"),
        {"ready": False, "active_generation_count": 0},
        {"ready": True, "active_generation_count": 0},
    ]
    monkeypatch.setattr(deploy_module, "_remote_health", lambda _host: _next_health(values))
    monkeypatch.setattr(deploy_module.time, "sleep", lambda _seconds: None)

    deploy_module._wait_until_ready("kiosk-host")


def test_deploy_orders_config_gate_idle_gate_switch_restart_and_cleanup(
    deploy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, tuple[str, ...]]] = []
    image_bytes = b"oci image"
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    health_calls = 0

    monkeypatch.setattr(deploy_module, "preflight", lambda: "abc123")

    def fake_run(args: tuple[str, ...], *, check: bool = True, **_: Any) -> Any:
        events.append(("local", args))
        if args[:2] == ("podman", "save"):
            Path(args[3]).write_bytes(image_bytes)
        return deploy_module.CommandResult()

    def fake_remote(host: str, args: tuple[str, ...], *, check: bool = True) -> Any:
        nonlocal health_calls
        events.append(("remote", args))
        if args[:2] == ("sha256sum", deploy_module.REMOTE_IMAGE):
            return deploy_module.CommandResult(f"{image_sha}  {deploy_module.REMOTE_IMAGE}\n")
        if args == ("podman", "container", "exists", "photo-prompt"):
            return deploy_module.CommandResult()
        if args[:3] == ("podman", "exec", "photo-prompt"):
            health_calls += 1
            return deploy_module.CommandResult(
                json.dumps({"ready": health_calls > 1, "active_generation_count": 0})
            )
        return deploy_module.CommandResult()

    monkeypatch.setattr(deploy_module, "run_command", fake_run)
    monkeypatch.setattr(deploy_module, "remote", fake_remote)
    deploy_module.deploy(host="kiosk-host", remote_root="/srv/photo-prompt")

    remote_commands = [args for kind, args in events if kind == "remote"]
    config_index = next(
        index for index, args in enumerate(remote_commands) if args[:2] == ("podman", "run")
    )
    idle_index = next(
        index
        for index, args in enumerate(remote_commands)
        if args[:3] == ("podman", "exec", "photo-prompt")
    )
    tag_index = remote_commands.index(
        ("podman", "tag", "localhost/photo-prompt:abc123", deploy_module.DEPLOY_TAG)
    )
    restart_index = remote_commands.index(("systemctl", "--user", "restart", deploy_module.SERVICE))
    cleanup_index = remote_commands.index(("rm", "-f", deploy_module.REMOTE_IMAGE))
    assert config_index < idle_index < tag_index < restart_index < cleanup_index
    config_command = remote_commands[config_index]
    assert "localhost/photo-prompt:abc123" in config_command
    assert "/srv/photo-prompt/app.toml:/etc/photo-prompt/app.toml:ro" in config_command
    assert "--entrypoint" in config_command
    assert "PYTHONPATH=/app/src" in config_command
    assert "--pull=never" in config_command
    assert not any(args[:2] == ("podman", "volume") for args in remote_commands)
    assert health_calls == 2


def test_initial_deploy_skips_missing_container_health_and_restarts(
    deploy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, ...]] = []
    image_bytes = b"oci image"
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    health_calls = 0
    monkeypatch.setattr(deploy_module, "preflight", lambda: "abc123")

    def fake_run(args: tuple[str, ...], **_: Any) -> Any:
        events.append(args)
        if args[:2] == ("podman", "save"):
            Path(args[3]).write_bytes(image_bytes)
        return deploy_module.CommandResult()

    def fake_remote(_host: str, args: tuple[str, ...], **_: Any) -> Any:
        nonlocal health_calls
        events.append(args)
        if args[:2] == ("sha256sum", deploy_module.REMOTE_IMAGE):
            return deploy_module.CommandResult(f"{image_sha}  {deploy_module.REMOTE_IMAGE}\n")
        if args == ("podman", "container", "exists", "photo-prompt"):
            return deploy_module.CommandResult(returncode=1)
        if args[:3] == ("podman", "exec", "photo-prompt"):
            health_calls += 1
            return deploy_module.CommandResult('{"ready":true,"active_generation_count":0}')
        return deploy_module.CommandResult()

    monkeypatch.setattr(deploy_module, "run_command", fake_run)
    monkeypatch.setattr(deploy_module, "remote", fake_remote)
    deploy_module.deploy(host="kiosk-host", remote_root="/srv/photo-prompt")

    assert ("podman", "container", "exists", "photo-prompt") in events
    assert ("podman", "tag", "localhost/photo-prompt:abc123", deploy_module.DEPLOY_TAG) in events
    assert ("systemctl", "--user", "restart", deploy_module.SERVICE) in events
    assert health_calls == 1


def test_existing_unhealthy_container_aborts_before_switch(
    deploy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, ...]] = []
    image_bytes = b"oci image"
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    monkeypatch.setattr(deploy_module, "preflight", lambda: "abc123")

    def fake_run(args: tuple[str, ...], **_: Any) -> Any:
        events.append(args)
        if args[:2] == ("podman", "save"):
            Path(args[3]).write_bytes(image_bytes)
        return deploy_module.CommandResult()

    def fake_remote(_host: str, args: tuple[str, ...], **_: Any) -> Any:
        events.append(args)
        if args[:2] == ("sha256sum", deploy_module.REMOTE_IMAGE):
            return deploy_module.CommandResult(f"{image_sha}  {deploy_module.REMOTE_IMAGE}\n")
        if args == ("podman", "container", "exists", "photo-prompt"):
            return deploy_module.CommandResult()
        if args[:3] == ("podman", "exec", "photo-prompt"):
            return deploy_module.CommandResult(returncode=1)
        return deploy_module.CommandResult()

    monkeypatch.setattr(deploy_module, "run_command", fake_run)
    monkeypatch.setattr(deploy_module, "remote", fake_remote)
    with pytest.raises(deploy_module.RemoteHealthUnavailable):
        deploy_module.deploy(host="kiosk-host", remote_root="/srv/photo-prompt")

    assert not any(args[:2] == ("podman", "tag") for args in events)
    assert not any(args[:2] == ("systemctl", "--user") for args in events)
    assert events[-1] == ("rm", "-f", deploy_module.REMOTE_IMAGE)


def test_invalid_active_config_aborts_before_idle_gate_or_switch(
    deploy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, ...]] = []
    image_bytes = b"oci image"
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    monkeypatch.setattr(deploy_module, "preflight", lambda: "abc123")

    def fake_run(args: tuple[str, ...], **_: Any) -> Any:
        events.append(args)
        if args[:2] == ("podman", "save"):
            Path(args[3]).write_bytes(image_bytes)
        return deploy_module.CommandResult()

    def fake_remote(_host: str, args: tuple[str, ...], **_: Any) -> Any:
        events.append(args)
        if args[:2] == ("sha256sum", deploy_module.REMOTE_IMAGE):
            return deploy_module.CommandResult(f"{image_sha}  {deploy_module.REMOTE_IMAGE}\n")
        if args[:2] == ("podman", "run"):
            raise deploy_module.DeployError("active configuration is invalid")
        return deploy_module.CommandResult()

    monkeypatch.setattr(deploy_module, "run_command", fake_run)
    monkeypatch.setattr(deploy_module, "remote", fake_remote)
    with pytest.raises(deploy_module.DeployError, match="active configuration"):
        deploy_module.deploy(host="kiosk-host", remote_root="/srv/photo-prompt")

    assert not any(args[:2] == ("systemctl", "--user") for args in events)
    assert not any(args[:2] == ("podman", "tag") for args in events)
    assert any(args == ("rm", "-f", deploy_module.REMOTE_IMAGE) for args in events)


def test_active_generation_aborts_before_switch(
    deploy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, ...]] = []
    image_bytes = b"oci image"
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    monkeypatch.setattr(deploy_module, "preflight", lambda: "abc123")

    def fake_run(args: tuple[str, ...], **_: Any) -> Any:
        events.append(args)
        if args[:2] == ("podman", "save"):
            Path(args[3]).write_bytes(image_bytes)
        return deploy_module.CommandResult()

    def fake_remote(_host: str, args: tuple[str, ...], **_: Any) -> Any:
        events.append(args)
        if args[:2] == ("sha256sum", deploy_module.REMOTE_IMAGE):
            return deploy_module.CommandResult(f"{image_sha}  {deploy_module.REMOTE_IMAGE}\n")
        if args[:3] == ("podman", "exec", "photo-prompt"):
            return deploy_module.CommandResult('{"ready":true,"active_generation_count":1}')
        return deploy_module.CommandResult()

    monkeypatch.setattr(deploy_module, "run_command", fake_run)
    monkeypatch.setattr(deploy_module, "remote", fake_remote)
    with pytest.raises(deploy_module.DeployError, match="generation is active"):
        deploy_module.deploy(host="kiosk-host", remote_root="/srv/photo-prompt")

    assert not any(args[:2] == ("podman", "tag") for args in events)
    assert not any(args[:2] == ("systemctl", "--user") for args in events)


def test_post_switch_health_failure_is_clear_and_does_not_recover(
    deploy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, ...]] = []
    image_bytes = b"oci image"
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    monkeypatch.setattr(deploy_module, "preflight", lambda: "abc123")
    monkeypatch.setattr(deploy_module, "HEALTH_TIMEOUT_SECONDS", 0.0)

    def fake_run(args: tuple[str, ...], **_: Any) -> Any:
        events.append(args)
        if args[:2] == ("podman", "save"):
            Path(args[3]).write_bytes(image_bytes)
        return deploy_module.CommandResult()

    def fake_remote(_host: str, args: tuple[str, ...], **_: Any) -> Any:
        events.append(args)
        if args[:2] == ("sha256sum", deploy_module.REMOTE_IMAGE):
            return deploy_module.CommandResult(f"{image_sha}  {deploy_module.REMOTE_IMAGE}\n")
        if args[:3] == ("podman", "exec", "photo-prompt"):
            return deploy_module.CommandResult('{"ready":false,"active_generation_count":0}')
        return deploy_module.CommandResult()

    monkeypatch.setattr(deploy_module, "run_command", fake_run)
    monkeypatch.setattr(deploy_module, "remote", fake_remote)

    with pytest.raises(deploy_module.DeployError, match="health deadline"):
        deploy_module.deploy(host="kiosk-host", remote_root="/srv/photo-prompt")

    assert any(args[:2] == ("podman", "tag") for args in events)
    assert any(args[:2] == ("systemctl", "--user") for args in events)
    assert not any(args[:2] == ("podman", "volume") for args in events)
    assert events[-1] == ("rm", "-f", deploy_module.REMOTE_IMAGE)


def test_optional_secret_and_pod_level_network_are_documented() -> None:
    container = (DEPLOY / "photo-prompt.container").read_text(encoding="utf-8")
    pod = (DEPLOY / "photo-prompt.pod").read_text(encoding="utf-8")
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "Secret=" not in container
    assert "Network=" not in container
    assert "Network=photo-prompt.network" in pod
    assert "Secret=photo-prompt-staff-pin,type=env,target=PHOTO_PROMPT_STAFF_PIN" in readme
    assert "Without this opt-in" in readme


def _next_health(values: list[Any]) -> Any:
    value = values.pop(0)
    if isinstance(value, BaseException):
        raise value
    return value
