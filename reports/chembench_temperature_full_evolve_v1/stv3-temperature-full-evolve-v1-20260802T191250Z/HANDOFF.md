# Temperature Full-Evolve v1 Handoff

- Status: `COMPLETE`
- Train/Test: 100/100; Train batches: 4
- Split SHA-256: `779e1b60098a958519bfe61296b5cb6c28afa2a2db300238876dcb693ccb1100`
- Source commit: `b625a8ceabc512636a474502d84a93ece1f50929`
- Model/reasoning: `gpt-5.5` / `medium`
- Managed Codex SHA-256: `a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902`
- Final text memory: `0cd3998a0f773977b05803b4c482ad70009c3a7027593cb7501e0ba06af23a57`
  (3455 bytes)
- Final skill bundle: `128d771e8399a12bf6c967da8006d252ebb6262ef00de21d15550ca903e62f50`
  (784 bytes)
- Final agent system: `ebf98e210c2c025b9c1f9ebb30b54057823e02ddbfdaab00538c5d5c6bc2a903`
  (492 bytes)
- Evolved Test: 83/100
- Baseline Test: 93/100
- Evolved - Baseline: -10.00 pp
- Paired flips baseline-only/evolved-only:
  11/1
- McNemar exact p: 0.00634766
- Calls Candidate/Reflector/Core: 400/
  4/12
- Experimental model logical calls / managed runtime session attempts:
  404/
  404
- Credential-readiness passed for accepted calls:
  404; canary provider attempts:
  `NOT_MEASURED` (lower bound
  404)
- Failures/retries/rejected/recoveries: 0/
  0/0/0
- Controller run: `stv3-temperature-full-evolve-v1-20260802T191250Z`
- Evolved/baseline runs: `stv3-temperature-full-evolve-v1-20260802T191250Z-evolved-test` /
  `stv3-temperature-full-evolve-v1-20260802T191250Z-baseline-test`

The package is aggregate-only. Verify every file except `SHA256SUMS.txt` against
`SHA256SUMS.txt` before copying or publishing it.
