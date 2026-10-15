# ChemCrowRun candidate publication plan

Status: **PREPARED; NOT AUTHORIZED OR EXECUTED**. The user's instruction requires final approval for push and formal PR creation. This file is a local review artifact.

## Proposed source publication

- Repository: `https://github.com/Ardorry/OpenEvo`
- New remote head: `codex/chemcrowrun-v1`
- Base for the formal PR: `chemcrow` (observed `4bfe8f797a2026ebbcd95fb64b6ea32646c827dd` on 2026-10-15).
- Push only the reviewed local candidate head. No force push, original branch overwrite, merge or default-branch change.
- Proposed PR title: **ChemCrowRun v1.0: isolated reproduction workflow and verified 14-task results**.
- Source, deployment, configuration/schema, historical mapping and sanitized evidence only. No raw model answers, transcripts, databases, credentials or large external assets.

After approval, recheck remote head and candidate diff, then use:

```bash
git push origin HEAD:refs/heads/codex/chemcrowrun-v1
gh pr create --repo Ardorry/OpenEvo --base chemcrow --head codex/chemcrowrun-v1 \
  --title 'ChemCrowRun v1.0: isolated reproduction workflow and verified 14-task results' \
  --body-file benchmarks/chemcrow/reports/chemcrowrun_acceptance_20261015/PR_BODY.md
```

No PR is created by preparing this plan. The proposed PR remains unmerged pending review.

## Assets withheld from this approval scope

The original runtime and 24 companion assets are staged and checksummed locally. Native dependency attribution remains `PENDING_LICENSE_OBLIGATION_REVIEW`; they are **not proposed for upload now**. No GHCR/Docker Hub image, GitHub Release Asset or leaderboard submission is part of this approval request.

After distribution review and a later explicit asset approval, the manifest must contain the real public HTTPS asset URL. Anonymous download/hash/import/start and a real remote fresh clone must then be executed and recorded. Those checks are pending, not implied by the passing local experiment.

## Review evidence

See [A–H acceptance](ACCEPTANCE.md), [final report](chemcrowrun-report.json), [submission](chemcrowrun-submission.json), [analysis provenance](export-provenance.json), [Git review](publication-git-state.json), [commits](publication-commits.txt) and [diff](publication-diff-stat.txt). The dated review state is explicitly a pre-evidence-commit snapshot; the final candidate head is verified again before any push.
