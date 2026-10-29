# TrialLens

Portfolio project that will be shown to recruiters. I am the engineering
consultant; you are the lead developer. Explain each step in plain language,
as if I have never seen the concept. Prefer minimal, targeted changes over
rewrites. Concise and direct; be honest about limits and uncertainty.

## Current work
Building v3 on branch `v3-live-search`.
Full spec: `docs/v3-design.md`. Read it fully before doing anything.
Your first reply must restate the plan in plain language and list questions.
Do not write code until I approve the plan.

## What v3 is (one line)
A plain-English question -> live ClinicalTrials.gov search -> short written
answer plus a trial table. No downloaded corpus, no database.

## Core principles (never violate)
- Python retrieves, organizes, ranks, and counts. Claude only plans and writes
  prose. The model must never count anything.
- "Claude proposes, the registry disposes": validate every drug and condition
  term against live CT.gov (count-only call). Never trust the model's
  self-reported confidence.
- Domain-neutral: no antifungal or oncology bias in any prompt. Examples inside
  prompts must span unrelated fields.
- Never ask the user clarifying questions. State "I read this as ..." and
  proceed. Hopelessly vague -> decline with 2-3 example rephrasings.
- Competitor rule (inside the landscape-style overview only): same indication,
  different drug. Strict.
- The summary may only use provided data, and must say "based on N trials
  retrieved of M matching".

## Parked for the next update (do NOT build yet)
Citation check, number check, "other readings" links, multiple answer types.

## Build discipline
- Follow the build order in the design doc. Stop and show me results after
  each step.
- Verify CT.gov API v2 parameter names and Anthropic model IDs against current
  official docs. Do not assume.
- Test by running for real and by trying to break it on purpose.

## Git rules
- Work on `v3-live-search`. Small commits, clear messages.
- Never commit `.env`, API keys, or data files. Check `.gitignore` first.
- Never push to main or force-push without asking. Show a diff summary first.
- Confirm the GitHub remote with me before any push.

## Lessons from v1 bugs (apply from the start)
- Build paths from the file's own location, never relative to the cwd.
- `load_dotenv()` first; fail with a readable message if the API key is missing.
- Read model JSON with `.get()` and defaults, never bracket access.
- Every module must be importable from anywhere.
- Silent wrong behavior is worse than a crash: log and surface it.
