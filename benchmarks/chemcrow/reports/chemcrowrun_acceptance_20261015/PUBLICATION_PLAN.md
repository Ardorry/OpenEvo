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

## Proposed runtime release — included in the final approval request

The corrected actual-link review closes `V8-LGPL-RELINK`. Original runtime redistribution is now
supported by evidence **with all required source/notice companions**. The original 53 MB supplement
is unchanged; `runtime-native-link-materials.tar.gz` is additive. The authoritative upload list is
`release-assets.json`, plus `release-assets.json` itself and `SHA256SUMS`. Do not upload only the OCI.

Proposed destination: a GitHub Release in `Ardorry/OpenEvo`, tag **`chemcrowrun-v1.0`**, at the approved
candidate commit. Title: **ChemCrowRun v1.0 reproduction runtime and source companions**. This tag,
release and its asset URLs are proposals, not existing public artifacts. The release description
must link the distribution review and required source/notice companions and disclose the zsh
GLIBC limitation. The runtime retains its original archive/index/config hashes.

No release or upload has been executed. The final approval request covers only the reviewed source
branch push, formal unmerged PR, and this exact Release asset set. It does not authorize merging,
GHCR/Docker Hub uploads, leaderboard submissions or publication of private scientific state.

After approval, recheck the remote branches/tag, publish the exact reviewed assets together, and
record the actual public HTTPS URL. In a new empty directory verify anonymous download/hash/import,
clone the actual remote commit, and run the independent paid reproduction with fresh state and
budget protection. Previously passed local experiments are retained and are not relabeled as a
remote fresh-clone run. Public download and remote experiment acceptance remain PENDING.

## Review evidence

See [A–H acceptance](ACCEPTANCE.md), [final report](chemcrowrun-report.json), [submission](chemcrowrun-submission.json), [analysis provenance](export-provenance.json), [Git review](publication-git-state.json), [commits](publication-commits.txt) and [diff](publication-diff-stat.txt). The dated review state is explicitly a pre-evidence-commit snapshot; the final candidate head is verified again before any push.
