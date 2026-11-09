# Repository sanitization instructions

Sanitize `dclm` by replacing confirmed API credentials with consistent placeholders. Preserve every uncontaminated file and every unrelated byte within contaminated files.

The recorded verification passed `test_removal_of_secret_information` and `test_correct_replacement_of_secret_information`, but failed `test_no_other_files_changed`. Overall correctness failed. Treat file preservation as an independent completion requirement; successful credential scans do not establish it.

## Priorities

1. Find all confirmed API credentials and their duplicate occurrences.
2. Replace only credential values with the required placeholders.
3. Keep existing placeholders.
4. Preserve unrelated content, files, formatting, permissions, and repository structure.
5. Verify removal, replacement correctness, and file preservation separately.

Use these exact placeholders:

| Credential type | Placeholder |
| --- | --- |
| AWS access key ID | `<your-aws-access-key-id>` |
| AWS secret access key | `<your-aws-secret-access-key>` |
| GitHub token | `<your-github-token>` |
| Hugging Face token | `<your-huggingface-token>` |

## Establish the baseline

Before editing, record Git status and a file inventory with content hashes. Distinguish preexisting user changes from your changes. Keep temporary scripts, reports, and backups outside the repository.

Build an evidence-based list of contaminated files before mutation. Record credential type, location, and occurrence count without printing sensitive values. Do not hardcode an expected number of files or credentials into validation.

## Discover and classify

Start with `rg` searches for provider token formats and credential assignments. Inspect source, configuration, documentation, and serialized content. Saved diffs inside JSON strings can contain real credentials even when the surrounding file is experiment metadata.

Treat search matches as candidates. This repository contains harmless credential terminology, empty assignments, environment-variable references, HTML `accesskey` attributes, example passwords, tokenizer data, hashes, and long URL components. Length or entropy alone does not justify editing them.

Use token boundaries and bounded patterns. A broad provider pattern previously matched ordinary URL text containing a token-like substring. Inspect redacted surrounding context before deciding to replace it.

After confirming a credential, search for that exact value throughout the working tree. Retain the value privately in memory for replacement and verification.

## Control archive scope

Inspect compressed content read-only when useful. Stream large gzip files with overlapping chunks. Handle encrypted ZIP entries individually so an unreadable entry does not terminate the remaining scan.

Do not automatically classify historical signed URLs in bundled benchmark data as API keys requiring replacement. Establish how a finding fits the requested sanitization before expanding the changed-file list.

The recorded attempt expanded from text substitutions into rewriting an encrypted dataset archive. Its archive integrity checks passed, but the final file-preservation test failed. The available feedback does not identify the exact offending change. Preserve this distinction: readable entries, retained encryption, and unchanged member payloads do not prove that changing the archive file was within scope.

## Make minimal edits

Replace confirmed literal values directly. Preserve quoting, whitespace, line endings, JSON escaping, and surrounding commands. Avoid reserializing whole documents.

Do not normalize harmless examples, fill empty credential fields, add prevention tooling, reformat files, or delete content. Do not rewrite Git history, commit, or push as an automatic extension of local sanitization.

## Completion checks

Complete all three checks against the original baseline:

- **Removal:** Confirm original credential values are absent from the intended working-tree scope and rerun independent provider/context searches. Classify remaining matches.
- **Replacement:** Confirm each removed occurrence has the correct retained placeholder. Parse changed Python and JSON without executing repository workloads.
- **Preservation:** Compare the complete file inventory and hashes. Every changed file must have independently confirmed contamination; every content difference must be an intended substitution. Check untracked files and file modes as well as `git diff --check`.

Run the supplied task tests when available. Custom assertions and clean diffs supplement those tests; they do not replace their verdicts. After any additional mutation, repeat preservation checks.

Report only completed checks. State archive access limitations or remaining historical copies accurately, and do not claim overall success while a required preservation check fails.
