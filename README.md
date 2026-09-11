# TrialLens

Clinical trial intelligence over ClinicalTrials.gov. Detects which trials
have actually changed, writes readable summaries of those, and answers
plain-English questions about a therapeutic area without letting the model
invent drugs that don't exist.

Python, SQLite, Streamlit, Claude.

## Why I built this

I've spent enough time in antifungals to know what tracking a therapeutic
area actually looks like, and most of it is not analysis. It's opening
ClinicalTrials.gov, paging through records you've already read, and trying to
notice which of the handful that changed since last month changed in a way
that matters. A site coordinator's phone number gets updated and the record
looks new. Enrollment drops by 40% and it looks the same.

The obvious fix is to point a language model at the registry and ask it
questions. I tried that first and stopped, because it fails in a specific and
dangerous way: ask a model to name antifungals in development and it will
eventually give you one that has never existed, described fluently and with
total confidence. In a competitive intelligence context that isn't a cute
demo failure. It's the thing that ends the meeting.

So TrialLens splits the work by what each side is actually good at.

Python decides what's worth looking at. Which trials changed, which fields
count as a real change, how results get ranked. All deterministic, all
reproducible, all explainable to someone who asks why a trial showed up.

Claude does the two jobs code genuinely can't. It turns a dense registry
record into something a non-scientist can read, and it translates a question
like "what's competing with rezafungin in refractory candidemia?" into search
terms. That second one is the interesting case, because keyword search cannot
do it at all. Competitors to a drug share no words with that drug. There is
no "competitor" field in the registry. Answering it requires knowing which
compounds share a mechanism, which is real domain knowledge and a reasonable
thing to ask a model for.

The catch is that it's also exactly where the model will make something up.
Which is the next section.

## The guardrail

This is the part I'd point at first if you only read one thing.

The query planner never searches for anything directly. It *proposes*
candidate drug names, each with a confidence rating and a rationale. Every
single proposed term then gets checked against the live CT.gov API with a
count-only call. Zero hits means the term is dropped, logged, and reported
back in a `dropped_terms` field that the dashboard displays on screen.

Claude proposes. The registry disposes.

I tested this with a drug I made up. Asked whether `zorbafungin` was being
studied in candidemia trials, the planner came back with:

```json
{
  "term": "zorbafungin",
  "confidence": "high",
  "rationale": "Zorbafungin is a real investigational antifungal compound
                in clinical development...",
  "registry_hits": 0
}
```

High confidence, fluent rationale, completely fabricated. The check caught
it anyway, because the check doesn't read confidence. It reads a hit count
from the live registry:

```
WARNING: All candidate interventions failed validation for question:
'is zorbafungin being studied in any candidemia trials?'
```

That's the design point, and it's why the validation exists as a separate
step rather than a stern instruction in the prompt. A model's stated
certainty tells you nothing about whether it's right, so nothing in this
system is allowed to depend on it.

Two smaller decisions inside that:

The validation is not cached. Whether a drug exists in the registry is a
fact about the registry right now, not about my local copy of it. Something
registered yesterday should validate today.

Dropped terms are shown, not swallowed. When the guardrail fires, you see
what got rejected. A tool that silently discards a third of its own search
plan is worse than one that tells you.

## How it works

Five stages. Each one is a script you run on purpose, and each one is safe
to re-run.

```
                     CT.gov API v2                Anthropic API
                           |                            |
              +------------+------------+               |
              |                         |               |
              v                         v               v
       ingest/pull_trials.py    common/ctgov_client.py  enrich/client.py
              |                  (shared HTTP wrapper)  (only Claude entry point)
              v                         ^                    ^
       data/raw/*.json                  |                    |
              |                         |                    |
              v                         |                    |
     normalize/build_db.py              |                    |
     normalize/materiality.py           |                    |
              |                         |                    |
              v                         |                    |
       data/trials.db  <----------------+--------------------+
              |            enrich reads `trials`, writes `summaries`
              |
              +-----------------+
              v                 v
       cli/ask.py         view/app.py  (Streamlit)
```

**1. Ingest.** Pulls full study records for one therapeutic area at a time,
defined in `ingest/query_config.json`, and writes one raw JSON file per trial
to `data/raw/`. No parsing, no database, no Claude. Re-running skips files
that already exist unless you pass `--refresh`.

**2. Normalize.** Parses the raw JSON into SQLite and computes a materiality
hash for each trial. This is the change detection layer and it's one column,
not a diff engine. A whitelist of fields that actually signal something
(status, phase, enrollment, primary completion date, `whyStopped`, arms,
primary outcomes) gets serialized in a canonical form and SHA-256'd. Contact
names, facility addresses and record verification dates are deliberately
excluded, because they change constantly and mean nothing.

**3. Enrich.** Sends trials to Claude for a three-part summary: scientific
summary, potential risks, business impact. It only sends trials that are new
or whose materiality hash has moved since the last summary was written.
Everything else is skipped at zero cost. A second run over an unchanged
dataset reports `0 trial(s) need enrichment` and spends nothing.

**4. Ask.** Takes a question, runs the planner and its validation, searches
the local database, ranks matches deterministically (phase, then status, then
recency) and generates one competitive landscape paragraph across the whole
result set. One Claude call per question, not per trial. Any matching trial
that hasn't been summarized yet is listed separately rather than quietly left
out of the paragraph.

**5. View.** Streamlit dashboard. Ask is the landing page. Browse, trial
detail and a change feed are behind it in the sidebar. Everything except Ask
is read-only against the local database and free to reload as much as you
like.

Current dataset: 175 antifungal trials pulled, 47 with cached summaries. The
gap is on purpose, see limitations.

## Setup

```bash
git clone https://github.com/Brooks-Burt/TrialLens-AI.git
cd TrialLens-AI
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
```

## Usage

Cold start, in order:

```bash
# 1. pull trials (free). --max-pages for a quick smoke test
python ingest/pull_trials.py --area antifungals --max-pages 1
python ingest/pull_trials.py --area antifungals

# 2. build the database and compute hashes (free)
python normalize/build_db.py --area antifungals --dry-run
python normalize/build_db.py --area antifungals

# 3. summarize (this is the part that costs money, so check first)
python enrich/run_enrichment.py --dry-run
python enrich/run_enrichment.py --limit 5

# 4a. ask from the terminal
python cli/ask.py "what's competing with rezafungin in refractory candidemia?"

# 4b. or run the dashboard
python -m streamlit run view/app.py
```

Use `python -m streamlit`, not bare `streamlit`. The bare command runs
whichever streamlit is first on your PATH, which may be tied to a different
interpreter than the one holding your dependencies. You'll get a working app
that fails with `No module named 'anthropic'` the moment you ask a question.
`docs/SETUP.md` has the longer explanation.

You can also run the planner on its own, which is the fastest way to see the
guardrail work:

```bash
python enrich/query_planner.py "is zorbafungin being studied in candidemia?"
```

## How it was made

Notes on the build, mostly for anyone reading the code and wondering why it's
shaped this way.

**Four modules that don't import each other.** `ingest`, `normalize`,
`enrich` and `view` communicate only through files in `data/`. Any one can be
rewritten or run alone. The single exception is `common/`, a stateless HTTP
wrapper with no domain knowledge, shared because the alternative was
duplicating retry and backoff logic in two places.

**One Claude entry point.** Every API call goes through `enrich/client.py`,
so retries, backoff and defensive output parsing are defined once. When
Claude started wrapping its JSON in markdown fences despite being told not
to, the fix went in one function instead of five call sites.

**One file touches SQLite.** `prompts.py`, `client.py`, `summarize.py` and
`landscape.py` take dicts and return dicts. Only `run_enrichment.py` opens a
database connection. Swapping SQLite for something else touches one file and
zero prompts.

**Things I cut mid-build.** Originally this was going to end in a Power BI
dashboard. I dropped it for Streamlit, because a `.pbix` binary in git can't
be reviewed or run by anyone reading the repo. I also cut a `snapshots` plus
`changes` table pair down to the single materiality hash column, since full
snapshot history is a feature of a monitoring product and this has no
scheduled runs, so there's no history to accumulate. And I skipped the Batch
API, which would halve enrichment cost but adds an async polling path to save
a few dollars at this scale.

**Bugs I found by running it, not reading it.** Six of them, and the
uncomfortable thing is that most weren't crashes. The worst was a SELECT that
never fetched `materiality_hash` from the database, so every summary got
written with an empty-string hash, so every subsequent run compared `""`
against a real hash, decided every trial had changed, and re-enriched the
entire dataset. No error, no warning, no symptom except the bill. I only
caught it by running enrichment twice and noticing the second run should have
cost nothing and didn't.

The others: a hardcoded relative DB path that silently created a second empty
database when run from the wrong folder, bare imports that only worked when
launched from the repo root, a `KeyError` on out-of-scope questions because
the model omitted a field the code indexed into directly, a missing
`load_dotenv()` that turned a missing API key into a raw `KeyError`, and a
prompt whose stated scope was broader than the data I'd actually pulled.

I also deliberately tried to break it. Interrupted an enrichment run with
Ctrl-C mid-call (the in-flight trial was never written, completed ones
weren't duplicated, resuming picked up where it left off). Fed a trial with
enrollment, arms and outcomes all null into the summarizer to check it would
name the gaps instead of inventing numbers. Asked an out-of-scope question
about Alzheimer's. Asked about a drug that doesn't exist.

## Limitations

Being explicit about these, since a few were found in testing rather than
designed around.

**Fungal only, despite what the planner says.** `query_config.json` has
antifungal terms and nothing else, so oncology data doesn't exist locally
even though the planner's prompt mentions it as in scope. This is the shape
of a bug I already hit once: I asked about bacterial pneumonia, the planner
reasonably read that as an anti-infective question and confidently built a
fully validated query, and it would have returned nothing against my actual
data. Prompt scope and pulled data are two independent sources of truth and
nothing enforces agreement between them automatically.

**Only 47 of 175 trials are summarized.** I capped the backfill to control
token spend. Because summaries cache against the materiality hash, finishing
it later is a one-time cost and re-runs after that are free.

**Trial level, not asset level.** Drug names aren't normalized across trials,
so the same compound under a generic and a brand name counts twice. Doing
this properly needs an external drug ontology.

**Registry fields only.** No FDA filings, patents or press releases. If
CT.gov doesn't publish it, TrialLens doesn't know it.

**No inferred milestones.** It won't tell you when a readout is expected
unless the registry states a date. Anything shown as an estimate is labelled
as one with its basis visible. This is enforced in `prompts.py`, and I
verified it by feeding in a record with everything important set to null and
checking the output named the missing fields instead of writing around them.

**The change feed can't show a diff.** It tells you that a trial changed and
when, and flags summaries that are now stale. It can't show you which field
moved, because only the current hash is stored, not the previous one. That's
the cost of the snapshot tables I cut.

**Manual everything.** No scheduler, no server, no auth, no hosting. Every
run is deliberate.

## Stack

Python, ClinicalTrials.gov API v2, SQLite, Streamlit, Anthropic Claude
(Haiku 4.5 for query planning since it's a classification task, Sonnet for
summaries and landscape synthesis).
