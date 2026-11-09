# AGENTS.md

Implement `find_dominant_eigenvalue_and_eigenvector` in `/app/eigen.py` for real `np.float64` square matrices up to 10×10. Return the eigenvalue with largest **magnitude** and a corresponding nonzero eigenvector. Support nonsymmetric matrices and complex results.

The recorded verifier outcome was failure: `test_eigen_pair` and `test_dominance_eigenvalue` passed, while `test_speedup` failed. Preserve numerical correctness and prioritize reliable performance. The available feedback does not identify the failing size, timing threshold, or cause.

**Inspect before changing code**

- Read `/app/eigen.py`, `/app/eval.py`, and applicable repository instructions.
- Establish baseline timings before selecting an optimization.
- Check installed packages, compiler availability, NumPy configuration, and native-library interfaces directly. Build configuration metadata does not establish that a compiler exists.
- Keep the reference implementation and evaluation behavior unchanged.

**Treat performance as a separate acceptance requirement**

- `/app/eval.py` prints candidate and reference medians but does not assert a speedup. Successful execution alone does not demonstrate completion.
- Its public correctness check verifies the eigenpair equation but does not establish dominance. Check dominance independently.
- Measure the complete Python entrypoint, including copying, allocations, eigenvalue selection, and complex-vector construction.
- Benchmark every supported size, including odd sizes and the largest matrices. Strong improvements on small matrices cannot establish consistent performance across the range.
- Repeat measurements in fresh processes with independent inputs. Match the public evaluator’s separate candidate and reference timing loops; use interleaved measurements as an additional diagnostic.
- Record medians and ratios per size. Investigate near-parity results and changes caused by measurement order. Earlier local measurements varied substantially, and one largest-size measurement was nearly tied despite stronger gains in other runs.
- Require a repeatable margin over the reference. Local timing gains and thousands of numerical checks did not prevent the recorded speed-test failure.
- Recheck timings after changes to solver workspace, allocation strategy, wrappers, or compilation settings. A faster prototype does not establish the speed of its replacement.

**Preserve numerical and memory safety**

- Check both `np.allclose(A @ eigenvec, eigenval * eigenvec)` and the magnitude of the returned eigenvalue against an independent reference.
- Require a finite eigenvalue and a finite, nonzero vector. Do not make the residual appear acceptable by returning a negligible vector.
- Exercise random nonsymmetric matrices, dominant complex pairs, negative dominant eigenvalues, tied magnitudes, repeated eigenvalues, zero matrices, triangular matrices, and varied scales.
- Preserve the input. Check contiguous, transposed, Fortran-order, sliced, and negative-stride arrays if using direct memory access.
- Verify returned vectors remain valid after subsequent calls and do not alias overwritten scratch storage.
- If using LAPACK directly, verify integer width, calling convention, storage order, workspace requirements, convergence status, and reconstruction of complex vectors from conjugate-pair columns. Do not infer ABI compatibility from a library filename.
- Retain loaded libraries for as long as native function pointers remain callable.

**Keep implementation and setup reproducible**

- Prefer the simplest measured approach that satisfies correctness and speed requirements. Native code is optional.
- Bound package installation and compiler discovery efforts. A stalled system-package operation previously consumed several minutes; switch to an available route when setup stops progressing.
- Verify imports and any native build from a clean cache. Keep setup outside repeated calls and ensure required dependencies are available.
- Surface compilation and solver failures clearly.
- Use a supported patch operation when replacing a file, inspect the result, and confirm dependent checks actually executed after any tool failure.

**Completion criteria**

Finish only after the final implementation passes eigenpair and dominance checks and repeatedly beats the unchanged reference across tested sizes. Run the available verifier when accessible. Report its result separately from local measurements; if verification is unavailable, state that limitation and do not claim a speed-test pass.
