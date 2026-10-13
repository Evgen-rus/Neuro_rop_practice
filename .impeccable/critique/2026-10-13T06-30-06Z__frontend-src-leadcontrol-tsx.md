---
target: Таблица контроля лидов
total_score: 18
max_score: 28
na_heuristics: 5,9,10
p0_count: 0
p1_count: 1
target_identity: "file:D:\\My_dev_project\\Neuro_rop_practice\\frontend\\src\\LeadControl.tsx"
target_fingerprint: "sha256:251b19a36e647fc8ead2a0051d699f740960cfd16fef228a513f3a6664c09a0f"
target_path: "D:\\My_dev_project\\Neuro_rop_practice\\frontend\\src\\LeadControl.tsx"
timestamp: 2026-10-13T06-30-06Z
slug: frontend-src-leadcontrol-tsx
---
Method: dual-agent (A: table_visual_critic; B: table_evidence_critic)

Target: frontend/src/LeadControl.tsx, table only. Visual evidence: user-supplied desktop screenshot.

The existing dashboard identity is coherent: restrained blue, white surfaces and compact typography. Spatial hierarchy needs refinement rather than a new visual identity.

| Heuristic | Score | Evidence |
|---|---|---|
| Status visibility | 3 | Report availability clear, business state visually flat |
| Business language | 2 | unknown and legacy notes interrupt reading |
| User control | 3 | Clear report/history actions; interactions untested |
| Consistency | 3 | Coherent palette, oversized controls |
| Error prevention | n/a | Static screenshot insufficient |
| Recognition | 3 | Clear columns, empty disclosure misleading |
| Efficiency | 2 | Tall rows and sparse action columns |
| Minimalism | 2 | Repeated technical notes and borders |
| Error recovery | n/a | No evidence |
| Help | n/a | No evidence |
| Total | 18/28 | Refinement needed |

Strengths: readable lead titles, recognizable primary report action, predictable columns.

P1: First row height doubles through legacy qualification information. Collapse these facts under one disclosure without removing data.
P2: History and analysis occupy wide sparse columns. Discuss combining as Actions to free width for substantive text.
P2: Status hierarchy is flat. Use existing soft stage colors and subtle next-step emphasis only for factual states; no inferred overdue colors.
P2: Empty recommendation displays an expandable dash. Use a quiet noninteractive absence state.
P3: Reduce control padding, preserve existing 13px body typography, soften vertical dividers.

Minimal proposal retains columns and compresses details. Alternative: checkbox / lead / responsible plus stage / next step / actions.

Cognitive load: technical missing-data details add reading without helping a decision. ROP persona sees few leads per screen; newcomer may read unknown and legacy notes as errors.

Detector: zero findings, JSON []. Source confirms 13px body tokens, vertically centered cells and responsive card layout below 1100px. Automated cleanliness does not validate visual hierarchy.

Limits: browser security policy blocks live inspection. Screenshot and source only; mobile, keyboard behavior and contrast measurement unverified.
