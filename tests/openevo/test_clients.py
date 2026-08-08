from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from openevo import experiments

RolloutHttpClient = experiments.RolloutHttpClient
EvolutionHttpClient = experiments.EvolutionHttpClient
EvolutionHttpStatusError = experiments.EvolutionHttpStatusError


def test_rollout_http_client_url_encodes_task_id_path_segment() -> None:
    captured_paths: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.raw_path)
        return httpx.Response(
            200,
            json={"task_id": "bench?x#frag", "status": "completed"},
        )

    client = RolloutHttpClient(
        "http://rollout.example",
        transport=httpx.MockTransport(handler),
    )

    result = client.get_task("bench?x#frag")

    assert result["status"] == "completed"
    assert captured_paths == [b"/rollout/task/bench%3Fx%23frag"]


def test_rollout_http_client_cancel_requires_exact_terminal_authority() -> None:
    captured: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, request.url.raw_path))
        return httpx.Response(
            200,
            json={"task_id": "task?cancel#exact", "status": "cancelled"},
        )

    client = RolloutHttpClient(
        "http://rollout.example",
        transport=httpx.MockTransport(handler),
    )

    assert client.cancel_task("task?cancel#exact") == {
        "task_id": "task?cancel#exact",
        "status": "cancelled",
    }
    assert captured == [("DELETE", b"/rollout/task/task%3Fcancel%23exact")]


def test_rollout_http_client_rejects_non_object_submit_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    client = RolloutHttpClient(
        "http://rollout.example",
        transport=httpx.MockTransport(handler),
    )

    try:
        client.submit_task({"task_id": "task-a"})
    except ValueError as exc:
        assert "rollout submit response was not a JSON object" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_internal_clients_attach_generation_bound_headers_to_every_request() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers))
        if request.url.path == "/rollout/task/task-a":
            return httpx.Response(200, json={"task_id": "task-a", "status": "completed"})
        return httpx.Response(200, json={"artifact_id": "artifact-a"})

    headers = {
        "Authorization": "Bearer private-generation-credential",
        "X-OpenEvo-Internal-Generation": "a" * 64,
        "X-OpenEvo-Internal-Registry": "b" * 64,
        "X-OpenEvo-Internal-Service": "core-control",
    }
    transport = httpx.MockTransport(handler)
    rollout = RolloutHttpClient(
        "http://127.0.0.1:18100",
        headers=headers,
        transport=transport,
    )
    evolution = EvolutionHttpClient(
        "http://127.0.0.1:18200",
        headers=headers,
        transport=transport,
    )

    rollout.get_task("task-a")
    evolution.get_artifact("artifact-a")

    assert len(captured) == 2
    assert all(
        item["authorization"] == "Bearer private-generation-credential"
        and item["x-openevo-internal-service"] == "core-control"
        for item in captured
    )


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [
        (409, False),
        (422, False),
        (429, True),
        (503, True),
    ],
)
def test_evolution_http_client_exposes_closed_retryability(
    status_code: int,
    retryable: bool,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "remote failure"})

    client = EvolutionHttpClient(
        "http://evolution.example",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(EvolutionHttpStatusError) as captured:
        client.create_dataset({"name": "dataset"})

    assert captured.value.status_code == status_code
    assert captured.value.retryable is retryable


def test_evolution_http_client_classifies_core_config_failure_without_detail_leak() -> None:
    private_detail = (
        "core config may only contain Core-owned fields: "
        "content_admission_basis_sha256; private-value-must-not-escape"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": private_detail})

    client = EvolutionHttpClient(
        "http://evolution.example",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(EvolutionHttpStatusError) as captured:
        client.create_dataset({"name": "dataset"})

    assert captured.value.detail_code == "core_config_contains_non_core_owned_fields"
    assert private_detail not in str(captured.value)


def test_planned_job_422_preserves_redacted_durable_http_evidence(tmp_path) -> None:
    private_authorization = "Bearer private-generation-credential"
    private_api_key = "private-api-key-must-not-be-recorded"
    response_detail = [
        {
            "loc": ["body", "plan", "plan_id"],
            "msg": "Value error, plan identity is invalid",
            "type": "value_error",
            "input": {"api_key": private_api_key, "plan_id": "invalid-plan"},
        }
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": response_detail})

    client = EvolutionHttpClient(
        "http://evolution.example",
        headers={
            "Authorization": private_authorization,
            "X-OpenEvo-Internal-Generation": "a" * 64,
            "X-OpenEvo-Internal-Registry": "b" * 64,
            "X-OpenEvo-Internal-Service": "core-control",
        },
        transport=httpx.MockTransport(handler),
        planned_job_evidence_root=tmp_path / "planned-job-http",
        planned_job_evidence_context={
            "source_task_id": "task-a",
            "source_project_head_id": "project-head-a",
            "credential_note": private_api_key,
        },
    )

    with pytest.raises(EvolutionHttpStatusError) as captured:
        client.create_plan_bound_job(
            {
                "plan": {"plan_id": "invalid-plan"},
                "target_id": "skill_bundle",
            }
        )

    evidence_files = list((tmp_path / "planned-job-http").glob("*.json"))
    assert len(evidence_files) == 2
    pre_dispatch_file = next(
        item for item in evidence_files if item.name.startswith("planned-job-http-pre-")
    )
    pre_dispatch = json.loads(pre_dispatch_file.read_bytes())
    assert pre_dispatch["phase"] == "pre_dispatch"
    assert pre_dispatch["response"] is None
    evidence_file = next(
        item
        for item in evidence_files
        if item.name.startswith("planned-job-http-")
        and not item.name.startswith("planned-job-http-pre-")
    )
    evidence_bytes = evidence_file.read_bytes()
    evidence = json.loads(evidence_bytes)
    assert evidence_file.stat().st_mode & 0o777 == 0o600
    assert evidence["request"]["method"] == "POST"
    assert evidence["request"]["url_path"] == "/v1/planned-jobs"
    assert "authorization" not in evidence["request"]["headers"]
    assert evidence["request"]["sha256"] == hashlib.sha256(
        httpx.Request(
            "POST",
            "http://evolution.example/v1/planned-jobs",
            json={
                "plan": {"plan_id": "invalid-plan"},
                "target_id": "skill_bundle",
            },
        ).content
    ).hexdigest()
    assert evidence["response"]["http_status"] == 422
    assert evidence["response"]["sha256"]
    assert evidence["response"]["body"]["detail"][0]["loc"] == [
        "body",
        "plan",
        "plan_id",
    ]
    assert evidence["response"]["body"]["detail"][0]["input"]["api_key"] == (
        "<redacted>"
    )
    assert evidence["parsed_error"]["validation_detail_present"] is True
    assert evidence["pre_dispatch_evidence"] == {
        "evidence_id": pre_dispatch["evidence_id"],
        "content_sha256": pre_dispatch["content_sha256"],
    }
    assert evidence["endpoint_identity"]["context"]["credential_note"] == (
        "<redacted>"
    )
    assert evidence["secret_recorded"] is False
    assert private_authorization.encode() not in evidence_bytes
    assert private_api_key.encode() not in evidence_bytes
    assert captured.value.status_code == 422
    assert captured.value.validation_detail_present is True
    assert captured.value.diagnostic_evidence_id == evidence["evidence_id"]
    assert captured.value.diagnostic_evidence_sha256 == evidence["content_sha256"]
    assert captured.value.request_method == "POST"
    assert captured.value.request_path == "/v1/planned-jobs"
    assert private_api_key not in str(captured.value)


def test_planned_job_retry_422_preserves_exact_path_and_validation_detail(
    tmp_path,
) -> None:
    response_detail = "non-retryable plan-bound job requires a replacement plan"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": response_detail})

    client = EvolutionHttpClient(
        "http://evolution.example",
        transport=httpx.MockTransport(handler),
        planned_job_evidence_root=tmp_path / "planned-job-http",
    )

    with pytest.raises(EvolutionHttpStatusError) as captured:
        client.retry_plan_bound_job(
            "job-a?private-fragment",
            {
                "retry_request_id": "successor-attempt-a",
                "plan_id": "plan-a",
                "target_id": "agent_system",
            },
        )

    evidence_file = next(
        item
        for item in (tmp_path / "planned-job-http").glob("*.json")
        if not item.name.startswith("planned-job-http-pre-")
    )
    evidence = json.loads(evidence_file.read_bytes())
    assert evidence["request"]["url_path"] == (
        "/v1/planned-jobs/job-a%3Fprivate-fragment/retry"
    )
    assert evidence["response"]["body"]["detail"] == response_detail
    assert evidence["parsed_error"]["validation_detail_present"] is True
    assert captured.value.request_method == "POST"
    assert captured.value.request_path == (
        "/v1/planned-jobs/job-a%3Fprivate-fragment/retry"
    )
    assert captured.value.validation_detail_present is True


def test_non_planned_evolution_422_preserves_redacted_durable_http_evidence(
    tmp_path,
) -> None:
    response_detail = "dataset source authority is invalid"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": response_detail})

    client = EvolutionHttpClient(
        "http://evolution.example",
        headers={"Authorization": "Bearer private-generation-credential"},
        transport=httpx.MockTransport(handler),
        planned_job_evidence_root=tmp_path / "planned-job-http",
        planned_job_evidence_context={"source_task_id": "task-a"},
    )

    with pytest.raises(EvolutionHttpStatusError) as captured:
        client.create_dataset({"name": "dataset-a"})

    evidence_file = next((tmp_path / "planned-job-http").glob("evolution-http-*.json"))
    evidence_bytes = evidence_file.read_bytes()
    evidence = json.loads(evidence_bytes)
    assert evidence["evidence_kind"] == "evolution-error"
    assert evidence["request"]["method"] == "POST"
    assert evidence["request"]["url_path"] == "/v1/datasets"
    assert evidence["request"]["json"] == {"name": "dataset-a"}
    assert "authorization" not in evidence["request"]["headers"]
    assert evidence["response"]["http_status"] == 422
    assert evidence["response"]["body"] == {"detail": response_detail}
    assert evidence["parsed_error"]["validation_detail_present"] is True
    assert evidence["secret_recorded"] is False
    assert b"private-generation-credential" not in evidence_bytes
    assert captured.value.diagnostic_evidence_id == evidence["evidence_id"]
    assert captured.value.diagnostic_evidence_sha256 == evidence["content_sha256"]
    assert captured.value.request_method == "POST"
    assert captured.value.request_path == "/v1/datasets"
