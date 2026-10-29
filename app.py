"""
app.py -- TrialLens v3, one page.

    question -> "I read this as ..." -> written answer -> trial table -> "Show your work"

Run:
    .venv/bin/streamlit run app.py

Everything shown comes from pipeline.run.answer(), which never raises. The
answer is kept in session state because Streamlit reruns this whole script
on every click: flipping a toggle must not re-run the search (or re-bill
the model).
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline.llm import PLANNER_MODEL, WRITER_MODEL  # noqa: E402
from pipeline.organize import SUMMARY_TRIAL_CAP  # noqa: E402
from pipeline.planner import MAX_QUESTION_CHARS  # noqa: E402
from pipeline.retrieve import (  # noqa: E402
    BOTH_ARMS, COMPARATOR_ONLY, NOT_IN_DRUG_LIST, OTHER_NAME, PER_GROUP_CAP, ROLE_UNCLEAR, TESTED,
)
from pipeline.run import ANSWERED, DECLINED, FAILED, Answer, answer  # noqa: E402
from pipeline.validate import DROPPED, KEPT, UNVERIFIED  # noqa: E402

# Examples span unrelated fields on purpose (domain-neutral rule).
EXAMPLES = [
    "What else is being tested for invasive candidiasis besides fluconazole?",
    "Recruiting phase 3 trials for heart failure with preserved ejection fraction",
    "Which JAK inhibitors are being tested in alopecia areata?",
]

ROLE_SHORT = {
    TESTED: "tests it", OTHER_NAME: "lists it (other name)", ROLE_UNCLEAR: "lists it (role unclear)",
    COMPARATOR_ONLY: "comparator only", BOTH_ARMS: "background (all arms)", NOT_IN_DRUG_LIST: "not in drug list",
}
CHECK_LABEL = {KEPT: "kept", DROPPED: "dropped (0 trials)", UNVERIFIED: "could not verify"}


def md(text: str) -> str:
    """
    Escape model-written text before st.markdown shows it. HTML is already
    inert in Streamlit, but Markdown is not: a "[link](url)" echoed from a
    question would become a clickable link, and "$" starts math rendering.
    """
    return "".join("\\" + ch if ch in "\\`*_[]()<>#|~$" else ch for ch in text or "")


def fmt_phase(phases: list[str]) -> str:
    if not phases:
        return "-"
    names = {"EARLY_PHASE1": "Early 1", "PHASE1": "1", "PHASE2": "2", "PHASE3": "3", "PHASE4": "4", "NA": "N/A"}
    return "Phase " + "/".join(names.get(p, p) for p in phases)


def fmt_status(s: str) -> str:
    return s.replace("_", " ").capitalize() if s else "-"


# --- state ---------------------------------------------------------------

def _ask(q: str) -> None:
    """Button callback: put a question in the box and run it."""
    st.session_state.q_input = q
    st.session_state.pending = q


st.set_page_config(page_title="TrialLens", page_icon=None, layout="centered")
st.session_state.setdefault("q_input", "")
st.session_state.setdefault("pending", None)
st.session_state.setdefault("answer", None)

st.title("TrialLens")
st.caption("Ask a question about clinical trials. TrialLens searches the live ClinicalTrials.gov registry "
           "and writes a short answer based only on the trials it found, shown beside the trials themselves.")

with st.form("ask", border=False):
    st.text_input("Your question", key="q_input", max_chars=MAX_QUESTION_CHARS,
                  placeholder="e.g. What is being tested for migraine?")
    if st.form_submit_button("Search", type="primary"):
        st.session_state.pending = st.session_state.q_input

st.caption("Examples:")
cols = st.columns(len(EXAMPLES))
for col, ex in zip(cols, EXAMPLES):
    col.button(ex, on_click=_ask, args=(ex,), width="stretch")

if st.session_state.pending is not None:
    q, st.session_state.pending = st.session_state.pending, None
    with st.spinner("Reading the question, checking terms against the registry, searching, writing..."):
        st.session_state.answer = answer(q)


# --- rendering -------------------------------------------------------------

def render_table(a: Answer) -> None:
    rows = a.facts.rows
    def off_topic(row):
        return all(role == NOT_IN_DRUG_LIST for _, role, _ in row["groups"])
    hidden = sum(off_topic(r) for r in rows)
    show_all = False
    if hidden:
        show_all = st.toggle(f"Also show {hidden} trial{'s' if hidden != 1 else ''} that matched the search text "
                             "but do not list the drug as an intervention")
    data = []
    for row in rows:
        if off_topic(row) and not show_all:
            continue
        t = row["trial"]
        drugs = [g for g, _, _ in row["groups"] if g]
        roles = [f"{g}: {ROLE_SHORT.get(r, '')}" if g else "" for g, r, _ in row["groups"]]
        notes = sorted({n for _, _, ns in row["groups"] for n in ns})
        date = t.primary_completion or "-"
        if t.primary_completion and t.primary_completion_type == "ESTIMATED":
            date += " (est.)"
        # The columns a reader scans first sit next to the trial ID; long text goes last.
        data.append({
            "Trial": t.url, "Drug": "; ".join(drugs) or "any", "Role of drug": "; ".join(r for r in roles if r) or "-",
            "Phase": fmt_phase(t.phases), "Status": fmt_status(t.status), "Primary completion": date,
            "Lead sponsor": t.sponsor or "-", "Title": t.title, "Notes": "; ".join(notes),
        })
    st.dataframe(
        pd.DataFrame(data), hide_index=True, width="stretch",
        column_config={
            "Trial": st.column_config.LinkColumn("Trial", display_text=r"https://clinicaltrials\.gov/study/(NCT\d+)",
                                                 help="Opens the trial's registry page"),
            "Title": st.column_config.TextColumn(width="medium"),
        },
    )


def render_work(a: Answer) -> None:
    with st.expander("Show your work"):
        p, v = a.plan, a.validation
        if p and p.interpretation:
            st.markdown(f"**Interpretation** ({PLANNER_MODEL}): {md(p.interpretation)}")
        if p and p.warnings:
            st.markdown("**Python corrections to the model's plan**")
            st.markdown("\n".join(f"- {md(w)}" for w in p.warnings))

        if v:
            st.markdown("**Terms checked against the live registry** (existence verified by registry, "
                        "never by the model's confidence)")
            claims = {c.term: c for c in p.candidates} if p else {}
            st.dataframe(pd.DataFrame([{
                "Term": c.term, "Kind": c.kind + (f" of {c.parent}" if c.parent else ""),
                "Registry trials": c.hits if c.hits is not None else "error",
                "Result": CHECK_LABEL.get(c.status, c.status),
                "Model's claim (not verified)": (f"{claims[c.term].confidence}: {claims[c.term].rationale}"
                                                 if c.term in claims else ""),
            } for c in v.checks]), hide_index=True, width="stretch")
            st.caption("The registry check proves a term exists. It does not prove the model's claims about a "
                       "drug's class or mechanism.")

        if a.facts:
            f = a.facts
            st.markdown("**Counts**")
            st.markdown(
                f"- Matching the whole search in the registry: **{f.total_matching if f.total_matching is not None else 'unknown'}**\n"
                f"- Retrieved (at most {PER_GROUP_CAP} per drug, ranked): **{f.retrieved}**\n"
                f"- Of those, testing or listing a searched drug: **{f.core_unique}**\n"
                f"- Sent to the writer (at most {SUMMARY_TRIAL_CAP}): **{len(f.summary_trials)}**")
            st.dataframe(pd.DataFrame([{
                "Drug": g.term or "any (condition-only)", "Also searched as": ", ".join(g.aliases),
                "Registry matches": g.registry_total, "Retrieved": g.fetched, "Tests / lists it": g.core,
                "Comparator / background": g.comparator, "Not in drug list": g.off_topic,
                "Removed (competitor rule)": g.excluded, "Active": g.active, "Highest phase": g.top_phase,
                "Search error": g.error or "",
            } for g in f.groups]), hide_index=True, width="stretch")

        if a.groups:
            st.markdown("**Exact registry queries** (ClinicalTrials.gov API v2, `/studies`)")
            queries = {f"{g.term or 'condition-only'}": {k: v for k, v in g.query.items() if v} for g in a.groups}
            if a.total_query:
                queries["total (M)"] = {k: v for k, v in a.total_query.items() if v}
            st.json(queries, expanded=False)
            excluded = {nct: why for g in a.groups for nct, why in g.excluded.items()}
            if excluded:
                st.markdown("**Removed by the competitor rule** (same condition, different drug)")
                st.markdown("\n".join(f"- [{n}](https://clinicaltrials.gov/study/{n}): {md(w)}" for n, w in excluded.items()))

        if a.tokens:
            st.markdown("**Model calls**")
            st.dataframe(pd.DataFrame([{
                "Call": name, "Input tokens": u.get("input_tokens", 0), "Output tokens": u.get("output_tokens", 0),
                "Cache read": u.get("cache_read_input_tokens", 0), "Cache write": u.get("cache_creation_input_tokens", 0),
            } for name, u in a.tokens.items() if u]), hide_index=True, width="stretch")
            st.caption("Seconds: " + ", ".join(f"{k} {v}" for k, v in a.seconds.items()))

        if a.facts_text and st.toggle("Show exactly what the writer was given"):
            st.code(a.facts_text, language=None)


def render(a: Answer) -> None:
    if a.status == FAILED:
        st.error(md(a.message) or "Something went wrong.")
        return
    p = a.plan
    if a.status == DECLINED:
        st.warning(md(a.message) or "This question can't be answered from the registry.")
        if p and p.example_rephrasings:
            st.markdown("Questions the registry can answer:")
            for i, r in enumerate(p.example_rephrasings):
                st.button(md(r), key=f"rephrase{i}", on_click=_ask, args=(r,))
        return

    if p and p.interpretation:
        st.markdown(f"**I read this as:** {md(p.interpretation)}")
    if a.message:
        (st.info if a.status == ANSWERED else st.warning)(md(a.message))

    if a.status == ANSWERED:
        st.markdown(f"{a.lead} {md(a.summary.text) if a.summary else ''}")
        st.caption(f"Written by {WRITER_MODEL} from the numbers below; Python did all counting. "
                   "The summary is not yet automatically checked: the table is the check.")
        render_table(a)
    render_work(a)


if st.session_state.answer is not None:
    try:
        render(st.session_state.answer)
    except Exception:  # never show a stack trace on the page
        traceback.print_exc()
        st.error("Something went wrong while showing this answer. The details are in the server log.")

st.divider()
st.caption("Data: ClinicalTrials.gov API v2, queried live for every question; nothing is stored. "
           f"Planner: {PLANNER_MODEL}. Writer: {WRITER_MODEL}.")
