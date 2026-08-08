from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

_HTTP_EVIDENCE_REQUEST_HEADERS = frozenset(
    {
        "content-length",
        "content-type",
        "x-openevo-internal-generation",
        "x-openevo-internal-registry",
        "x-openevo-internal-service",
        "x-request-id",
    }
)
_HTTP_EVIDENCE_RESPONSE_HEADERS = frozenset(
    {
        "content-length",
        "content-type",
        "date",
        "server",
        "x-request-id",
    }
)
_SENSITIVE_FIELD_RE = re.compile(
    r"(?:authorization|bearer|api[_-]?key|oauth|cookie|password|secret|token|credential)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~+/-]+=*|\bsk-or-v1-[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_EVIDENCE_FILE_MODE = 0o600
_EVIDENCE_ROOT_MODE = 0o700


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _redact_http_evidence(value: Any, *, field_name: str | None = None) -> Any:
    if field_name is not None and _SENSITIVE_FIELD_RE.search(field_name):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(key): _redact_http_evidence(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_http_evidence(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_http_evidence(item) for item in value]
    if isinstance(value, str) and _SENSITIVE_VALUE_RE.search(value):
        return "<redacted>"
    return value


def _allowlisted_headers(
    headers: httpx.Headers,
    allowlist: frozenset[str],
) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in allowlist
    }


def _safe_endpoint_identity(base_url: str) -> str:
    parsed = urlsplit(base_url)
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _request_url_path(request: httpx.Request) -> str:
    return request.url.raw_path.decode("ascii")


def _decoded_http_body(content: bytes) -> Any:
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = content.decode("utf-8", errors="replace")
    return _redact_http_evidence(decoded)


def _write_private_json(root: Path, evidence_id: str, payload: Mapping[str, Any]) -> Path:
    root.mkdir(mode=_EVIDENCE_ROOT_MODE, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("planned-job HTTP evidence root is invalid")
    os.chmod(root, _EVIDENCE_ROOT_MODE)
    destination = root / f"{evidence_id}.json"
    temporary = root / f".{evidence_id}.{secrets.token_hex(8)}.tmp"
    body = _canonical_json_bytes(payload)
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        _EVIDENCE_FILE_MODE,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, _EVIDENCE_FILE_MODE)
        directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return destination


class RolloutClientProtocol(Protocol):
    def submit_task(self, payload: dict[str, Any]) -> str: ...

    def get_task(self, task_id: str) -> dict[str, Any]: ...

    def cancel_task(self, task_id: str) -> dict[str, Any]: ...


class EvolutionClientProtocol(Protocol):
    def get_internal_health(self) -> dict[str, Any]: ...

    def get_completed_dataset_authority(self, dataset_id: str) -> dict[str, Any]: ...

    def create_dataset(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_dataset(self, dataset_id: str) -> dict[str, Any]: ...

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def create_plan_bound_job(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def retry_plan_bound_job(
        self,
        job_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def get_artifact(self, artifact_id: str) -> dict[str, Any]: ...

    def create_training_feedback_attachment(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_training_feedback_attachment(self, attachment_id: str) -> dict[str, Any]: ...

    def list_training_feedback_attachments_for_session(
        self, session_id: str
    ) -> dict[str, Any]: ...

    def resolve_evolution_dataset_view(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_context_runtime_authority(self, context_id: str) -> dict[str, Any]: ...

    def create_materialized_context(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_materialized_context(self, context_id: str) -> dict[str, Any]: ...

    def get_internal_successor_materialized_context(
        self,
        successor_transition_id: str,
        request_digest: str,
    ) -> dict[str, Any]: ...

    def get_internal_job_result(self, job_id: str) -> dict[str, Any]: ...

    def get_internal_failed_plan_bound_job_authority(
        self,
        job_id: str,
    ) -> dict[str, Any]: ...

    def get_internal_succeeded_plan_bound_job_authority(
        self,
        successor_transition_id: str,
        target_id: str,
    ) -> dict[str, Any]: ...

    def get_internal_successor_transition_job_inventory(
        self,
        successor_transition_id: str,
    ) -> dict[str, Any]: ...

    def get_internal_reflector_inference_reservation(
        self,
        job_id: str,
    ) -> dict[str, Any]: ...

    def get_internal_successor_artifact(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> dict[str, Any]: ...

    def get_internal_successor_artifact_authority(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> dict[str, Any]: ...

    def discard_successor_transition_outputs(
        self,
        successor_transition_id: str,
    ) -> dict[str, Any]: ...

    def update_artifact_promotion(
        self,
        artifact_id: str,
        *,
        promoted: bool,
    ) -> dict[str, Any]: ...

    def apply_internal_artifact_admission(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def create_review_request(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def list_review_requests(self, **filters: Any) -> list[dict[str, Any]]: ...

    def submit_human_feedback(
        self,
        review_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def list_human_feedback(self, *, review_id: str) -> list[dict[str, Any]]: ...

    def create_feedback_application(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def create_human_query_decision(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class RolloutHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        timeout = (
            httpx.Timeout(timeout_seconds, connect=30.0)
            if timeout_seconds
            else httpx.Timeout(None, connect=30.0)
        )
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            trust_env=False,
            headers=dict(headers or {}),
            transport=transport,
        )

    def __enter__(self) -> RolloutHttpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def submit_task(self, payload: dict[str, Any]) -> str:
        response = self._client.post(f"{self.base_url}/rollout/task/submit", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("rollout submit response was not a JSON object")
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("rollout submit response did not include task_id")
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any]:
        encoded_task_id = quote(task_id, safe="")
        response = self._client.get(f"{self.base_url}/rollout/task/{encoded_task_id}")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("rollout task response was not a JSON object")
        return result

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        encoded_task_id = quote(task_id, safe="")
        response = self._client.delete(f"{self.base_url}/rollout/task/{encoded_task_id}")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("rollout cancellation response was not a JSON object")
        if result.get("task_id") != task_id or result.get("status") != "cancelled":
            raise ValueError("rollout cancellation response did not prove termination")
        return result


class EvolutionHttpStatusError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        detail_code: str = "unspecified",
        diagnostic_evidence_id: str | None = None,
        diagnostic_evidence_sha256: str | None = None,
        validation_detail_present: bool = False,
        request_method: str | None = None,
        request_path: str | None = None,
    ) -> None:
        if type(status_code) is not int or not 100 <= status_code <= 599:
            raise ValueError("evolution HTTP status code is invalid")
        if detail_code not in {
            "conflict",
            "core_config_contains_non_core_owned_fields",
            "not_found",
            "request_validation_failed",
            "server_failure",
            "unspecified",
        }:
            raise ValueError("evolution HTTP detail code is invalid")
        self.status_code = status_code
        self.detail_code = detail_code
        self.diagnostic_evidence_id = diagnostic_evidence_id
        self.diagnostic_evidence_sha256 = diagnostic_evidence_sha256
        self.validation_detail_present = validation_detail_present
        self.request_method = request_method
        self.request_path = request_path
        self.retryable = status_code >= 500 or status_code in {408, 425, 429}
        super().__init__(
            f"evolution service returned HTTP status {status_code} ({detail_code})"
        )


class EvolutionHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        planned_job_evidence_root: str | Path | None = None,
        planned_job_evidence_context: Mapping[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._planned_job_evidence_root = (
            None
            if planned_job_evidence_root is None
            else Path(planned_job_evidence_root).absolute()
        )
        self._planned_job_evidence_context = _redact_http_evidence(
            dict(planned_job_evidence_context or {})
        )
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            trust_env=False,
            headers=dict(headers or {}),
            transport=transport,
        )

    def __enter__(self) -> EvolutionHttpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_internal_health(self) -> dict[str, Any]:
        response = self._client.get(f"{self.base_url}/v1/health")
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise TypeError("evolution health response was not a JSON object")
        return result

    def _raise_for_status(
        self,
        response: httpx.Response,
        *,
        diagnostic_evidence_id: str | None = None,
        diagnostic_evidence_sha256: str | None = None,
    ) -> None:
        if 200 <= response.status_code < 300:
            return
        detail_code = "unspecified"
        try:
            body = response.json()
        except (TypeError, ValueError):
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        if (
            isinstance(detail, str)
            and detail.startswith("core config may only contain Core-owned fields:")
        ):
            detail_code = "core_config_contains_non_core_owned_fields"
        elif response.status_code == 404:
            detail_code = "not_found"
        elif response.status_code == 409:
            detail_code = "conflict"
        elif response.status_code == 422:
            detail_code = "request_validation_failed"
        elif response.status_code >= 500:
            detail_code = "server_failure"
        if (
            diagnostic_evidence_id is None
            and diagnostic_evidence_sha256 is None
            and self._planned_job_evidence_root is not None
        ):
            (
                diagnostic_evidence_id,
                diagnostic_evidence_sha256,
            ) = self._record_http_response_evidence(
                request=response.request,
                response=response,
                evidence_kind="evolution-error",
            )
        raise EvolutionHttpStatusError(
            status_code=response.status_code,
            detail_code=detail_code,
            diagnostic_evidence_id=diagnostic_evidence_id,
            diagnostic_evidence_sha256=diagnostic_evidence_sha256,
            validation_detail_present=detail is not None,
            request_method=response.request.method,
            request_path=_request_url_path(response.request),
        )

    def _record_http_request_evidence(
        self,
        *,
        request: httpx.Request,
    ) -> tuple[str | None, str | None]:
        root = self._planned_job_evidence_root
        if root is None:
            return None, None
        request_body = bytes(request.content)
        request_sha256 = hashlib.sha256(request_body).hexdigest()
        try:
            request_json = json.loads(request_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("planned-job request was not serialized JSON") from exc
        recorded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        identity_seed = _canonical_json_bytes(
            {
                "phase": "pre_dispatch",
                "recorded_at": recorded_at,
                "request_sha256": request_sha256,
                "nonce": secrets.token_hex(16),
            }
        )
        evidence_id = (
            "planned-job-http-pre-"
            f"{hashlib.sha256(identity_seed).hexdigest()}"
        )
        evidence = {
            "schema_version": "openevo.planned_job_http_evidence.v2",
            "evidence_id": evidence_id,
            "phase": "pre_dispatch",
            "recorded_at": recorded_at,
            "endpoint_identity": {
                "base_url": _safe_endpoint_identity(self.base_url),
                "context": self._planned_job_evidence_context,
            },
            "request": {
                "method": request.method,
                "url_path": _request_url_path(request),
                "headers": _allowlisted_headers(
                    request.headers,
                    _HTTP_EVIDENCE_REQUEST_HEADERS,
                ),
                "json": _redact_http_evidence(request_json),
                "sha256": request_sha256,
            },
            "response": None,
            "parsed_error": None,
            "secret_recorded": False,
        }
        evidence_sha256 = hashlib.sha256(_canonical_json_bytes(evidence)).hexdigest()
        evidence["content_sha256"] = evidence_sha256
        _write_private_json(root, evidence_id, evidence)
        return evidence_id, evidence_sha256

    def _record_http_response_evidence(
        self,
        *,
        request: httpx.Request,
        response: httpx.Response,
        evidence_kind: str,
        pre_dispatch_evidence: tuple[str | None, str | None] = (None, None),
    ) -> tuple[str | None, str | None]:
        root = self._planned_job_evidence_root
        if root is None:
            return None, None
        request_body = bytes(request.content)
        response_body = bytes(response.content)
        request_sha256 = hashlib.sha256(request_body).hexdigest()
        response_sha256 = hashlib.sha256(response_body).hexdigest()
        if request_body:
            try:
                request_json = json.loads(request_body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                request_json = request_body.decode("utf-8", errors="replace")
        else:
            request_json = None
        parsed_response = _decoded_http_body(response_body)
        detail = (
            parsed_response.get("detail")
            if isinstance(parsed_response, dict)
            else None
        )
        recorded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        identity_seed = _canonical_json_bytes(
            {
                "recorded_at": recorded_at,
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "nonce": secrets.token_hex(16),
            }
        )
        prefix = (
            "planned-job-http"
            if evidence_kind == "planned-job"
            else "evolution-http"
        )
        evidence_id = f"{prefix}-{hashlib.sha256(identity_seed).hexdigest()}"
        evidence = {
            "schema_version": "openevo.evolution_http_evidence.v2",
            "evidence_id": evidence_id,
            "phase": "response",
            "evidence_kind": evidence_kind,
            "recorded_at": recorded_at,
            "endpoint_identity": {
                "base_url": _safe_endpoint_identity(self.base_url),
                "context": self._planned_job_evidence_context,
            },
            "request": {
                "method": request.method,
                "url_path": _request_url_path(request),
                "headers": _allowlisted_headers(
                    request.headers,
                    _HTTP_EVIDENCE_REQUEST_HEADERS,
                ),
                "json": _redact_http_evidence(request_json),
                "sha256": request_sha256,
            },
            "response": {
                "http_status": response.status_code,
                "headers": _allowlisted_headers(
                    response.headers,
                    _HTTP_EVIDENCE_RESPONSE_HEADERS,
                ),
                "body": parsed_response,
                "sha256": response_sha256,
            },
            "parsed_error": {
                "detail": detail,
                "validation_detail_present": detail is not None,
            },
            "pre_dispatch_evidence": {
                "evidence_id": pre_dispatch_evidence[0],
                "content_sha256": pre_dispatch_evidence[1],
            },
            "secret_recorded": False,
        }
        evidence_sha256 = hashlib.sha256(_canonical_json_bytes(evidence)).hexdigest()
        evidence["content_sha256"] = evidence_sha256
        _write_private_json(root, evidence_id, evidence)
        return evidence_id, evidence_sha256

    def _send_planned_job_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any],
    ) -> httpx.Response:
        request = self._client.build_request(
            method,
            f"{self.base_url}{path}",
            json=payload,
        )
        pre_dispatch_evidence = self._record_http_request_evidence(request=request)
        response = self._client.send(request)
        evidence_id, evidence_sha256 = self._record_http_response_evidence(
            request=request,
            response=response,
            evidence_kind="planned-job",
            pre_dispatch_evidence=pre_dispatch_evidence,
        )
        self._raise_for_status(
            response,
            diagnostic_evidence_id=evidence_id,
            diagnostic_evidence_sha256=evidence_sha256,
        )
        return response

    def create_dataset(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/datasets", json=payload)
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution dataset response was not a JSON object")
        return result

    def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        encoded_dataset_id = quote(dataset_id, safe="")
        response = self._client.get(f"{self.base_url}/v1/datasets/{encoded_dataset_id}")
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution dataset response was not a JSON object")
        return result

    def get_completed_dataset_authority(self, dataset_id: str) -> dict[str, Any]:
        encoded = quote(dataset_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/training-feedback/datasets/{encoded}"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("completed dataset authority was not a JSON object")
        return result

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/jobs", json=payload)
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution job response was not a JSON object")
        return result

    def create_plan_bound_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._send_planned_job_request(
            "POST",
            "/v1/planned-jobs",
            payload=payload,
        )
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("planned evolution job response was not a JSON object")
        return result

    def retry_plan_bound_job(
        self,
        job_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        encoded_job_id = quote(job_id, safe="")
        response = self._send_planned_job_request(
            "POST",
            f"/v1/planned-jobs/{encoded_job_id}/retry",
            payload=payload,
        )
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("planned evolution job retry response was not a JSON object")
        return result

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        encoded_artifact_id = quote(artifact_id, safe="")
        response = self._client.get(f"{self.base_url}/v1/artifacts/{encoded_artifact_id}")
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution artifact response was not a JSON object")
        return result

    def create_training_feedback_attachment(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/internal/training-feedback/attachments",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("training feedback response was not a JSON object")
        return result

    def get_training_feedback_attachment(self, attachment_id: str) -> dict[str, Any]:
        encoded = quote(attachment_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/training-feedback/attachments/{encoded}"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("training feedback response was not a JSON object")
        return result

    def list_training_feedback_attachments_for_session(self, session_id: str) -> dict[str, Any]:
        encoded = quote(session_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/training-feedback/sessions/{encoded}/attachments"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("training feedback list was not a JSON object")
        return result

    def resolve_evolution_dataset_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/internal/training-feedback/resolve",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("resolved evolution dataset view was not a JSON object")
        return result

    def get_context_runtime_authority(self, context_id: str) -> dict[str, Any]:
        encoded_context_id = quote(context_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/contexts/{encoded_context_id}/runtime-authority"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("context runtime authority was not a JSON object")
        return result

    def create_materialized_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/internal/materialized-contexts",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("materialized context response was not a JSON object")
        return result

    def get_materialized_context(self, context_id: str) -> dict[str, Any]:
        encoded_context_id = quote(context_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/materialized-contexts/{encoded_context_id}"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("materialized context response was not a JSON object")
        return result

    def get_internal_successor_materialized_context(
        self,
        successor_transition_id: str,
        request_digest: str,
    ) -> dict[str, Any]:
        encoded_transition = quote(successor_transition_id, safe="")
        encoded_digest = quote(request_digest, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/successor-transitions/"
            f"{encoded_transition}/materialized-contexts/by-request/"
            f"{encoded_digest}"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(
                "successor materialized context response was not an object"
            )
        return result

    def get_internal_job_result(self, job_id: str) -> dict[str, Any]:
        encoded_job_id = quote(job_id, safe="")
        response = self._client.get(f"{self.base_url}/v1/internal/jobs/{encoded_job_id}")
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution job result was not a JSON object")
        return result

    def get_internal_failed_plan_bound_job_authority(
        self,
        job_id: str,
    ) -> dict[str, Any]:
        encoded_job_id = quote(job_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/jobs/{encoded_job_id}/failed-plan-authority"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("failed plan-bound job authority was not a JSON object")
        return result

    def get_internal_succeeded_plan_bound_job_authority(
        self,
        successor_transition_id: str,
        target_id: str,
    ) -> dict[str, Any]:
        encoded_transition_id = quote(successor_transition_id, safe="")
        encoded_target_id = quote(target_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/successor-transitions/"
            f"{encoded_transition_id}/targets/{encoded_target_id}/"
            "succeeded-plan-authority"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("succeeded plan-bound job authority was not an object")
        return result

    def get_internal_successor_transition_job_inventory(
        self,
        successor_transition_id: str,
    ) -> dict[str, Any]:
        encoded_transition_id = quote(successor_transition_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/successor-transitions/"
            f"{encoded_transition_id}/jobs"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("successor transition job inventory was not a JSON object")
        return result

    def get_internal_reflector_inference_reservation(
        self,
        job_id: str,
    ) -> dict[str, Any]:
        encoded_job_id = quote(job_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/jobs/{encoded_job_id}/reflector-inference"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("reflector inference reservation was not a JSON object")
        return result

    def apply_internal_artifact_admission(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/internal/artifact-admission/decisions",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("artifact admission response was not a JSON object")
        return result

    def get_internal_successor_artifact(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        encoded_transition_id = quote(
            successor_transition_id,
            safe="",
        )
        encoded_artifact_id = quote(artifact_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/successor-transitions/"
            f"{encoded_transition_id}/artifacts/{encoded_artifact_id}"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("successor transition artifact was not a JSON object")
        return result

    def get_internal_successor_artifact_authority(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        encoded_transition_id = quote(successor_transition_id, safe="")
        encoded_artifact_id = quote(artifact_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/successor-transitions/"
            f"{encoded_transition_id}/artifacts/{encoded_artifact_id}/authority"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("successor artifact authority was not a JSON object")
        return result

    def discard_successor_transition_outputs(
        self,
        successor_transition_id: str,
    ) -> dict[str, Any]:
        encoded_transition_id = quote(
            successor_transition_id,
            safe="",
        )
        response = self._client.post(
            f"{self.base_url}/v1/internal/successor-transitions/{encoded_transition_id}/discard"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("successor transition discard was not a JSON object")
        return result

    def update_artifact_promotion(
        self,
        artifact_id: str,
        *,
        promoted: bool,
    ) -> dict[str, Any]:
        encoded_artifact_id = quote(artifact_id, safe="")
        response = self._client.patch(
            f"{self.base_url}/v1/artifacts/{encoded_artifact_id}/promotion",
            json={"promoted": promoted},
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution artifact promotion response was not a JSON object")
        return result

    def create_review_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/reviews", json=payload)
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution review response was not a JSON object")
        return result

    def list_review_requests(self, **filters: Any) -> list[dict[str, Any]]:
        params = {key: value for key, value in filters.items() if value is not None}
        response = self._client.get(f"{self.base_url}/v1/reviews", params=params)
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, list):
            raise ValueError("evolution review list response was not a JSON array")
        return result

    def submit_human_feedback(
        self,
        review_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        encoded_review_id = quote(review_id, safe="")
        response = self._client.post(
            f"{self.base_url}/v1/reviews/{encoded_review_id}/feedback",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution human feedback response was not a JSON object")
        return result

    def list_human_feedback(self, *, review_id: str) -> list[dict[str, Any]]:
        encoded_review_id = quote(review_id, safe="")
        response = self._client.get(f"{self.base_url}/v1/reviews/{encoded_review_id}/feedback")
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, list):
            raise ValueError("evolution human feedback list response was not a JSON array")
        return result

    def create_feedback_application(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/feedback-applications",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution feedback application response was not a JSON object")
        return result

    def create_human_query_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/query-decisions", json=payload)
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution query decision response was not a JSON object")
        return result
