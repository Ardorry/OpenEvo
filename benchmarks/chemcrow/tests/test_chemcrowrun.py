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


def test_service_readiness_rejects_another_workspaces_evolution(tmp_path, monkeypatch):
    import httpx

    from openevo_chemcrow import chemcrowrun as module

    root = initialize(
        run_id="identity", output_root=tmp_path, fixture_root=tmp_path, port_base=18080
    )
    artifact_root = str(root / "state/artifacts")

    def handler(request):
        payload = {"status": "ok"}
        if request.url.path == "/v1/health":
            payload = {"db": "ok", "artifact_root": artifact_root}
        elif request.url.path == "/nodes":
            payload = [{"healthy": True, "node_id": "chemcrowrun-identity"}]
        elif request.url.path == "/openapi.json":
            payload = {"paths": {"/product_prediction": {}, "/retro_prediction": {}}}
        return httpx.Response(200, json=payload)

    original_client = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    assert module.services_status(root)["status"] == "READY"
    artifact_root = "/another-experiment/artifacts"
    assert module.services_status(root)["status"] == "BLOCKED"


@pytest.mark.parametrize(
    "price,credit,expected",
    [
        ("0.001", 20, "price/context"),
        ("0.00003", 1, "available credit"),
        ("0.00003", 20, "preflight reached"),
    ],
)
def test_judge_guard_stops_before_any_paid_request(tmp_path, monkeypatch, price, credit, expected):
    import json

    import httpx

    from openevo_chemcrow import workflow

    monkeypatch.setattr(workflow, "verify_workspace_binding", lambda root: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-credential")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:10809")
    credential = tmp_path / "private.env"
    credential.write_text("# existing process credentials\n")
    credential.chmod(0o600)
    (tmp_path / "run.json").write_text(
        json.dumps(
            {
                "environment": {"CHEMCROW_EXPERIMENT_ID": "test-experiment"},
                "run_id": "test",
                "fixture_root": str(tmp_path / "fixtures"),
            }
        )
    )

    def preflight(arguments):
        import yaml

        config = yaml.safe_load((tmp_path / "judge/judge.yaml").read_text())
        assert config["completed_run_audit"] == str(tmp_path / "runs/completed_run.audit.json")
        assert arguments[0] == "paper-evaluator-preflight"
        raise ValueError("preflight reached")

    monkeypatch.setattr(workflow, "invoke", preflight)
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        data = {"total_credits": credit, "total_usage": 0}
        if request.url.path.endswith("/models"):
            data = [
                {
                    "id": "openai/gpt-4",
                    "pricing": {"prompt": price, "completion": "0.00006"},
                    "context_length": 8191,
                }
            ]
        return httpx.Response(200, json={"data": data})

    original_client = httpx.Client
    monkeypatch.setattr(
        workflow.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError, match=expected):
        workflow.run_judge(tmp_path, credential_file=credential, budget_usd=12, allow_paid=True)
    assert requests and all(method == "GET" for method, path in requests)


def test_audit_cli_uses_declared_isolated_ledger(tmp_path, monkeypatch):
    import argparse
    import os

    from openevo_chemcrow import cli
    from openevo_chemcrow.chemcrowrun import environment

    root = initialize(
        run_id="audit-path", output_root=tmp_path, fixture_root=tmp_path, port_base=18080
    )
    monkeypatch.setattr(os, "environ", environment(root))
    observed = {}

    def audit(**kwargs):
        observed.update(kwargs)
        return {"status": "PASS", "task_count": 1}

    monkeypatch.setattr(cli, "audit_three_artifact_run", audit)
    args = argparse.Namespace(
        config=root / "chemcrowrun.reproduction.yaml",
        core_completions=root / "state/completions",
        stop_after_task_id="chemcrow-01",
        output=root / "audit.json",
    )
    assert cli.command_audit_run(args) == 0
    assert observed["ledger_root"] == root / "claims"


def test_post_run_comparison_requires_complete_standardized_evidence(tmp_path):
    from openevo_chemcrow.chemcrowrun import PACKAGE
    from openevo_chemcrow.comparison import compare_submission

    example = PACKAGE / "reproduction/examples/FORMAT_ONLY.synthetic-submission.json"
    comparison = compare_submission(example)
    stats = comparison["internal_blind_12_point"]
    assert len(stats["per_task"]) == 14
    assert stats["new_g1_g2"]["n"] == 14
    assert comparison["paper_10_point"] == "PENDING"
    assert comparison["raw_answers_included"] is False
    with pytest.raises(ValueError, match="42 distinct sealed"):
        compare_submission(example, paper_results=tmp_path)
    incomplete = tmp_path / "invalid-submission.json"
    incomplete.write_text("{}")
    with pytest.raises(ValueError):
        compare_submission(incomplete)


def test_canonical_config_defines_current_protocol():
    import yaml

    package = Path(__file__).parents[1]
    config = yaml.safe_load((package / "configs/chemcrowrun.reproduction.yaml").read_text())
    assert config["execution_scope"] == "full_g1_g2_pair"
    assert config["artifact_protocol"] == "three_isolated_core_native_v2"
    assert config["task_ids"] == "all"
    assert set(config["reflectors"]) == {"memory", "skill_bundle", "agent_system"}
