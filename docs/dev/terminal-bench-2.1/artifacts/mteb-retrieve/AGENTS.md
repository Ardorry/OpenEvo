# Task-local operating instructions

## Objective and priorities

Retrieve the fifth-highest cosine-similarity document for `terminal-bench` from `/app/data.txt` and write that original line to `/app/result.txt`.

- Use installed `mteb` version `1.36.8` to load and encode `BAAI/bge-small-zh-v1.5`.
- Pin revision `7999e1d3359715c523056ef9478215996d62a620`.
- Preserve each input line as a separate document, including duplicates and meaningful whitespace.
- Resolve encoding and prompt behavior before accepting a ranking.

## Evidence that must guide the work

The previous run loaded the pinned model, computed normalized similarities, wrote a result, and exited successfully. Grading nevertheless reported `test_outputs.py::test_result_exists` passed and `test_outputs.py::test_data_matches` failed. File existence, successful execution, and reading the written line back therefore do not establish correctness.

That run encoded the query and documents together with no explicit `prompt_type`. Model metadata exposed a query instruction, while the wrapper selected prompts using both `task_name` and `prompt_type`. Treat the omitted query role as a concrete issue to investigate; the available grading evidence does not prove the precise cause of the mismatch or reveal the correct document.

## Loading and encoding checks

1. Confirm the installed package version and inspect its local model metadata and wrapper API.
2. Load through `mteb.get_model` with the exact model identifier and revision. Keep encoding through the returned MTEB wrapper.
3. Inspect the wrapper’s prompt-selection logic, including `get_prompt_name`, and confirm the effective prompt for each encoding call.
4. Encode the query and passages separately so their roles can be supplied explicitly. For retrieval encoding, verify the registered query instruction is selected for the query and confirm passage behavior from the installed implementation.
5. Use a valid registered task name where required. An empty task name raised `KeyError` in the previous run. Replacing it with a valid name only resolves task lookup; it does not validate prompt semantics.
6. Avoid adding an instruction manually when the wrapper already applies it. Do not bypass MTEB by encoding directly through its underlying model.

When inspecting a loader stored as `functools.partial`, inspect its `.func` and bound arguments; passing the partial directly to `inspect.getsource` failed.

## Ranking and output checks

- Remove only line terminators when reading documents; do not strip, filter, reorder, or deduplicate their contents.
- Check that encoding returns one vector per document and one query vector, with matching dimensions, finite values, and nonzero norms.
- Compute cosine similarity explicitly or normalize both query and document vectors before taking dot products.
- Sort scores in descending order and select zero-based position `4`. Preserve the connection between scores and original line indices.
- Inspect nearby ranks and any ties. Keep a deterministic tie policy and document unresolved ambiguity.
- Write only the selected original document line, with an optional terminating newline. Read the file back and compare it with the selected source line.

## Download recovery

The previous download was slow but completed. Check cache-file growth and process output before restarting a loader. Enable offline loading only after the required snapshot files are complete. `curl` and `ps` were unavailable; Python requests and filesystem inspection provided useful diagnostics.

## Completion criteria

Require a successful encoding run, verified model revision and prompt behavior, checked fifth-place selection, and exact output-file contents. Run available authorized output tests. If content validation fails, investigate encoding semantics before rewriting the result. Report validation limits accurately; a successful script or existence check alone is insufficient.
