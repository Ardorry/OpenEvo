Fit the graphene spectrum in `/app/graphene.dat` and write `/app/results.json` with top-level keys `G` and `2D`, each containing numeric `x0`, `gamma`, `amplitude`, and `offset`. Preserve the source data.

Prioritize coordinate interpretation and parameter definitions before optimizing the fit. The previous attempt produced valid JSON and passed `test_outputs.py::test_result_file_exists`, but failed both `test_outputs.py::test_G_Peak` and `test_outputs.py::test_2D_Peak`. Its local fit checks passed. The available feedback does not identify which parameters caused either failure.

1. **Inspect and parse the instrument output.**
   - Handle decimal commas, tab separators, and line endings explicitly.
   - Check column count, finite values, axis direction, spacing, and intensity range.
   - The observed first column decreased and was strongly nonlinear; its reciprocal revealed recognizable G and 2D features. Investigate the coordinate meaning before selecting fitting windows.
   - Locate peaks from the spectrum. Do not identify the graphene bands by the largest intensity anywhere in the file.

2. **Establish the fitting coordinate and parameter convention.**
   - Inspect available task documentation, metadata, and local fitting code for units and model definitions.
   - Treat the previous decision to retain the original numerical axis as an unverified assumption.
   - If a coordinate transformation is justified, transform the sample coordinates and refit. A nonlinear transformation does not preserve a Lorentzian shape or permit a simple conversion of its width.
   - Define whether `gamma` denotes half-width or full width, and whether `amplitude` denotes peak height or integrated area. These conventions change the reported values even when curves look identical.
   - Keep the model equation and parameter definitions together in the fitting script. If local evidence leaves the convention ambiguous, record that uncertainty rather than treating a chosen convention as verified.

3. **Fit the peaks with a defensible background.**
   - Use physically sensible initial guesses and bounds, including positive width and peak strength.
   - Inspect both sides of each peak to estimate its background and choose a window containing sufficient tails without unrelated features.
   - Compare nearby windows and constant versus sloping backgrounds. The previous exploration showed substantial changes in width, amplitude, and offset across windows.
   - Explain the final background choice. The previous attempt observed sloping backgrounds and lower residual errors with slope terms, then submitted constant-background fits without resolving that discrepancy.
   - If using a slope internally, define the reference coordinate for the reported offset. Ensure the four output fields retain a coherent meaning.

4. **Validate parameter recovery separately from curve appearance.**
   - Check convergence, covariance or uncertainty, bound contacts, and residual structure.
   - Examine window sensitivity, particularly correlations among width, amplitude, and offset.
   - Do not accept a fit solely because it exceeds an arbitrary R² threshold. Both previously submitted fits exceeded their local threshold while both peak tests failed.
   - Reconstruct each fitted curve using the selected equation and parameter convention, and check its center, width, peak strength, and background against the data.

5. **Keep execution reproducible.**
   - Check imports before fitting. NumPy and SciPy were initially unavailable; their later installation succeeded.
   - Install only needed dependencies, use bounded installation attempts, wait for completion, and verify imports.
   - Avoid overlapping installations or repeatedly running code against an unfinished dependency setup.

Completion requires a readable `/app/results.json` with exactly the requested keys and finite JSON numbers, independently checked coordinate and model conventions, and justified fits for both peaks. Reload the saved file to verify serialization. Run available authorized tests and report their actual statuses; file existence, optimizer convergence, and local fit quality do not establish that the peak parameters are correct.
