from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from app.content.importer import ChallengeMaterializationError, materialize_challenges
from app.content.repository import CatalogValidationError, ChallengeCatalog


def test_materialization_is_deterministic_and_copies_browser_asset_paths(
    tmp_path: Path, challenge_source: Path
) -> None:
    first = materialize_challenges(challenge_source, tmp_path / "first")
    second = materialize_challenges(challenge_source, tmp_path / "second")

    assert first.catalog_path.read_bytes() == second.catalog_path.read_bytes()
    assert [challenge.id for challenge in first.challenges] == sorted(
        challenge.id for challenge in first.challenges
    )
    assert len(first.challenges) == 20
    assert Counter(challenge.level for challenge in first.challenges) == {
        level: 5 for level in {challenge.level for challenge in first.challenges}
    }
    for challenge in first.challenges:
        assert challenge.target_asset_url == f"/assets/challenges/{challenge.id}.webp"
        target = first.assets_dir / f"{challenge.id}.webp"
        assert target.is_file()
        assert (
            target.read_bytes()
            == (
                challenge_source / _bundle_for_id(challenge_source, challenge.id) / "target.webp"
            ).read_bytes()
        )


def test_malformed_bundle_fails_before_replacing_existing_output(
    tmp_path: Path, challenge_source: Path
) -> None:
    source = tmp_path / "source"
    shutil.copytree(challenge_source, source)
    output = tmp_path / "generated"
    original = materialize_challenges(source, output).catalog_path.read_bytes()

    frontmatter_path = source / "p1-p3-01-rabbit-pancake" / "challenge.md"
    frontmatter_path.write_text(
        frontmatter_path.read_text(encoding="utf-8").replace("schema: 1", "schema: 2", 1),
        encoding="utf-8",
    )

    with pytest.raises(ChallengeMaterializationError):
        materialize_challenges(source, output)
    assert (output / "catalog.json").read_bytes() == original


def test_invariant_rejects_wrong_total_and_wrong_level_distribution(
    tmp_path: Path, challenge_source: Path
) -> None:
    source = tmp_path / "source"
    shutil.copytree(challenge_source, source)
    shutil.rmtree(source / "p1-p3-01-rabbit-pancake")
    with pytest.raises(ChallengeMaterializationError, match="exactly 20"):
        materialize_challenges(source, tmp_path / "generated")

    source = tmp_path / "unbalanced-source"
    shutil.copytree(challenge_source, source)
    markdown_path = source / "p4-p6-01-dragon-goalkeeper" / "challenge.md"
    markdown_path.write_text(
        markdown_path.read_text(encoding="utf-8").replace("level: p4-p6", "level: p1-p3", 1),
        encoding="utf-8",
    )
    with pytest.raises(ChallengeMaterializationError, match="per level"):
        materialize_challenges(source, tmp_path / "unbalanced-generated")


def test_runtime_catalog_validates_generated_json_without_markdown_access(
    materialized_catalog,
) -> None:
    catalog = ChallengeCatalog.load(materialized_catalog.catalog_path)

    assert len(catalog) == 20
    assert len(catalog.for_level("p1-p3")) == 5
    assert catalog.get("p1-p3-01").title == "Rabbit chef and giant pancake"

    payload = json.loads(materialized_catalog.catalog_path.read_text(encoding="utf-8"))
    payload["challenges"].pop()
    invalid_path = materialized_catalog.output_dir / "invalid.json"
    invalid_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CatalogValidationError):
        ChallengeCatalog.load(invalid_path)


def test_malformed_sections_are_rejected(tmp_path: Path, challenge_source: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(challenge_source, source)
    markdown_path = source / "p1-p3-01-rabbit-pancake" / "challenge.md"
    markdown = markdown_path.read_text(encoding="utf-8").replace(
        "## Feedback focus", "## Unknown section", 1
    )
    markdown_path.write_text(markdown, encoding="utf-8")

    with pytest.raises(ChallengeMaterializationError, match="expected H2 sections"):
        materialize_challenges(source, tmp_path / "generated")


def _bundle_for_id(source: Path, challenge_id: str) -> str:
    for bundle in source.iterdir():
        if bundle.is_dir() and bundle.name.startswith(challenge_id + "-"):
            return bundle.name
    raise AssertionError(f"missing bundle for {challenge_id}")
