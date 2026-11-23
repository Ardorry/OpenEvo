# Canonical task replacement

Seven solver attempts ran against the canonical `sanitize-git-repo` task ref. All seven passed the original verifier. The admission order comes from the timestamps of the original lock files. [Attempt IDs, task refs, hashes, and grading receipts](attempts.json)

| Canonical attempt | Reported batch | Grading source |
| --- | --- | --- |
| 1 | Outside the five-round cohort | Recorded-command replay, then original verifier |
| 2 | Outside the five-round cohort | Recorded-command replay, then original verifier |
| 3 | A | Recorded-command replay, then original verifier |
| 4 | B | Recorded-command replay, then original verifier |
| 5 | C | Original completed native verifier |
| 6 | D | Preserved answer snapshot, then original verifier |
| 7 | E | Preserved answer snapshot, then original verifier |

Attempts 3–7 replace the historical `sanitize-git-repo` record in the corresponding original round. The other 440 trial outcomes and usage records are preserved. The grading recovery used the original tests and no additional model calls. The [corrected trial tables](../corrected-five/) identify both the original trial and the grading source for each row.
