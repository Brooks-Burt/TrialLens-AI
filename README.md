# Clinical trial intelligence

A tool for searching clinical trials (via CT.gov) across therapeutic areas
and stages, enriched with Claude-generated summaries, viewed through a
simple search/filter dashboard.

## Architecture

Four independent domain modules, each reading from or writing to a
shared `data/` directory — `data/` is the only interface between them.
The one exception: `ingest` and `enrich` both import `common/`, a thin,
stateless CT.gov API client with no domain logic (no parsing, no
disk writes, no Claude calls). Sharing that utility avoids duplicating
retry/backoff logic; it isn't ingest and enrich depending on each other.

```
common/ctgov_client.py           (shared CT.gov API v2 wrapper — no domain logic)

ingest    --> data/raw/*.json    (bulk pulls via common/, no parsing)
normalize --> data/trials.db     (raw JSON -> SQLite tables)
enrich    --> data/trials.db     (Claude summaries, cached per trial;
                                   also uses common/ to live-validate
                                   candidate query terms before a
                                   natural-language search runs)
view      <-- data/trials.db     (search + filter UI, read-only)
```

## Status

- [x] Repo scaffolded
- [ ] `ingest` — pulling antifungal/anti-infective trials (in progress)
- [ ] `normalize`
- [ ] `enrich`
- [ ] `view`

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r ingest/requirements.txt
```

## Running ingest

```bash
# smoke test — one page only
python ingest/pull_trials.py --area antifungals --max-pages 1

# full pull
python ingest/pull_trials.py --area antifungals

# re-pull, overwriting existing files
python ingest/pull_trials.py --area antifungals --refresh
```

Writes one raw JSON file per trial to `data/raw/{nctId}.json`.
