from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ConfigError, RuntimeConfig, load_config

SAMPLE = Path(__file__).parents[2] / "conf" / "app.sample.toml"


def test_sample_is_complete_and_resolves_relative_paths_from_project_root() -> None:
    config = load_config(SAMPLE)
    assert config.schema_version == 1
    assert config.pi.provider == "openai-codex"
    assert config.pi.max_concurrent_attempts == 3
    assert config.paths.pi_bridge_path == SAMPLE.parents[1] / "deploy" / "codex-bridge.ts"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("schema_version"),
        lambda value: value["paths"].pop("db_path"),
        lambda value: value["pi"].pop("max_concurrent_attempts"),
        lambda value: value["pi"].update(unknown_key=1),
    ],
)
def test_runtime_toml_rejects_missing_or_unknown_keys(mutate) -> None:
    import tomllib

    raw = tomllib.loads(SAMPLE.read_text(encoding="utf-8"))
    mutate(raw)
    with pytest.raises(ConfigError):
        RuntimeConfig.from_toml(raw)


def test_runtime_toml_rejects_unsupported_provider_and_bad_capacity() -> None:
    import tomllib

    raw = tomllib.loads(SAMPLE.read_text(encoding="utf-8"))
    raw["pi"]["provider"] = "fake"
    with pytest.raises(ConfigError):
        RuntimeConfig.from_toml(raw)
    raw = tomllib.loads(SAMPLE.read_text(encoding="utf-8"))
    raw["pi"]["max_concurrent_attempts"] = 4
    with pytest.raises(ConfigError):
        RuntimeConfig.from_toml(raw)


def test_runtime_toml_rejects_unsafe_bridge_relationship() -> None:
    import tomllib

    raw = tomllib.loads(SAMPLE.read_text(encoding="utf-8"))
    raw["paths"]["pi_bridge_path"] = "/tmp/bridge.ts"
    with pytest.raises(ConfigError, match="inside the project root"):
        RuntimeConfig.from_toml(raw)
