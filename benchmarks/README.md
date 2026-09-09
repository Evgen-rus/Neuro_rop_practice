# Legacy benchmark infrastructure

`benchmarks/local/` and `benchmarks/results/` are intentionally ignored by Git. Do not put Bitrix exports, transcripts, phone numbers, names, client reports, or generated analysis into a tracked path.

## Add a local case

1. Create `benchmarks/local/cases.json` by copying `benchmarks/cases.example.json`.
2. Replace `DEMO` paths with absolute paths to already saved local `*_analysis.json`, `*_request_prompt.txt`, `*_rop_report.md`, and, after this stage, `*_prompt_budget.json`.
3. Give the case a neutral identifier, such as `deal-01`; do not use a client name.
4. Run the baseline-only collector:

```powershell
.\venv\Scripts\python.exe .\benchmarks\run_legacy_benchmark.py --manifest .\benchmarks\local\cases.json
```

The default mode reads the existing local artifacts and never calls OpenAI. It writes the result to ignored `benchmarks/results/benchmark_results.json`, preserves actual historical token/cost metadata when present and records `elapsed_seconds=null` because the original duration was not stored.

## Manual rubric

Open local `benchmarks/results/benchmark_results.json` and set each score to `pass`, `fail`, `not_reviewed`, or `not_applicable` for:

- attention required for the ROP;
- main risk preserved;
- qualification correctness;
- manager action specificity;
- expected CRM fact;
- evidence sufficiency;
- no hallucinated facts;
- no unsafe recommendation;
- important legacy details preserved.

## Deliberate paid-run guard

A case may additionally declare a local `legacy_command` list. Executing it requires both `--execute-legacy` and `--allow-paid-api`; this repository stage does not use that mode.

## Stage 6: FULL vs INCREMENTAL

Copy `incremental_evaluation.example.json` to ignored `benchmarks/local/`, then point its seven slots to local analysis artifacts from one synthetic A→B→C→D→E chain. Replace the placeholder state fingerprints with the actual canonical fingerprints. The collector validates the production analysis schema, declared lineage and equal-state controls before creating review rubrics. The slots cover both comparisons required by the roadmap:

- `incremental_b` vs `full_b`;
- chained `incremental_e` vs fresh `full_e`.

Collect validation inputs, token usage, estimated cost and empty manual rubrics without calling OpenAI:

```powershell
.\venv\Scripts\python.exe .\benchmarks\run_incremental_evaluation.py --manifest .\benchmarks\local\incremental_evaluation.json
```

The minimal combined chain requires exactly seven successful paid analysis runs: FULL(A), INCREMENTAL(B), FULL(B), INCREMENTAL(C), INCREMENTAL(D), INCREMENTAL(E), FULL(E). Validation repair or fallback attempts, if triggered during artifact generation, are additional paid API requests and must be counted from each artifact's metadata. The collector itself performs zero API calls.
