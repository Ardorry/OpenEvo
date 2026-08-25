# ChemCrow paper evaluator v2 smoke audit

> Historical-scope correction, 2026-08-26: this file records the immutable V2 attempt only. Its
> exact HTTP 403 cause is **UNPROVEN**; the guardrail discussion below was a hypothesis, not proof.
> The separately proven legacy GPT-4 `response_format=json_object` HTTP 400 incompatibility does
> not explain V2's 403. Current v6-v8 plus minimal-direct evidence now proves a separate present-day
> pre-provider routing exclusion (`attempt=0`, all endpoints unselected). See
> `NEXT_STAGE_READINESS_AUDIT.md`; do not modify or retry the historical V2 claim/receipt.

Date: 2026-08-25 Asia/Shanghai

## Verdict

```text
FAIL_CLOSED_UPSTREAM_HTTP_403
HUMAN_OPENROUTER_GUARDRAIL_ACTION_REQUIRED
```

The authorized v2 smoke issued exactly one OpenRouter provider request. It traversed the required
`PaperEvaluatorHarness -> dedicated OpenEvo Rollout -> dedicated OpenEvo Gateway -> auth shim ->
OpenRouter` route. OpenRouter returned HTTP 403 before model, provider, schema, or usage metadata was
available. The request was not retried and is excluded from the formal 42-call ledger and benchmark
metrics.

## Pre-call gates

- `/key`: HTTP 200.
- `/credits`: HTTP 200.
- Frozen ceiling: funded.
- `openai/gpt-4`: visible to the configured inference key.
- Public OpenAI endpoint metadata: active and supports `temperature`, `max_tokens`, and
  `response_format`.
- Shim health: OpenAI-only, `data_collection=allow`, credential present but value absent.
- Dedicated Rollout/Gateway/shim: healthy on ports 8180/8110/8400 before submission.
- v2 claim and completion roots: empty before submission.

## Immutable attempt evidence

```text
call_id: paper-chemcrow-smoke-core-v2
provider request attempts: 1
requested model: openai/gpt-4
requested provider: openai only
temperature: 0.1
allow_fallbacks: false
require_parameters: true
data_collection: allow
upstream HTTP: 403
upstream error code: 403
upstream error category: other_upstream_error
successful model completions: 0
usage receipt: absent
billing: not proven
```

The shim stored only request/response hashes, HTTP status, error code/category, and error-message
hash. It did not store the credential, prompt, response body, or upstream error body. The terminal
claim and receipt remain at:

```text
/home/lhy-h/work/chemcrowrun/runs/paper-evaluator-smoke-v2/openrouter-receipts/
```

The Core callback itself ended in `ERROR` because the failed inference produced no
`step.00.stdout.log`; Gateway's subscription-transcript verifier therefore failed closed. The
harness originally raised before writing its sanitized report. This local bug was fixed by adding a
terminal-failure sealing path that consumes only existing claim/receipt/Core evidence and performs
zero upstream calls. The temporary Core completion was deleted after SHA256 sealing; it is not
recoverable from the workspace. The sanitized report is:

```text
/home/lhy-h/work/chemcrowrun/reports/OPENROUTER_GPT4_CORE_PAID_SMOKE_V2.json
```

## Remaining cause

Changing request-level `data_collection` from `deny` to `allow` did not clear the 403. The model is
visible, credits are sufficient, the selected endpoint supports every required parameter, and the
response contained no runtime content-filter routing metadata. OpenRouter documents that budget,
model/provider allowlist, and account/workspace ZDR restrictions can return 403 without the runtime
content-filter metadata.

The strongest remaining hypothesis is an account, workspace, member, or key guardrail—particularly
`enforce_zdr_openai=true`. Request-level `data_collection=allow` cannot override a stricter
account-wide ZDR rule. Since OpenAI direct is not ZDR and the protocol pins `provider.only=openai`,
that intersection has no eligible endpoint. Provider/model allowlists, ignored lists, and guardrail
budget are the other remaining checks.

The configured inference key cannot read management guardrails: zero-paid GET requests to
`/api/v1/guardrails` and `/api/v1/guardrails/assignments/keys` returned HTTP 401. No management key
should be pasted into chat or added to the benchmark environment.

Official references:

- <https://openrouter.ai/docs/guides/features/guardrails/overview>
- <https://openrouter.ai/docs/api/api-reference/guardrails/list-guardrails>
- <https://openrouter.ai/docs/guides/routing/provider-selection>

## Exact human action

In OpenRouter Dashboard, open `Settings -> Privacy -> Guardrails` and inspect the combined account,
workspace default, member, and API-key policy:

1. Set OpenAI-group ZDR enforcement to off: `enforce_zdr_openai=false`.
2. Ensure allowed providers is unrestricted or contains `openai`.
3. Ensure allowed models is unrestricted or contains `openai/gpt-4`.
4. Ensure ignored providers/models do not contain `openai` or `openai/gpt-4`.
5. Ensure every applicable guardrail budget limit has sufficient remaining capacity.
6. Check sensitive-information/custom filters, although the missing runtime metadata makes these
   less likely for the harmless water-formula smoke prompt.

Do not change the API key in chat and do not delete either v1 or v2 claim.

After this dashboard repair, a distinct v3 route is prepared but unauthorized:

```text
call_id: paper-chemcrow-smoke-core-v3
authorization: I_AUTHORIZE_ONE_CORE_PAPER_GPT4_SMOKE_V3_20260825
```

No v3 provider request has been made. Formal 42-call authorization remains unconsumed.
