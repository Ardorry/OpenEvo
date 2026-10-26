"""Offline accounting of recorded Codex turn counters, without inferred billing."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def summarize_codex_usage(root: Path) -> dict:
    totals: Counter = Counter()
    field_turns: Counter = Counter()
    counts: Counter = Counter()
    inventory = []
    records = []
    identities = {}
    for path in sorted(root.glob("task_*/ses_*.json")):
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        payload = json.loads(raw)
        identity = (payload.get("task_id"), payload.get("session_id"))
        if not all(isinstance(value, str) and value for value in identity):
            raise ValueError("completion identity is missing")
        inventory.append({"path": path.relative_to(root).as_posix(), "sha256": digest})
        if identity in identities:
            if identities[identity] != digest:
                raise ValueError("conflicting completion identity")
            counts["duplicate_completion_files_skipped"] += 1
            continue
        identities[identity] = digest
        turns = 0
        seen = set()
        trajectory = payload.get("trajectory") or {}
        for trace in trajectory.get("traces", []):
            transcript = (trace.get("metadata") or {}).get("transcript")
            if not isinstance(transcript, str) or not transcript:
                continue
            if transcript in seen:
                counts["duplicate_transcript_attachments_skipped"] += 1
                continue
            seen.add(transcript)
            for line in transcript.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    counts["non_json_transcript_lines"] += 1
                    continue
                if not isinstance(event, dict) or event.get("type") != "turn.completed":
                    continue
                usage = event.get("usage")
                if not isinstance(usage, dict) or not any(key in usage for key in FIELDS):
                    counts["turns_without_usage"] += 1
                    continue
                observed = {key: usage[key] for key in FIELDS if key in usage}
                if any(type(value) is not int or value < 0 for value in observed.values()):
                    counts["invalid_usage_events"] += 1
                    continue
                turns += 1
                totals.update(observed)
                field_turns.update(observed.keys())
        counts["reported_turns"] += turns
        if payload.get("status") == "COMPLETED" and not turns:
            counts["completed_without_reported_usage"] += 1
        records.append(
            {
                "task_id": identity[0],
                "session_id": identity[1],
                "status": payload.get("status"),
                "sha256": digest,
                "reported_turns": turns,
            }
        )
    count_names = (
        "reported_turns",
        "completed_without_reported_usage",
        "turns_without_usage",
        "invalid_usage_events",
        "duplicate_transcript_attachments_skipped",
        "duplicate_completion_files_skipped",
        "non_json_transcript_lines",
    )
    partial = any(
        counts[key]
        for key in (
            "completed_without_reported_usage",
            "turns_without_usage",
            "invalid_usage_events",
        )
    )
    return {
        "schema_version": "chemcrowrun_recorded_codex_usage_v1",
        "status": ("PARTIAL" if partial else "RECORDED_TURNS_ONLY")
        if counts["reported_turns"]
        else "NO_USAGE_RECORDED",
        **{key: counts[key] for key in count_names},
        "observed_totals": dict(totals),
        "turns_reporting_field": dict(field_turns),
        "provider_account_token_total": None,
        "subscription_cost_usd": None,
        "infrastructure_canary_usage_included": False,
        "limitation": "Counters are summed independently as reported, including cached/reasoning fields; do not add overlapping fields. This is not complete provider billing or canary accounting.",
        "source_inventory_sha256": hashlib.sha256(
            json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "analysis_module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "completions": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-completions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-model-calls", action="store_true", required=True)
    args = parser.parse_args()
    if not args.core_completions.is_dir():
        parser.error("Core completion directory does not exist")
    result = summarize_codex_usage(args.core_completions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        handle.write(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
