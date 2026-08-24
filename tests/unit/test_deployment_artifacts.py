from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
DEPLOY = ROOT / "deploy"


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


def test_deploy_script_exposes_only_status_and_deploy_without_remote_test_actions() -> None:
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
    assert "rollback" not in result.stdout
    script = (DEPLOY / "deploy.py").read_text(encoding="utf-8")
    assert "sha256sum" in script
    assert "photo-prompt:rollback" in script
    assert "global prune" not in script.lower()
