"""
view/app.py

Streamlit dashboard. Read-only against data/trials.db, with one exception:
the "Ask" tab calls out to Claude and to the live CT.gov API, via the exact
same cli/ask.py pipeline used from the command line. That reuse is
deliberate -- the query-planning, validation, ranking, and landscape logic
lives in exactly one place (cli/ask.py + enrich/), and this file never
re-implements any of it. If that pipeline changes, the CLI and the
dashboard change together, because they're calling the same function.

Three tabs, matching the scope doc:
  1. Search    -- free text + filters over the local `trials` table.
  2. Detail    -- registry fields plus the three cached summary sections
                  for whichever trial is selected in Search.
  3. Change    -- trials whose materiality hash has moved since they were
     feed        first pulled. See the module docstring on
                  `render_change_feed()` for an honest note on what this
                  tab can and can't show, given what's actually stored.
  4. Ask       -- free-text question -> planned, validated query ->
                  ranked local matches -> landscape synthesis. Dropped
                  terms are always shown, never hidden, matching the
                  project's stated design rule.

Run with:
    streamlit run view/app.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import streamlit as st

# view/ is a sibling of cli/, enrich/, normalize/, common/ -- add the repo
# root so `from cli.ask import ...` resolves regardless of the working
# directory streamlit was launched from. Same pattern used throughout the
# rest of the repo (query_planner.py, ask.py, pull_trials.py).
HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))

DB_PATH = REPO_ROOT / "data" / "trials.db"

PHASES = ["PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]
STATUSES = ["RECRUITING", "ACTIVE_NOT_RECRUITING", "COMPLETED", "TERMINATED", "WITHDRAWN"]


# ---------------------------------------------------------------------
# Data access. All read-only. Cached with a short TTL rather than
# forever, because a normalize/enrich run can update data/trials.db
# while the app is sitting open in a browser tab, and this is a demo
# tool, not a service with a push-refresh channel.
# ---------------------------------------------------------------------

@st.cache_resource
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_data(ttl=30)
def load_trials() -> list[dict]:
    """
    Everything needed for Search + Detail + Change feed in one query.
    A few hundred trials is small enough to hold in memory and filter
    with pandas/plain Python rather than pushing every filter combination
    down into SQL -- consistent with the scope doc's "no vector search,
    SQL LIKE is enough at this scale" stance.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT t.nct_id, t.title, t.phase, t.overall_status, t.sponsor,
               t.enrollment, t.primary_completion_date, t.why_stopped,
               t.has_results, t.conditions, t.interventions, t.arms,
               t.primary_outcome_measures, t.therapeutic_area,
               t.materiality_hash, t.first_seen_at, t.last_changed_at,
               s.scientific_summary, s.potential_risks, s.business_impact,
               s.materiality_hash AS summary_hash, s.generated_at
        FROM trials t
        LEFT JOIN summaries s ON s.nct_id = t.nct_id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [raw]


def _text_blob(trial: dict) -> str:
    return " ".join(
        [trial.get("title") or "", trial.get("conditions") or "", trial.get("interventions") or ""]
    ).lower()


# ---------------------------------------------------------------------
# Search tab
# ---------------------------------------------------------------------

def render_search(trials: list[dict]):
    st.subheader("Trial search")
    st.caption(
        "Free text plus phase / status / sponsor / therapeutic-area filters, "
        "run against the local database -- no network call happens here."
    )

    query_col, area_col = st.columns([3, 1])
    with query_col:
        text = st.text_input(
            "Search title, conditions, or interventions",
            key="search_text",
            placeholder="e.g. rezafungin, candidemia, aspergillosis",
        )
    with area_col:
        areas = sorted({t["therapeutic_area"] for t in trials if t.get("therapeutic_area")})
        area = st.selectbox("Therapeutic area", ["Any"] + areas, key="search_area")

    f1, f2, f3 = st.columns(3)
    with f1:
        phase_sel = st.multiselect("Phase", PHASES, key="search_phase")
    with f2:
        status_sel = st.multiselect("Status", STATUSES, key="search_status")
    with f3:
        sponsors = sorted({t["sponsor"] for t in trials if t.get("sponsor")})
        sponsor_sel = st.multiselect("Sponsor", sponsors, key="search_sponsor")

    results = trials
    if text:
        needle = text.lower()
        results = [t for t in results if needle in _text_blob(t)]
    if area != "Any":
        results = [t for t in results if t.get("therapeutic_area") == area]
    if phase_sel:
        results = [
            t for t in results
            if set((t.get("phase") or "").split("/")) & set(phase_sel)
        ]
    if status_sel:
        results = [t for t in results if t.get("overall_status") in status_sel]
    if sponsor_sel:
        results = [t for t in results if t.get("sponsor") in sponsor_sel]

    st.write(f"{len(results)} of {len(trials)} trial(s) match.")

    table_rows = [
        {
            "NCT ID": t["nct_id"],
            "Title": t.get("title") or "",
            "Phase": t.get("phase") or "",
            "Status": t.get("overall_status") or "",
            "Sponsor": t.get("sponsor") or "not reported",
            "Summarized": "yes" if t.get("scientific_summary") else "no",
        }
        for t in results
    ]
    st.dataframe(table_rows, hide_index=True, use_container_width=True)

    if results:
        options = [t["nct_id"] for t in results]
        current = st.session_state.get("detail_nct_id")
        index = options.index(current) if current in options else 0
        selected = st.selectbox(
            "Select an NCT ID to open in the Detail tab",
            options,
            index=index,
            key="search_select",
        )
        st.session_state["detail_nct_id"] = selected
        st.info("Open the **Detail** tab above to see the full record for this selection.")


# ---------------------------------------------------------------------
# Detail tab
# ---------------------------------------------------------------------

def render_detail(trials: list[dict]):
    st.subheader("Trial detail")

    by_id = {t["nct_id"]: t for t in trials}
    default = st.session_state.get("detail_nct_id")
    options = sorted(by_id.keys())
    if not options:
        st.warning("No trials in the database yet.")
        return

    index = options.index(default) if default in options else 0
    nct_id = st.selectbox("NCT ID", options, index=index, key="detail_select")
    st.session_state["detail_nct_id"] = nct_id
    t = by_id[nct_id]

    st.markdown(f"### {t.get('title') or nct_id}")
    st.caption(f"[{nct_id}](https://clinicaltrials.gov/study/{nct_id})")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Phase", t.get("phase") or "not reported")
    c2.metric("Status", t.get("overall_status") or "not reported")
    c3.metric("Enrollment", t.get("enrollment") or "not reported")
    c4.metric("Has results", "yes" if t.get("has_results") else "no")

    st.markdown("**Registry fields**")
    reg1, reg2 = st.columns(2)
    with reg1:
        st.write(f"Sponsor: {t.get('sponsor') or 'not reported'}")
        st.write(f"Primary completion date: {t.get('primary_completion_date') or 'not reported'}")
        st.write(f"Therapeutic area: {t.get('therapeutic_area') or 'not tagged'}")
    with reg2:
        st.write(f"Conditions: {', '.join(_json_list(t.get('conditions'))) or 'not reported'}")
        st.write(f"Interventions: {', '.join(_json_list(t.get('interventions'))) or 'not reported'}")

    if t.get("why_stopped"):
        st.warning(f"Why stopped: {t['why_stopped']}")

    with st.expander("Arms and primary outcome measures (raw registry text)"):
        st.write("**Arms**")
        for a in _json_list(t.get("arms")):
            st.write(f"- {a}")
        st.write("**Primary outcome measures**")
        for o in _json_list(t.get("primary_outcome_measures")):
            st.write(f"- {o}")

    st.divider()
    st.markdown("**Claude-generated summary** (cached; regenerated only when the trial's "
                "materiality hash changes)")

    if not t.get("scientific_summary"):
        st.info(
            "Not summarized yet. Run `enrich/run_enrichment.py` to generate the three "
            "sections below for this trial."
        )
        return

    if t.get("summary_hash") != t.get("materiality_hash"):
        st.warning(
            "This trial changed materially after its summary was generated. The text "
            "below reflects the older version -- re-run enrichment to refresh it."
        )

    st.markdown("*Scientific summary*")
    st.write(t["scientific_summary"])
    st.markdown("*Potential risks*")
    st.write(t.get("potential_risks") or "not generated")
    st.markdown("*Business impact*")
    st.write(t.get("business_impact") or "not generated")
    st.caption(f"Generated at: {t.get('generated_at') or 'unknown'}")


# ---------------------------------------------------------------------
# Change feed tab
# ---------------------------------------------------------------------

def render_change_feed(trials: list[dict]):
    st.subheader("Change feed")
    st.caption(
        "Trials sorted by most recently changed. Honest limitation: the schema stores "
        "one current materiality_hash per trial, not a history of prior values (see "
        "scope doc, 'Decisions changed from v0' -- full snapshot history was cut as "
        "out of scope for a project with no scheduled runs). This feed can tell you "
        "*that* and *when* a trial last changed, and flags trials whose cached summary "
        "is now stale against the current hash -- it cannot show a field-by-field diff "
        "of what changed, because that value was never persisted."
    )

    changed = [t for t in trials if t.get("last_changed_at") and t["last_changed_at"] != t.get("first_seen_at")]
    changed.sort(key=lambda t: t["last_changed_at"], reverse=True)

    stale = [t for t in trials if t.get("scientific_summary") and t.get("summary_hash") != t.get("materiality_hash")]

    m1, m2 = st.columns(2)
    m1.metric("Trials changed since first pull", len(changed))
    m2.metric("Cached summaries now stale", len(stale))

    if not changed:
        st.info("No trial has changed since it was first pulled -- every last_changed_at "
                 "still matches its first_seen_at.")
        return

    for t in changed:
        stale_flag = " -- summary is stale" if t in stale else (
            " -- not yet summarized" if not t.get("scientific_summary") else ""
        )
        with st.expander(f"{t['nct_id']} · {t.get('title') or 'untitled'}{stale_flag}"):
            st.write(f"First seen: {t.get('first_seen_at')}")
            st.write(f"Last changed: {t.get('last_changed_at')}")
            st.write(f"Phase: {t.get('phase') or 'not reported'} · "
                      f"Status: {t.get('overall_status') or 'not reported'}")
            if t.get("why_stopped"):
                st.warning(f"Why stopped: {t['why_stopped']}")


# ---------------------------------------------------------------------
# Ask tab -- the only tab that leaves the local database.
# ---------------------------------------------------------------------

def render_ask():
    st.subheader("Ask")
    st.caption(
        "Runs the exact pipeline in cli/ask.py: Claude proposes candidate search terms, "
        "every one is checked against the live CT.gov registry before it's allowed to "
        "reach a query, matches are found locally, and Claude writes one competitive-"
        "landscape paragraph across the results. Dropped terms are always shown below, "
        "never hidden -- that's the point of the guardrail."
    )

    question = st.text_input(
        "Ask a question about the antifungal / oncology trial landscape",
        placeholder="e.g. what's competing with rezafungin in refractory candidemia?",
        key="ask_question",
    )
    top_n = st.slider("Local matches to consider", min_value=5, max_value=30, value=15, key="ask_top_n")
    run = st.button("Ask", type="primary", disabled=not question)

    if not run:
        return

    if not DB_PATH.exists():
        st.error(f"No database at {DB_PATH}. Run normalize/build_db.py first.")
        return

    try:
        from cli.ask import answer_question  # imported lazily: touches Anthropic + CT.gov
    except ImportError as e:
        st.error(f"Could not import cli/ask.py: {e}")
        return

    with st.spinner("Planning query, validating terms against the live registry, ranking, synthesizing..."):
        try:
            result = answer_question(question, top_n=top_n)
        except Exception as e:
            st.error(
                f"The Ask pipeline raised an error: {e}\n\n"
                "Common cause: ANTHROPIC_API_KEY is not set in the repo's .env file."
            )
            return

    if result.get("out_of_scope"):
        st.info(f"**Interpretation:** {result['interpretation']}")
        st.warning(f"Out of scope: {result['out_of_scope']}")
        return

    st.info(f"**Interpretation:** {result['interpretation']}")

    if result["dropped_terms"]:
        st.warning(
            "**Dropped terms** -- proposed by Claude, zero hits against the live registry, "
            "never reached a query:\n\n" + "\n".join(f"- {t}" for t in result["dropped_terms"])
        )

    st.write(f"{result['total_matches']} trial(s) matched locally; showing top {result['shown']}.")

    if result["landscape"]:
        st.markdown("### Competitive landscape")
        st.write(result["landscape"])
    else:
        st.info(
            "No matching trials have a cached summary yet, so no landscape synthesis was "
            "generated. Run enrich/run_enrichment.py, then ask again."
        )

    if result["with_summary"]:
        st.markdown(f"### Trials included in the synthesis ({len(result['with_summary'])})")
        for t in result["with_summary"]:
            st.write(
                f"**{t['nct_id']}** {t.get('title') or ''} -- "
                f"phase={t.get('phase') or '?'}, status={t.get('overall_status') or '?'}, "
                f"sponsor={t.get('sponsor') or 'not reported'}"
            )

    if result["without_summary"]:
        st.markdown(f"### Matched but not yet summarized ({len(result['without_summary'])})")
        st.caption("Excluded from the synthesis above rather than silently folded in. "
                   "Run enrich/run_enrichment.py to include these next time.")
        for t in result["without_summary"]:
            st.write(
                f"- {t['nct_id']} ({t.get('phase') or 'phase unknown'}, "
                f"{t.get('overall_status') or 'status unknown'})"
            )


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def main():
    st.set_page_config(page_title="TrialLens", layout="wide")
    st.title("TrialLens")
    st.caption(
        "Clinical trial intelligence over ClinicalTrials.gov, scoped to antifungal / "
        "anti-infective and oncology trials. Search and Detail and Change feed are "
        "entirely local and free to reload; Ask calls Claude and the live registry."
    )

    if not DB_PATH.exists():
        st.error(
            f"No database found at `{DB_PATH}`.\n\n"
            "Run the pipeline first:\n"
            "1. `python ingest/pull_trials.py`\n"
            "2. `python normalize/build_db.py`\n"
            "3. (optional) `python enrich/run_enrichment.py` for cached summaries"
        )
        st.stop()

    if st.button("Refresh from disk"):
        load_trials.clear()

    trials = load_trials()

    tab_search, tab_detail, tab_changes, tab_ask = st.tabs(
        ["Search", "Detail", "Change feed", "Ask"]
    )
    with tab_search:
        render_search(trials)
    with tab_detail:
        render_detail(trials)
    with tab_changes:
        render_change_feed(trials)
    with tab_ask:
        render_ask()


if __name__ == "__main__":
    main()
