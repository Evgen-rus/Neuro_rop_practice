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
