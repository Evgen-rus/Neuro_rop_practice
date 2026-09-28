---
target: Расходы AI — мобильная версия
total_score: 12
max_score: 40
na_heuristics: 
p0_count: 2
p1_count: 2
p2_count: 2
target_identity: "file:D:\\My_dev_project\\Neuro_rop_practice\\frontend\\src\\AiSpend.tsx"
target_fingerprint: "sha256:bcd9047b85927a844d023ecdd4db776a99651f7a8393d0c0735fa9e44597a110"
target_path: "D:\\My_dev_project\\Neuro_rop_practice\\frontend\\src\\AiSpend.tsx"
timestamp: 2026-09-28T09-18-52Z
slug: frontend-src-aispend-tsx
---
## Design Health Score

| # | Heuristic | Score | Key issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 2 | Loading states exist, but nothing dims on refetch — a breakdown click looks like nothing happened |
| 2 | Match System / Real World | 1 | `Openai` is a "вид операции"; `Full deal analysis repair` and `run_id` are the operator's vocabulary |
| 3 | User Control and Freedom | 1 | `kindFilter` change silently killed an open modal; no back from the cross-filter jump |
| 4 | Consistency and Standards | 2 | The shell's own mobile nav is 44–48px; every AI Spend control was 24–36px |
| 5 | Error Prevention | 2 | Date `min`/`max` wired correctly; "Сбросить" cleared 4 filters with no undo; backwards custom dates read as "no data" |
| 6 | Recognition Rather Than Recall | 1 | Cost was the 5th of 6 cells; on mobile an unlabelled value in a 2×3 grid |
| 7 | Flexibility and Efficiency | 2 | The cross-filter skeleton is genuinely good — and gave no feedback when it fired |
| 8 | Aesthetic and Minimalist Design | 1 | ~95 interactive elements, ~5–6 phone-screens before the journal starts |
| 9 | Error Recovery | 2 | Honest notes for unknown-cost and skipped-lines; raw error string with no retry |
| 10 | Help and Documentation | 0 | The API ships `disclaimer` on 4 types; the UI rendered none of them |
| **Total** | | **12/40** | **Poor** |

Cognitive load: **7 of 8 failed** — critical band.

## Design Specificity Verdict

**Around 20% of this screen is НейроРОП.** The furniture is disciplined and the domain model is
authored (`kindGroupFromKind` knows `deal_manager_quick_help_`, entity rows link to Bitrix, days
are named in Moscow business time). But the whole `.ai-spend-*` block is written in hardcoded hex
while the shell runs on `--dc-*` tokens, so the screen can be re-skinned without anything upstream
noticing. It also never says what the money bought: no line reads "это стоило 450 ₽ и это сделка 42".

**Deterministic scan.** `detect --json frontend/src/AiSpend.tsx` → exit 0, `[]`. A directory scan of
`frontend/src` → 18 findings, rules `side-tab` and `layout-transition`, all in `index.css`, none
inside `.ai-spend-*`. False positive to name: `index.css:3949` is a Learning Shadow `.ls-flow` rule
sitting 4 lines above the first AI Spend selector. Scanning only the `.tsx` would have declared
this screen clean.

## Overall Impression

The honesty plumbing is excellent — `formatUsd` returns "оценка недоступна" rather than `0`,
`~0 ₽` is distinct from `0`, unknown-cost calls are explicitly excluded, `skipped_lines` is
surfaced. This is the one screen where *every* number is an estimate, and it shipped that caveat
nowhere.

## What's Working

- **The cross-filter skeleton** (`openJournal`) is the right IA: spend → cause → evidence,
  implemented four times consistently.
- **`EventDetails`** is textbook progressive disclosure — 15 technical fields behind one tap.
- **The honesty layer** is the strongest product-specific quality on the screen.

## Priority Issues

### [P0] The day-journal modal is a 6-value word cloud on a phone
`index.css:4401` (`.ai-spend-row { grid-template-columns: 1fr 1fr }` at ≤800px) and `4362`
(`.ai-spend-day-event .ai-spend-row`) have **equal specificity; 4401 wins by source order**.
The modal's 6-cell row collapsed to three unlabelled pairs in a 342px box, with no way to tell
cost from model. The `.ai-spend-row.head` collapsed identically into 3 meaningless pairs.
**Command:** `/impeccable adapt`

### [P0] The chart is frozen on the last day
`onMouseMove`/`onMouseLeave` with no touch binding. On a phone the tooltip can never leave
`activeIndex = hover ?? last`, so the chart is decoration and "which day was abnormal?" is
unanswerable. **Command:** `/impeccable adapt`

### [P1] The cross-filter gives zero feedback
Tap "Транскрибация" → smooth scroll → a journal that looks subtly different with no banner. Only
`attentionFilter` showed a note; `kindGroup`, `status` and `search` showed nothing.
**Command:** `/impeccable clarify`

### [P1] Primary control stranded atop a 5-screen page
Period, search and reset all live in the header; nothing on the page is `position: sticky`.
**Command:** `/impeccable adapt`

### [P2] 15 of 17 control types under 44pt
Worst: `.ai-spend-chart .ai-spend-pills button` at `min-height: 24px` — the *primary* view controls.
The search input measured 36px at `font-size: 13px`, under the 16px iOS zoom threshold.
**Command:** `/impeccable adapt`

### [P2] 21 of 30 days are "0 платных вызовов ~0 ₽"
~70% of the day list and chart is identical zero rows, which reads as "the counter is broken".
**Command:** `/impeccable distill`

## Persona Red Flags

**Casey (Distracted Mobile User).** `preset`, `metric`, `search`, `page` are plain `useState` —
**everything is lost on refresh**. The 208px day list traps the vertical drag. The answer itself is
a non-interactive `<article>` in the top-right, ~150px from the bottom edge.

**Sam (Accessibility-Dependent).** Zero `:focus-visible` on **any** `.ai-spend-*` selector.
All three journal controls unlabelled — the selects' accessible name was their first `<option>`,
so choosing "ошибка" made the field announce itself as "ошибка". Two AA contrast failures:
`.ai-spend-delta span` ≈2.8:1 and `.ai-spend-chart-labels` ≈3.4:1 — the axis labels and the
period-over-period annotation. No focus trap in the modal: `aria-modal="true"` was a promise the
DOM didn't keep.

**Jordan (First-Timer).** Two visually identical pill groups in the same header differing only by
size, with the fundamental choice ("what am I measuring?") being the smaller one. `Openai`
appeared as an operation type.

## Minor Observations

- `role="tablist"` on the period pills with no `role="tab"` children and no `aria-selected`.
- `aria-label="Тип операций"` on a plain `<div>` is ignored.
- Duplicate breakdown label "Полный скрипт" appears twice in live data.
- The `₽` teaser glyph is green — a status colour on something that isn't a status.
- `AiSpendDashboardCard` swallows fetch errors and renders `'…'` forever.
- `.ai-spend-entities a` sits next to a ~22px button as a sibling — a mis-tap pair.

## Questions to Consider

1. Should a raw journal exist on a phone at all?
2. What is the unit — roubles, or roubles per deal?
3. Should silence and waste share a view, or should the screen open with one verdict line?
4. Is a 2.2:1-squeezed chart with three metric pills earning its place on a phone?
