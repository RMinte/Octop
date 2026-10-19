"""Turn skill snapshots remain stable across upstream changes and damaged writes."""

from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import capabilities, resource_snapshot, service
from octop.infra.projects.models import ProjectFields, ProjectResources, SkillBinding
from octop.infra.projects.resource_snapshot import SnapshotError, load_snapshot, save_snapshot


def configuration(**changes):
    return json.dumps(
        {
            "skills": [{"source": "skillhub", "slug": "notes", "name": "Notes"}],
            "plugins": ["example"],
            "approval_policy_at_start": {"mode": "auto"},
            "private_credential": "not-in-the-cache",
            **changes,
        }
    )


@pytest.fixture
def skill_files():
    return [
        ("notes", [("SKILL.md", b"original instructions"), ("scripts/test.py", b"print('ok')")]),
        ("plugin-skill", [("SKILL.md", b"plugin instructions"), ("image.bin", bytes(range(256)))]),
    ]


def test_missing_snapshot_returns_none_and_round_trip_keeps_all_bytes(tmp_path, skill_files):
    root = tmp_path / "resources"
    assert load_snapshot(root, configuration()) is None
    save_snapshot(root, configuration(), skill_files)
    assert load_snapshot(root, configuration()) == skill_files
    assert "not-in-the-cache" not in (root / "skills.json").read_text()
    assert {path.name for path in root.iterdir()} == {"skills.json"}


def test_live_approval_changes_do_not_invalidate_the_selected_skill_content(tmp_path, skill_files):
    save_snapshot(tmp_path, configuration(), skill_files)
    assert (
        load_snapshot(tmp_path, configuration(approval_policy_at_start={"mode": "manual"}))
        == skill_files
    )


@pytest.mark.parametrize("changes", [{"skills": []}, {"plugins": []}])
def test_different_resource_selection_cannot_use_the_same_snapshot(tmp_path, skill_files, changes):
    save_snapshot(tmp_path, configuration(), skill_files)
    with pytest.raises(SnapshotError):
        load_snapshot(tmp_path, configuration(**changes))


@pytest.mark.parametrize("damage", ["truncated", "changed_file", "removed_skill"])
def test_corruption_is_an_error_instead_of_a_cache_miss(tmp_path, skill_files, damage):
    save_snapshot(tmp_path, configuration(), skill_files)
    path = tmp_path / "skills.json"
    if damage == "truncated":
        path.write_text('{"skills":')
    else:
        document = json.loads(path.read_bytes())
        if damage == "changed_file":
            document["skills"][0]["files"][0]["base64"] = base64.b64encode(b"changed").decode()
        else:
            document["skills"].pop()
        path.write_text(json.dumps(document))
    with pytest.raises(SnapshotError):
        load_snapshot(tmp_path, configuration())


@pytest.mark.parametrize(
    "path", ["../outside", "/outside", "a/../../outside", "C:\\outside", "C:outside"]
)
def test_snapshot_rejects_escaping_paths_before_publication(tmp_path, path):
    with pytest.raises(SnapshotError):
        save_snapshot(tmp_path, configuration(), [("notes", [("SKILL.md", b"ok"), (path, b"bad")])])
    assert not (tmp_path / "skills.json").exists()


def test_loaded_paths_are_validated_even_when_document_hash_matches(tmp_path, skill_files):
    save_snapshot(tmp_path, configuration(), skill_files)
    path = tmp_path / "skills.json"
    document = json.loads(path.read_bytes())
    document["skills"][0]["files"][1]["path"] = "../outside"
    document.pop("sha256")
    encoded = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    document["sha256"] = hashlib.sha256(encoded).hexdigest()
    path.write_text(json.dumps(document))
    with pytest.raises(SnapshotError):
        load_snapshot(tmp_path, configuration())


def test_interrupted_atomic_publication_preserves_the_previous_complete_snapshot(
    tmp_path, skill_files, monkeypatch
):
    save_snapshot(tmp_path, configuration(), skill_files)

    def interrupted(*args):
        raise OSError("interrupted before publication")

    monkeypatch.setattr(resource_snapshot.os, "replace", interrupted)
    with pytest.raises(SnapshotError):
        save_snapshot(tmp_path, configuration(), [("notes", [("SKILL.md", b"changed")])])
    assert load_snapshot(tmp_path, configuration()) == skill_files
    assert {path.name for path in tmp_path.iterdir()} == {"skills.json"}


def test_cached_skills_skip_missing_installations_but_still_validate_live_mcp(monkeypatch):
    available = ProjectResources(
        skills=[], mcp_servers=[], plugins=[], subagents=[], directory_roots=[]
    )
    monkeypatch.setattr(service, "resources", lambda *args, **kwargs: available)
    fields = ProjectFields(
        name="project", skills=[SkillBinding(package_id="removed", slug="notes")]
    )
    assert service._validate(None, 1, fields, cached_skills=True) is None
    with pytest.raises(OctopError) as error:
        service._validate(None, 1, fields)
    assert error.value.code == ErrorCode.PROJECT_RESOURCE_INVALID
    fields.mcp_servers = ["missing"]
    with pytest.raises(OctopError) as error:
        service._validate(None, 1, fields, cached_skills=True)
    assert error.value.code == ErrorCode.PROJECT_RESOURCE_INVALID


def test_cached_validation_does_not_even_read_the_installation_store(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("installation directory temporarily unavailable")

    monkeypatch.setattr(service, "SkillPackageStore", unavailable)
    monkeypatch.setattr(
        service,
        "ConnectorService",
        lambda **kwargs: SimpleNamespace(list_instances_for_api=lambda _: []),
    )
    monkeypatch.setattr(service, "_allowed_root", lambda *args: None)
    monkeypatch.setattr(service, "capability_options", lambda *args: ([], []))
    services = SimpleNamespace(
        connector_repo=None, secret_repo=None, settings_repo=None, config=None
    )
    fields = ProjectFields(
        name="project", skills=[SkillBinding(package_id="removed", slug="notes")]
    )
    assert service._validate(services, 1, fields, cached_skills=True) is None


def test_plugin_snapshot_skips_skill_reads_but_keeps_current_tools_and_middleware(monkeypatch):
    def unexpected_read(*args):
        pytest.fail("cached plugin skill content must not be read from the current installation")

    registry = SimpleNamespace(
        list_plugins=lambda: [SimpleNamespace(manifest=SimpleNamespace(id="example"))],
        build_middleware_chain=lambda **kwargs: ["current-middleware"],
        get=unexpected_read,
    )
    monkeypatch.setattr(capabilities, "PluginRegistry", lambda: registry)
    monkeypatch.setattr(capabilities, "build_plugin_tools", lambda **kwargs: ["current-tools"])
    monkeypatch.setattr(capabilities, "sanitize_plugin_tool_names", lambda *args, **kwargs: None)
    assert capabilities.plugin_capabilities(
        ProjectFields(name="project", plugins=["example"]), reserved=set(), include_skills=False
    ) == (["current-tools"], ["current-middleware"], [])
