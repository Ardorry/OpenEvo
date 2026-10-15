from pathlib import Path

import pytest

from openevo_chemcrow.chemcrowrun import initialize, paired_statistics


def test_initialize_is_isolated_and_never_reuses_state(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    root = initialize(
        run_id="test-run", output_root=tmp_path, fixture_root=fixtures, port_base=18080
    )
    assert root == tmp_path / "test-run"
    assert (root / "chemcrowrun.reproduction.yaml").is_file()
    assert (root / "run.json").stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        initialize(run_id="test-run", output_root=tmp_path, fixture_root=fixtures, port_base=18080)


@pytest.mark.parametrize("name", ["../old", "/tmp/old", "", "a/b"])
def test_run_name_cannot_escape_root(tmp_path, name):
    with pytest.raises(ValueError):
        initialize(run_id=name, output_root=tmp_path, fixture_root=tmp_path, port_base=18080)


def test_exact_paired_test_and_deterministic_bootstrap():
    result = paired_statistics([1, 2, 3, 4], [2, 3, 4, 5])
    assert result["mean_delta"] == 1
    assert result["bootstrap_95_ci"] == [1, 1]
    assert result["exact_sign_flip_p_two_sided"] == 0.125
    assert result == paired_statistics([1, 2, 3, 4], [2, 3, 4, 5])
    assert paired_statistics([1, 1], [1, 1])["exact_sign_flip_p_two_sided"] == 1


def test_statistics_rejects_missing_nonfinite_or_unpaired():
    for a, b in [([], []), ([1], [1, 2]), ([float("nan")], [1])]:
        with pytest.raises(ValueError):
            paired_statistics(a, b)


def test_canonical_config_preserves_legacy_protocol():
    import yaml

    package = Path(__file__).parents[1]
    legacy = yaml.safe_load((package / "configs/full-v8.reproduction.example.yaml").read_text())
    canonical = yaml.safe_load((package / "configs/chemcrowrun.reproduction.yaml").read_text())
    assert canonical == legacy


def test_incomplete_workspace_never_exports_a_submission(tmp_path, monkeypatch):
    from openevo_chemcrow.workflow import export_report

    root = initialize(
        run_id="incomplete", output_root=tmp_path, fixture_root=tmp_path, port_base=18080
    )
    # Environment changes are confined to this test by monkeypatch.
    import os

    monkeypatch.setattr(os, "environ", dict(os.environ))
    report = export_report(root)
    assert report["status"] == "BLOCKED"
    assert len(report["missing_tasks"]) == 14
    assert not (root / "chemcrowrun-submission.json").exists()


def test_runtime_rejects_credential_url_before_network(tmp_path):
    from openevo_chemcrow.runtime_assets import install_runtime

    for url in (
        "http://example.com/runtime",
        "https://key@example.com/runtime",
        "https://example.com/runtime?token=secret",
    ):
        with pytest.raises(ValueError):
            install_runtime(archive=None, url=url, cache=tmp_path)


def test_private_environment_is_literal_and_rejects_public_file(tmp_path, monkeypatch):
    from openevo_chemcrow.workflow import load_private_environment

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = tmp_path / "credential.env"
    path.write_text("OPENROUTER_API_KEY='$(do-not-execute)'\n")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_private_environment(path)
    path.chmod(0o600)
    load_private_environment(path)
    import os

    assert os.environ["OPENROUTER_API_KEY"] == "$(do-not-execute)"


def test_substitution_requires_reason_and_binds_every_role(tmp_path):
    import yaml

    from openevo_chemcrow.cli import _assert_authoritative_role_models
    from openevo_chemcrow.submission import _role_configs

    with pytest.raises(ValueError):
        initialize(
            run_id="missing-reason",
            output_root=tmp_path,
            fixture_root=tmp_path,
            port_base=18080,
            model="gpt-5.6-sol",
        )
    root = initialize(
        run_id="substitute",
        output_root=tmp_path,
        fixture_root=tmp_path,
        port_base=18080,
        model="gpt-5.6-sol",
        substitution_reason="Historical model absent from current model catalog",
    )
    cfg = yaml.safe_load((root / "chemcrowrun.reproduction.yaml").read_text())
    roles = _role_configs(cfg)
    _assert_authoritative_role_models(roles, cfg["model_substitution"])
    with pytest.raises(ValueError):
        _assert_authoritative_role_models(roles)
    roles["candidate"]["agent"]["model_name"] = "different-model"
    with pytest.raises(ValueError):
        _assert_authoritative_role_models(roles, cfg["model_substitution"])


def test_workspace_rejects_changed_source_config_and_environment(tmp_path, monkeypatch):
    import json

    from openevo_chemcrow import chemcrowrun as module

    root = initialize(
        run_id="binding", output_root=tmp_path, fixture_root=tmp_path, port_base=18080
    )
    original = json.loads((root / "run.json").read_text())
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda args, **kwargs: "" if "status" in args else original["source_commit"],
    )
    module.verify_workspace_binding(root)
    config = root / "chemcrowrun.reproduction.yaml"
    original_config = config.read_text()
    config.write_text(original_config + "\n# changed\n")
    with pytest.raises(ValueError, match="initialized identity"):
        module.verify_workspace_binding(root)
    config.write_text(original_config)
    changed = dict(original)
    changed["environment"] = {**original["environment"], "CHEMCROW_RUN_ROOT": "/old"}
    (root / "run.json").write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="environment"):
        module.verify_workspace_binding(root)
    (root / "run.json").write_text(json.dumps({**original, "source_commit": "0" * 40}))
    with pytest.raises(ValueError, match="original clean source"):
        module.verify_workspace_binding(root)
