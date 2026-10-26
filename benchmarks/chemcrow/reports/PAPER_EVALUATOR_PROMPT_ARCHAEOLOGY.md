# ChemCrow EvaluatorGPT Prompt Archaeology

## Verdict

`EXACT_PROMPT_RECOVERED=false`

No author-controlled artifact inspected contains the implementation of `Evaluator`, its
complete prompt, or a byte-equivalent prompt template. The current calibration therefore
uses the compatible-prompt branch and does not claim an official or verbatim reproduction.

## Authoritative sources searched

- [official chemcrow-runs repository](https://github.com/ur-whitelab/chemcrow-runs), complete reachable Git
  history, `main` `500104ed9a5d479a8dc4128afc463625ade5a409`, tag `v1` `1e098b171ac34b9dcf95a836b506b6cd289ce113`. Searches
  covered `class Evaluator`, `def run`, `chemcrow.evaluation`, teacher calls, and every
  released evaluator-output heading. Notebooks contain imports, calls, and outputs, but not
  the implementation.
- [official chemcrow-public repository](https://github.com/ur-whitelab/chemcrow-public),
  complete reachable Git history, tags, and releases. No Evaluator implementation found.
- [official Zenodo release](https://doi.org/10.5281/zenodo.10884645); downloaded archive
  SHA256 `806dcb13314a43ee07a66c2751ab5fc04bc4df4b67a6588023e55d7539ed567b`. It reproduces the v1 run repository and
  does not add the implementation.
- [official PyPI project](https://pypi.org/project/chemcrow/): all 27 published source
  distributions from 0.1.0 through 0.3.24 were downloaded and searched. None contains the
  historical Evaluator implementation.
- [Nature Machine Intelligence article](https://www.nature.com/articles/s42256-024-00832-8),
  Supplementary Information SHA256 `ac14f6ee5d5c00736eeb8d4bf4e25bbe8311763cc7b5935d5132f36d07eec02b`, and Source Data SHA256
  `9e3383177c975662b7e76e54f490a811bda690cb2b869ca47ad24439f5367b8d`. They specify evaluation semantics and released scores, not
  the exact prompt.
- [official arXiv source](https://arxiv.org/abs/2304.05376), archive SHA256
  `21c607b0c71631e362d318e2424cac73dacb38fd528e463f02a9f030bef5ed23`. It repeats the published semantics and contains no prompt.

## Recovered authoritative semantics

The paper describes a teacher comparing two student answers to the same chemistry task,
grading whether the task was addressed and whether the overall thought process was correct,
then reporting grades, strengths, weaknesses, and improvement feedback. The experiment used
GPT-4 at temperature 0.1. These statements constrain the candidates but do not determine a
unique verbatim prompt.

## Environment evidence and limits

The notebooks import `Evaluator` from `chemcrow.agents`, but notebook metadata does not pin a
package version or source revision. Most kernels report Python 3.8.16; tasks 08 and 15 report
3.11.3. No lockfile or wheel hash binds the missing implementation. Inferring prompt wording
from released prose or output shape would therefore not be exact recovery.

## Non-authoritative clues

Public code search was used only to locate potential sources. No third-party prompt was
accepted as authority and no non-authoritative wording was copied into the frozen candidates.

Dataset binding: `148ddc2bd38dc3c49a01d01cd9cc99035cb0d70c085c7c2f3e738b378c813de0`.
