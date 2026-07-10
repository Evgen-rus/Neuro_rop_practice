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

## Compact attention-delta shadow

The isolated shadow runner reuses the input-file paths recorded inside an existing local legacy `*_analysis.json`. It never replaces that analysis, its prompt, its report, SQLite state, or UI data.

```powershell
# Verifies one local case and writes only ignored shadow prompt telemetry.
.\venv\Scripts\python.exe .\benchmarks\run_attention_delta_shadow.py --manifest .\benchmarks\local\cases.json --case-id deal-01

# Calls OpenAI only after an explicit acknowledgement; do not run without approval.
.\venv\Scripts\python.exe .\benchmarks\run_attention_delta_shadow.py --manifest .\benchmarks\local\cases.json --case-id deal-01 --allow-api
```

After an API run, generate a comparison sheet for manual review:

```powershell
.\venv\Scripts\python.exe .\benchmarks\compare_attention_delta.py --manifest .\benchmarks\local\cases.json
```

`ATTENTION_DELTA_MAX_OUTPUT_TOKENS` is an isolated compact-output cap; it includes reasoning and visible JSON. Its final value will be chosen only after benchmarking, targeting p95 actual usage plus a 25–30% safety margin. The runner's preflight price is deliberately a no-cache worst-case estimate using this cap, not a prediction of actual spend.
