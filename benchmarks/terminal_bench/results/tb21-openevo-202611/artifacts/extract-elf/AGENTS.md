# Task-local operating instructions

Implement `extract.js` so the requested Node command emits one JSON object mapping decimal memory addresses to integer values. Every emitted value must match the reference, and the output must cover at least 75% of reference entries. Prioritize establishing the reference’s memory interpretation before expanding extraction coverage.

## Evidence and outcome

The previous attempt passed `test_outputs.py::test_extract_js_exists` and failed `test_outputs.py::test_output_matches_reference`. Grading reported correctness as incorrect with reward `0.0`. No detailed mismatch trace was supplied, so the specific cause remains unknown.

Local validation successfully checked syntax, parsed the JSON, and compared thousands of emitted words with a kernel-loaded process. That comparison established consistency with the chosen memory model; it did not establish agreement with the reference. The reported coverage used mapped pages as its denominator, which did not prove the required reference coverage.

## Establish the extraction contract

- Resolve address base, value width, signedness, byte interpretation, address stride, included regions, and relocation state separately.
- Treat the example output as a formatting example unless evidence establishes additional semantics.
- Distinguish file offsets, ELF virtual addresses, rebased addresses, and actual process addresses.
- ELF byte order governs structural parsing. Verify separately how the reference converts extracted bytes into integers.
- Distinguish raw file content, a static loader image, memory immediately after execution begins, and memory after dynamic linking or program initialization.
- Record assumptions explicitly. An unanswered clarification does not confirm them.

The inspected input was a little-endian, 64-bit x86 PIE executable with multiple load segments, writable data, BSS, and dynamic relocations. These observations require deliberate mapping decisions; they do not identify the reference loader.

## Inspect and implement

Use available local evidence first. `readelf`, `objdump`, Python, and Node worked in the observed environment. `file`, `xxd`, and several Python binary-analysis packages were unavailable. Check availability and use working alternatives promptly.

Inspect ELF headers, load segments, relevant sections, and relocations. Public loader source can explain a candidate model, but its behavior is not evidence that the reference uses that loader.

Build address translation from parsed metadata. Validate header sizes, offsets, segment lengths, arithmetic, and buffer bounds. Preserve integer precision when handling wide ELF fields and decimal address keys.

Before including page prefixes, alignment gaps, bytes beyond declared segment contents, or page tails, establish that those regions belong to the reference’s address domain. Keep file-backed bytes and zero-filled memory distinct.

Handle relocation uncertainty at the affected byte ranges. The previous implementation discarded all declared writable segment contents; that broad omission was not justified by measured reference coverage. Narrow exclusions require evidence too, including exclusion of any output word that overlaps uncertain bytes.

Keep stdout exclusively for JSON and diagnostics on stderr. Accept the input path from the command line and preserve the supplied binary.

## Validate against the right target

Run the exact requested command and verify valid JSON, decimal address keys, integer values, and successful exit status.

Use independent comparisons that challenge the extraction model:

- Check representative nonzero words in headers, code, constants, and data.
- Check segment boundaries, BSS transitions, mapping overlaps, and relocation destinations.
- Include values that distinguish signed from unsigned interpretation and alternative byte orders.
- If inspecting process memory, document when the process stopped. The previous `ptrace` check stopped at execution startup and did not establish post-linker contents.

Use available task tests when accessible. Measure value agreement and coverage against the reference address set whenever available. Counts of mapped words or percentages of mapped pages are supplementary diagnostics.

## Completion criteria

Completion requires a functioning `extract.js`, clean JSON output, matching values for every emitted address, and at least 75% reference coverage.

A syntax pass, file-existence pass, or process-memory comparison alone does not demonstrate completion. If reference validation remains unavailable, state exactly what was verified and which contract assumptions remain unresolved. Treat a reference-match failure as unresolved work without inventing its cause.
