"""Private canonical rule evidence for batch-level Temperature reflection."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes

REFLECTION_SCHEMA = "TemperatureBatchReflectionV1"
EVIDENCE_SCHEMA = "TemperatureRuleEvidenceIndexV1"
_UID = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RULE_ID = re.compile(r"rule-[0-9a-f]{24}\Z", re.ASCII)
_TARGET_BY_KIND = {
    "category_knowledge": "text_memory",
    "workflow": "skill_bundle",
    "behavior": "agent_system",
}


class TemperatureEvidenceError(RuntimeError):
    """Closed private-evidence validation error."""


def _one_line(value: str, *, field_name: str, maximum_bytes: int = 768) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be text")
    normalized = " ".join(value.split())
    if not normalized or len(normalized.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{field_name} is empty or too large")
    if "\x00" in normalized:
        raise ValueError(f"{field_name} contains NUL")
    return normalized


class ReflectionProposalV1(BaseModel):
    """One model proposal; the controller, not the model, derives its evidence state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["add", "update", "retire"]
    rule_id: str | None = None
    kind: Literal["category_knowledge", "workflow", "behavior"]
    content: str
    applicability: str
    validation: str
    supporting_train_uids: tuple[str, ...]
    counterexample_train_uids: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)
    retirement_reason: str | None = None

    @field_validator("rule_id")
    @classmethod
    def _rule_id(cls, value: str | None) -> str | None:
        if value is not None and _RULE_ID.fullmatch(value) is None:
            raise ValueError("rule_id is invalid")
        return value

    @field_validator("content", "applicability", "validation")
    @classmethod
    def _text(cls, value: str, info: Any) -> str:
        return _one_line(value, field_name=str(info.field_name))

    @field_validator("retirement_reason")
    @classmethod
    def _retirement_reason(cls, value: str | None) -> str | None:
        return None if value is None else _one_line(value, field_name="retirement_reason")

    @field_validator("supporting_train_uids", "counterexample_train_uids")
    @classmethod
    def _uids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values or any(_UID.fullmatch(value) is None for value in values):
            raise ValueError("evidence UIDs must be sorted unique SHA-256 values")
        return values

    @model_validator(mode="after")
    def _operation_and_evidence(self) -> ReflectionProposalV1:
        if (self.operation == "add") != (self.rule_id is None):
            raise ValueError("add must omit rule_id and update/retire must name one")
        if (self.operation == "retire") != (self.retirement_reason is not None):
            raise ValueError("only retire proposals must provide a retirement reason")
        if not self.supporting_train_uids:
            raise ValueError("a rule proposal needs supporting Train evidence")
        if set(self.supporting_train_uids) & set(self.counterexample_train_uids):
            raise ValueError("support and counterexample evidence overlap")
        return self


class BatchReflectionV1(BaseModel):
    """Exact JSON response accepted from the one-per-batch Reflector call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["TemperatureBatchReflectionV1"] = REFLECTION_SCHEMA
    batch_index: int = Field(ge=1, le=8)
    prior_evidence_sha256: str | None
    proposals: tuple[ReflectionProposalV1, ...] = Field(max_length=64)

    @field_validator("prior_evidence_sha256")
    @classmethod
    def _prior_digest(cls, value: str | None) -> str | None:
        if value is not None and _UID.fullmatch(value) is None:
            raise ValueError("prior evidence digest is invalid")
        return value


class CanonicalRuleV1(BaseModel):
    """Private cumulative evidence; UID references never enter projected artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    kind: Literal["category_knowledge", "workflow", "behavior"]
    content: str
    applicability: str
    validation: str
    supporting_train_uids: tuple[str, ...]
    counterexample_train_uids: tuple[str, ...]
    support_count: int = Field(ge=1, le=100)
    contradiction_count: int = Field(ge=0, le=100)
    first_seen_batch: int = Field(ge=1, le=8)
    last_validated_batch: int = Field(ge=1, le=8)
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["provisional", "confirmed", "retired"]
    target_projection: Literal["text_memory", "skill_bundle", "agent_system", "none"]
    retirement_reason: str | None = None

    @field_validator("rule_id")
    @classmethod
    def _id(cls, value: str) -> str:
        if _RULE_ID.fullmatch(value) is None:
            raise ValueError("canonical rule ID is invalid")
        return value

    @field_validator("content", "applicability", "validation")
    @classmethod
    def _text(cls, value: str, info: Any) -> str:
        return _one_line(value, field_name=str(info.field_name))

    @field_validator("retirement_reason")
    @classmethod
    def _retirement_reason(cls, value: str | None) -> str | None:
        return None if value is None else _one_line(value, field_name="retirement_reason")

    @field_validator("supporting_train_uids", "counterexample_train_uids")
    @classmethod
    def _uids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values or any(_UID.fullmatch(value) is None for value in values):
            raise ValueError("canonical evidence UIDs are invalid")
        return values

    @model_validator(mode="after")
    def _derived_fields(self) -> CanonicalRuleV1:
        expected_target = _TARGET_BY_KIND[self.kind] if self.status == "confirmed" else "none"
        expected_confirmed = (
            self.support_count >= 2
            and self.contradiction_count < self.support_count
            and self.confidence >= 0.5
        )
        if (
            self.support_count != len(self.supporting_train_uids)
            or self.contradiction_count != len(self.counterexample_train_uids)
            or set(self.supporting_train_uids) & set(self.counterexample_train_uids)
            or self.first_seen_batch > self.last_validated_batch
            or self.target_projection != expected_target
            or (self.status == "confirmed" and not expected_confirmed)
            or (self.status == "provisional" and expected_confirmed)
            or (self.status == "retired") != (self.retirement_reason is not None)
        ):
            raise ValueError("canonical rule derived fields are inconsistent")
        return self


class RuleEvidenceIndexV1(BaseModel):
    """Immutable cumulative rule/evidence checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["TemperatureRuleEvidenceIndexV1"] = EVIDENCE_SCHEMA
    batch_index: int = Field(ge=0, le=8)
    rules: tuple[CanonicalRuleV1, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def _closed(self) -> RuleEvidenceIndexV1:
        ids = tuple(rule.rule_id for rule in self.rules)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("canonical rules must be sorted and unique")
        seen_semantics: set[tuple[str, str, str]] = set()
        for rule in self.rules:
            semantic = (
                rule.kind,
                rule.content.casefold(),
                rule.applicability.casefold(),
            )
            if semantic in seen_semantics:
                raise ValueError("canonical rules contain duplicate semantics")
            seen_semantics.add(semantic)
        return self

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))

    @classmethod
    def generation_zero(cls) -> RuleEvidenceIndexV1:
        return cls(batch_index=0, rules=())

    def to_reflector_payload(self) -> dict[str, object]:
        """Private payload: UID references are permitted only inside the Reflector boundary."""

        return self.model_dump(mode="json")

    def to_public_summary(self) -> dict[str, object]:
        statuses = {status: 0 for status in ("confirmed", "provisional", "retired")}
        kinds = {kind: 0 for kind in _TARGET_BY_KIND}
        support = 0
        contradictions = 0
        for rule in self.rules:
            statuses[rule.status] += 1
            kinds[rule.kind] += 1
            support += rule.support_count
            contradictions += rule.contradiction_count
        return {
            "schema_version": EVIDENCE_SCHEMA,
            "batch_index": self.batch_index,
            "rule_count": len(self.rules),
            "status_counts": statuses,
            "kind_counts": kinds,
            "support_reference_count": support,
            "counterexample_reference_count": contradictions,
            "evidence_sha256": self.digest,
        }


def parse_batch_reflection(response: str, *, maximum_utf8_bytes: int = 65_536) -> BatchReflectionV1:
    """Parse exact JSON, rejecting code fences, duplicate keys, and oversized responses."""

    if type(response) is not str or len(response.encode("utf-8")) > maximum_utf8_bytes:
        raise TemperatureEvidenceError("REFLECTOR_RESPONSE_SIZE_INVALID")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise TemperatureEvidenceError("REFLECTOR_RESPONSE_DUPLICATE_KEY")
            result[key] = value
        return result

    try:
        payload = json.loads(
            response,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                TemperatureEvidenceError("REFLECTOR_RESPONSE_NONFINITE")
            ),
        )
        # JSON Schema cannot express the canonical tuple ordering enforced by
        # the evidence DTO.  Sort model-supplied UID arrays deterministically
        # (without deduplicating them) so harmless transport ordering cannot
        # invalidate an otherwise identical synthesis.  Duplicate references
        # remain a closed evidence error.
        if isinstance(payload, dict) and isinstance(payload.get("proposals"), list):
            for proposal in payload["proposals"]:
                if not isinstance(proposal, dict):
                    continue
                for field_name in (
                    "supporting_train_uids",
                    "counterexample_train_uids",
                ):
                    values = proposal.get(field_name)
                    if not isinstance(values, list) or any(
                        type(value) is not str for value in values
                    ):
                        continue
                    if len(values) != len(set(values)):
                        raise TemperatureEvidenceError(
                            "REFLECTOR_RESPONSE_DUPLICATE_EVIDENCE_UID"
                        )
                    proposal[field_name] = sorted(values)
        return BatchReflectionV1.model_validate(payload)
    except TemperatureEvidenceError:
        raise
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise TemperatureEvidenceError("REFLECTOR_RESPONSE_SCHEMA_INVALID") from exc


def apply_batch_reflection(
    prior: RuleEvidenceIndexV1,
    reflection: BatchReflectionV1,
    *,
    all_train_uids: frozenset[str],
    seen_train_uids: frozenset[str],
    current_batch_uids: frozenset[str],
) -> RuleEvidenceIndexV1:
    """Merge one reflection without allowing omission to erase earlier evidence."""

    if type(prior) is not RuleEvidenceIndexV1 or type(reflection) is not BatchReflectionV1:
        raise TypeError("evidence merge requires exact DTOs")
    if reflection.batch_index != prior.batch_index + 1:
        raise TemperatureEvidenceError("EVIDENCE_BATCH_SEQUENCE_INVALID")
    expected_prior = None if prior.batch_index == 0 else prior.digest
    if reflection.prior_evidence_sha256 != expected_prior:
        raise TemperatureEvidenceError("EVIDENCE_PREDECESSOR_INVALID")
    for values, name in (
        (all_train_uids, "all"),
        (seen_train_uids, "seen"),
        (current_batch_uids, "current"),
    ):
        if type(values) is not frozenset or any(_UID.fullmatch(uid) is None for uid in values):
            raise TypeError(f"{name} Train UID set is invalid")
    if not current_batch_uids or not current_batch_uids <= seen_train_uids <= all_train_uids:
        raise TemperatureEvidenceError("EVIDENCE_TRAIN_SCOPE_INVALID")

    by_id = {rule.rule_id: rule for rule in prior.rules}
    touched: set[str] = set()
    for proposal in reflection.proposals:
        refs = set(proposal.supporting_train_uids) | set(proposal.counterexample_train_uids)
        if not refs <= seen_train_uids or not (refs & current_batch_uids):
            raise TemperatureEvidenceError("EVIDENCE_REFERENCE_OUTSIDE_VISIBLE_TRAIN")
        if proposal.operation == "add":
            rule_id = _new_rule_id(proposal)
            if rule_id in by_id:
                raise TemperatureEvidenceError("EVIDENCE_RULE_ID_COLLISION")
            prior_rule = None
        else:
            rule_id = str(proposal.rule_id)
            prior_rule = by_id.get(rule_id)
            if (
                prior_rule is None
                or prior_rule.kind != proposal.kind
                or prior_rule.status == "retired"
            ):
                raise TemperatureEvidenceError("EVIDENCE_RULE_UPDATE_INVALID")
        if rule_id in touched:
            raise TemperatureEvidenceError("EVIDENCE_RULE_TOUCHED_TWICE")
        touched.add(rule_id)

        support = tuple(
            sorted(
                set(proposal.supporting_train_uids)
                | (set() if prior_rule is None else set(prior_rule.supporting_train_uids))
            )
        )
        counterexamples = tuple(
            sorted(
                set(proposal.counterexample_train_uids)
                | (set() if prior_rule is None else set(prior_rule.counterexample_train_uids))
            )
        )
        # A newly contradictory reference removes the same reference from support.
        support = tuple(uid for uid in support if uid not in set(counterexamples))
        if not support:
            raise TemperatureEvidenceError("EVIDENCE_RULE_WITHOUT_SUPPORT")
        if proposal.operation == "retire" and prior_rule is not None:
            if (
                proposal.content != prior_rule.content
                or proposal.applicability != prior_rule.applicability
                or proposal.validation != prior_rule.validation
            ):
                raise TemperatureEvidenceError("EVIDENCE_RETIRE_REWRITE_FORBIDDEN")
            content = prior_rule.content
            applicability = prior_rule.applicability
            validation = prior_rule.validation
        else:
            content = proposal.content
            applicability = proposal.applicability
            validation = proposal.validation
        eligible_for_projection = (
            len(support) >= 2
            and len(counterexamples) < len(support)
            and proposal.confidence >= 0.5
        )
        status: Literal["provisional", "confirmed", "retired"] = (
            "retired"
            if proposal.operation == "retire"
            else ("confirmed" if eligible_for_projection else "provisional")
        )
        target = _TARGET_BY_KIND[proposal.kind] if status == "confirmed" else "none"
        by_id[rule_id] = CanonicalRuleV1(
            rule_id=rule_id,
            kind=proposal.kind,
            content=content,
            applicability=applicability,
            validation=validation,
            supporting_train_uids=support,
            counterexample_train_uids=counterexamples,
            support_count=len(support),
            contradiction_count=len(counterexamples),
            first_seen_batch=(
                reflection.batch_index if prior_rule is None else prior_rule.first_seen_batch
            ),
            last_validated_batch=reflection.batch_index,
            confidence=proposal.confidence,
            status=status,
            target_projection=target,
            retirement_reason=proposal.retirement_reason,
        )

    if len(by_id) > 64:
        raise TemperatureEvidenceError("EVIDENCE_RULE_LIMIT_EXCEEDED")
    semantics: set[tuple[str, str, str]] = set()
    for rule in by_id.values():
        semantic = (rule.kind, rule.content.casefold(), rule.applicability.casefold())
        if semantic in semantics:
            raise TemperatureEvidenceError("EVIDENCE_DUPLICATE_RULE_SEMANTICS")
        semantics.add(semantic)
    result = RuleEvidenceIndexV1(
        batch_index=reflection.batch_index,
        rules=tuple(sorted(by_id.values(), key=lambda rule: rule.rule_id)),
    )
    return result


def _new_rule_id(proposal: ReflectionProposalV1) -> str:
    identity = {
        "kind": proposal.kind,
        "content": proposal.content.casefold(),
        "applicability": proposal.applicability.casefold(),
    }
    return "rule-" + sha256_bytes(canonical_json_bytes(identity))[:24]


def evidence_uid_closure(index: RuleEvidenceIndexV1) -> frozenset[str]:
    return frozenset(
        uid
        for rule in index.rules
        for uid in (*rule.supporting_train_uids, *rule.counterexample_train_uids)
    )


def require_no_forbidden_keys(value: object, *, forbidden: Iterable[str]) -> None:
    """Defensive recursive key gate for public evidence projections."""

    blocked = {item.casefold() for item in forbidden}
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in blocked:
                raise TemperatureEvidenceError("PUBLIC_EVIDENCE_PRIVATE_FIELD")
            require_no_forbidden_keys(child, forbidden=blocked)
    elif isinstance(value, (list, tuple)):
        for child in value:
            require_no_forbidden_keys(child, forbidden=blocked)


__all__ = [
    "EVIDENCE_SCHEMA",
    "REFLECTION_SCHEMA",
    "BatchReflectionV1",
    "CanonicalRuleV1",
    "ReflectionProposalV1",
    "RuleEvidenceIndexV1",
    "TemperatureEvidenceError",
    "apply_batch_reflection",
    "evidence_uid_closure",
    "parse_batch_reflection",
    "require_no_forbidden_keys",
]
