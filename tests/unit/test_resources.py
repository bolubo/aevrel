from __future__ import annotations

from shardferry.resources import resource_dir, resource_path


def test_packaged_schema_resources_are_resolvable() -> None:
    assert resource_path("migration-spec.schema.json").is_file()
    assert resource_path("plan.schema.json").is_file()
    assert resource_path("proof.schema.json").is_file()
    assert resource_path("report.schema.json").is_file()
    assert (resource_dir("v1") / "task.schema.json").is_file()
