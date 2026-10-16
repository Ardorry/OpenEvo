# Chemistry Task Memory

## Reliable Workflow

1. Select a close structural analogue using the smallest modification that removes the forbidden motif.
2. Obtain and report a canonical SMILES from software evidence.
3. Verify the exclusion with an explicit SMARTS/substructure search when possible.
4. Inspect the structure manually as a secondary check, distinguishing similar motifs such as:
   - acyl methyl groups,
   - N-methyl substituents,
   - generic amides.
5. Quantify similarity with a named fingerprint or metric; describe moderate scores accurately rather than overstating closeness.
6. Check identity, molecular weight, and relevant functional groups before planning synthesis.
7. Query purchasing data and report the exact outcome:
   - price found,
   - unavailable from searched sources,
   - or unknown because the lookup failed.
8. If synthesis is needed, provide a software-planned route and flag likely chemoselectivity or overreaction risks.

## Recurring Failure Modes

- A failed vendor or pricing query does not establish commercial unavailability. Never substitute “search failed” for “not purchasable.”
- Generic functional-group detection cannot prove absence of a specific forbidden substructure. Use dedicated substructure matching or clearly label manual inspection as the basis.
- Structural analogy and numerical similarity are different claims. A simple homologous change may be chemically intuitive while producing only moderate fingerprint similarity.
- Retrosynthetic and forward-prediction outputs are proposals, not experimental validation.
- Do not imply that procurement, synthesis, or laboratory testing was physically performed.

## Validation Checklist

- SMILES is tool-supported and structurally consistent.
- Forbidden motif definition is explicit and unambiguous.
- Substructure absence is directly tested where tooling permits.
- Similarity value, method, and interpretation agree.
- Purchasing status reflects evidence rather than inference.
- Proposed precursors reconstruct the target connectivity.
- Competing reactions and selectivity concerns are acknowledged.
- Every factual claim is separated from prediction or manual chemical reasoning.
