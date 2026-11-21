Operate only on the MNIST inference CLI task in `/app`. Deliver `cli_tool`, `weights.json`, and `prediction.txt`. The required invocation is `./cli_tool weights.json image.png`; successful stdout and `prediction.txt` must each contain exactly one ASCII digit from `0` through `9`.

1. **Inspect the supplied assets before implementing.** Read `model.py`, inspect the checkpoint’s tensor names, shapes, dtype, storage layout, and byte order, and inspect the PNG’s dimensions and mode. Preserve the original model and image. Use the supplied JSON parser and PNG decoder where suitable.

2. **Establish the build environment early.** The observed environment initially lacked PyTorch, Pillow, GCC, and G++. Check availability explicitly and keep dependency setup bounded. An HTTP package-index request stalled; an HTTPS probe succeeded and allowed installation to proceed. Diagnose transport failures with short timeouts before retrying, and avoid leaving duplicate package-manager processes running. Do not make inference depend on installation, network access, or compilation.

3. **Export the actual checkpoint weights faithfully.** Preserve tensor names, dimensions, ordering, offsets, strides, and float values. If PyTorch is unavailable, inspect the checkpoint archive and use a narrowly restricted reader that rejects unsupported serialized classes and storage types. Do not use unrestricted pickle loading. Validate the exported JSON against checkpoint metadata independently of the CLI.

4. **Resolve preprocessing from evidence.** The model definition describes the network but does not establish image normalization. Seek supplied training or inference configuration before selecting scaling, normalization, grayscale conversion, flattening order, or alpha handling. In the observed work, two preprocessing choices produced the same sample prediction; that agreement did not establish which transform was intended. Record unresolved assumptions and avoid treating conventional MNIST preprocessing as proven.

5. **Implement the complete inference contract.** Match the supplied layer operations, matrix orientation, biases, activations, and argmax behavior. Load weights and images from the command-line arguments. Validate tensor dimensions, numeric values, JSON parsing, and PNG decoding before inference. On success, emit only the digit. On invalid input, return nonzero, keep stdout empty, and send diagnostics to stderr.

6. **Verify model fidelity separately from implementation consistency.** Prefer a reference that loads the original checkpoint and follows independently established preprocessing. Compare intermediate activations or logits with appropriate numeric tolerances, as well as the final digit. A reference that consumes the same exported JSON and repeats the implementation’s preprocessing can detect arithmetic differences while sharing export or preprocessing mistakes.

7. **Keep validation bounded and relevant.** Give every inference subprocess an explicit timeout and inspect return code, stdout, and stderr separately. Exercise the supplied image, representative PNG encodings, and meaningful input variations. Temporary modified weights can demonstrate that the CLI loads the requested file and can select every output class; preserve the delivered weights. Check malformed weights, missing files, corrupt images, and incorrect arguments without expanding beyond the supported contract.

8. **Finalize artifacts only after a successful run.** Build a real executable for the target environment and verify its binary format and executable permission. Static linking was locally demonstrated, but the requirement is a runnable binary with available dependencies. Generate `prediction.txt` from captured successful CLI output after validating it; avoid truncating an existing valid prediction through a failed shell redirection. Recheck the exact required invocation against the final files.

Treat the recorded verification evidence precisely:

- `test_outputs.py::test_weights_file_exists`, `test_cli_tool_exists`, `test_prediction_file_exists`, `test_prediction_file_content`, and `test_cli_tool_executable` passed.
- `test_outputs.py::test_cli_tool_output` was reported failed.
- The grading result recorded failure, reward `0.0`, and `VerifierTimeoutError`. This does not establish an incorrect predicted digit or identify the timeout’s cause.
- Local checks reported 26 reference comparisons, selection of all ten output digits, and six error cases passing. Those results support local behavior only; they do not override the grading outcome.

Completion requires all three artifacts in `/app`, valid exported weights, a promptly terminating executable, exact digit-only output, and a prediction file matching a successful final invocation. Report local checks separately from verifier results. If verification times out, retain correctness as unconfirmed and investigate termination and execution behavior without inventing an expected digit or failure explanation.
