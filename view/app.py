"""
TrialLens v2 dashboard.

Run:  streamlit run view/app.py

No API key required. The structured half of the query comes from the
dropdowns; the semantic half comes from the text box. That split is
deliberate -- it makes the hybrid visible. Change only the dropdowns and
you see keyword matching. Change only the text and you see the embedding.
The `found_by` column then shows which half surfaced each trial.

The LLM layers (query decomposition, executive summary, claim validation)
are not wired in yet. Everything on screen here is deterministic.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analyze.landscape import PHASE_RANK, LandscapeAnalyzer  # noqa: E402
from common.paths import DB_PATH  # noqa: E402
from retrieve.encoder import get_encoder  # noqa: E402
from retrieve.rank import Retriever  # noqa: E402

PHASE_ORDER = [p for p, _ in sorted(PHASE_RANK.items(), key=lambda kv: kv[1])]

st.set_page_config(page_title="TrialLens v2", page_icon="🔬", layout="wide")


# ---------------------------------------------------------------------------
# data access
# ---------------------------------------------------------------------------

@st.cache_resource
def connect():
    return sqlite3.connect(str(DB_PATH), check_same_thread=False)


@st.cache_resource
def load_encoder(name: str):
    """
    Falls back to the hashing encoder if fastembed is absent, so the app
    always starts. Which encoder is running is shown in the sidebar --
    silently degrading to a weaker one would make the semantic column
    mysterious rather than absent.
    """
    try:
        return get_encoder(name), None
    except RuntimeError as exc:
        return get_encoder("hashing"), str(exc)


@st.cache_data
def corpus_stats() -> dict:
    conn = connect()
    one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    return {
        "trials": one("SELECT COUNT(*) FROM trials"),
        "programs": one("SELECT COUNT(*) FROM programs"),
        "industry_programs": one(
            "SELECT COUNT(*) FROM programs WHERE sponsor_class='INDUSTRY'"),
        "assets": one("SELECT COUNT(DISTINCT asset_name) FROM programs"),
        "models": [r[0] for r in conn.execute(
            "SELECT DISTINCT model_id FROM trial_embeddings")],
        "last_ingest": one("SELECT MAX(last_changed_at) FROM trials"),
    }


@st.cache_data
def vocab_options() -> dict:
    conn = connect()
    return {
        "indications": [r[0] for r in conn.execute(
            """SELECT canonical FROM trial_indications
                WHERE kind='indication' AND specificity='specific'
                GROUP BY canonical ORDER BY COUNT(*) DESC""")],
        "classes": [r[0] for r in conn.execute(
            """SELECT drug_class FROM programs WHERE drug_class IS NOT NULL
                GROUP BY drug_class ORDER BY COUNT(*) DESC""")],
        "assets": [r[0] for r in conn.execute(
            "SELECT asset_name FROM programs GROUP BY asset_name ORDER BY asset_name")],
        "statuses": [r[0] for r in conn.execute(
            """SELECT overall_status FROM trials WHERE overall_status IS NOT NULL
                GROUP BY overall_status ORDER BY COUNT(*) DESC""")],
    }


@st.cache_data
def trial_details(nct_ids: tuple[str, ...]) -> pd.DataFrame:
    if not nct_ids:
        return pd.DataFrame()
    conn = connect()
    marks = ",".join("?" * len(nct_ids))
    rows = conn.execute(
        f"""SELECT nct_id, title, phase, overall_status, sponsor, enrollment,
                   why_stopped
              FROM trials WHERE nct_id IN ({marks})""", nct_ids).fetchall()
    return pd.DataFrame(rows, columns=[
        "nct_id", "title", "phase", "status", "sponsor", "enrollment", "why_stopped"])


# ---------------------------------------------------------------------------
# sidebar -- the query
# ---------------------------------------------------------------------------

stats = corpus_stats()
options = vocab_options()

st.sidebar.title("🔬 TrialLens v2")
st.sidebar.caption(
    f"{stats['trials']:,} antifungal trials · {stats['industry_programs']} "
    f"industry programs · {stats['assets']} assets")

encoder_choice = st.sidebar.selectbox(
    "Embedding model",
    ["onnx", "hashing"],
    format_func=lambda n: {"onnx": "BAAI/bge-small-en-v1.5 (real)",
                           "hashing": "hashing bag-of-words (baseline)"}[n],
    help="The baseline has no semantics at all. Switching between them is "
         "the ablation: see how much the trained model changes the ranking.",
)
encoder, encoder_error = load_encoder(encoder_choice)
if encoder_error:
    st.sidebar.warning("fastembed not installed — using the hashing baseline.")

st.sidebar.divider()
st.sidebar.subheader("Semantic half")
question = st.sidebar.text_area(
    "Question",
    "glucan synthase inhibitors for invasive candidiasis",
    help="Embedded and compared against every trial. This is what finds "
         "trials whose wording does not match your filters.",
    height=80,
)

st.sidebar.subheader("Structured half")
sel_indications = st.sidebar.multiselect("Indication", options["indications"])
sel_classes = st.sidebar.multiselect("Mechanism class", options["classes"])
sel_assets = st.sidebar.multiselect("Asset", options["assets"])
sel_phase = st.sidebar.multiselect("Phase", PHASE_ORDER)
sel_status = st.sidebar.multiselect("Status", options["statuses"],
                                    default=["RECRUITING", "ACTIVE_NOT_RECRUITING"])
top_k = st.sidebar.slider("Trials to retrieve", 10, 200, 60, step=10)

criteria = {
    "indications": [i.lower() for i in sel_indications],
    "drug_classes": [c.lower() for c in sel_classes],
    "interventions": [a.lower() for a in sel_assets],
    "phase": sel_phase,
    "status": sel_status,
}
criteria = {k: v for k, v in criteria.items() if v}

if not stats["models"]:
    st.error("No cached embeddings. Run `python retrieve/embed.py --encoder onnx`.")
    st.stop()


# ---------------------------------------------------------------------------
# retrieve + analyse
# ---------------------------------------------------------------------------

conn = connect()
with st.spinner("Retrieving…"):
    result = Retriever(conn, encoder).retrieve(question, criteria, top_k)
    nct_ids = [r["nct_id"] for r in result["results"]]
    landscape = LandscapeAnalyzer(conn).analyze(nct_ids)

counts = landscape["counts"]
cards = landscape["cards"]

tab_landscape, tab_trials, tab_retrieval, tab_quality = st.tabs(
    ["Landscape", "Trials", "How retrieval worked", "Data quality"])


# ---------------------------------------------------------------------------
# Landscape
# ---------------------------------------------------------------------------

with tab_landscape:
    st.subheader("Landscape overview")
    cols = st.columns(5)
    for col, key, label in [
        (cols[0], "trials", "Trials"),
        (cols[1], "programs_industry", "Industry programs"),
        (cols[2], "companies", "Companies"),
        (cols[3], "indications", "Indications"),
        (cols[4], "phase3_plus_programs", "Phase 3 programs"),
    ]:
        col.metric(label, counts[key]["value"], help=counts[key]["formula"])

    st.caption(
        f"Also {counts['programs_investigator_initiated']['value']} "
        f"investigator-initiated programs — academic or government sponsors "
        f"running trials of an asset they do not own. Counted separately, "
        f"from the registry's own sponsor class.")

    st.divider()

    c1, c2, c3 = st.columns(3)
    adv = cards.get("most_advanced_in_development")
    if adv:
        with c1:
            st.markdown("**Most advanced in development**")
            st.markdown(f"### {adv['value']['program']}")
            st.write(f"{adv['value']['sponsor']} · {adv['value']['phase']}")
            st.caption("Phase 4 excluded — that means already marketed.")
    crowd = landscape["crowding"].get("most_crowded_indication")
    if crowd:
        with c2:
            v = crowd["value"]
            st.markdown("**Most crowded indication**")
            st.markdown(f"### {v['indication']}")
            st.write(f"{v['programs']} programs · {v['distinct_mechanisms']} mechanisms")
            st.caption(f"{v['dominant_mechanism_programs']} of them are "
                       f"{v['dominant_mechanism']}")
    big = cards.get("largest_active_trial")
    if big:
        with c3:
            v = big["value"]
            st.markdown("**Largest active trial**")
            st.markdown(f"### n={v['enrollment']:,}")
            st.write(v["sponsor"])
            st.caption(v["title"][:90])

    marketed = cards.get("already_marketed")
    if marketed:
        st.info(f"**Already marketed** (set the standard of care rather than "
                f"competing within it): {', '.join(marketed['value'])}")

    st.divider()
    st.subheader("Competitive landscape")

    programs = landscape["programs"]
    if programs:
        bubble = pd.DataFrame([{
            "Phase": p["development_phase"] or p["phase"] or "not reported",
            "Indication": p["indications"][0] if p["indications"] else "not reported",
            "Enrollment": p["max_enrollment"] or 0,
            "Sponsor": (p["sponsor"] or "")[:34],
            "Program": p["asset_name"],
            "Mechanism": p["drug_class"] or "unknown",
            "Trials": p["trial_count"],
            "Marketed": "yes" if p["is_marketed"] else "no",
        } for p in programs])

        chart = (
            alt.Chart(bubble)
            .mark_circle(opacity=0.75, stroke="white", strokeWidth=1)
            .encode(
                x=alt.X("Phase:N", sort=[p for p in PHASE_ORDER] + ["not reported"],
                        title="Development phase"),
                y=alt.Y("Indication:N", title=None,
                        sort=alt.EncodingSortField("Trials", op="sum", order="descending")),
                size=alt.Size("Enrollment:Q", scale=alt.Scale(range=[60, 1200]),
                              legend=alt.Legend(title="Enrollment")),
                color=alt.Color("Sponsor:N", legend=alt.Legend(title="Sponsor", columns=1)),
                tooltip=["Program", "Sponsor", "Mechanism", "Phase", "Indication",
                         "Enrollment", "Trials", "Marketed"],
            )
            .properties(height=460)
        )
        st.altair_chart(chart, use_container_width=True)
        st.caption("One bubble per program. Size is the largest enrollment among "
                   "its retrieved trials; a program appears at its most advanced "
                   "phase. Programs with no reported phase or indication are "
                   "shown as 'not reported' rather than dropped.")

        table = pd.DataFrame([{
            "Program": p["asset_name"],
            "Company": p["sponsor"],
            "Type": "industry" if p["sponsor_class"] == "INDUSTRY" else "investigator",
            "Mechanism": p["drug_class"] or "—",
            "Indication": ", ".join(p["indications"]) or "—",
            "Phase": p["development_phase"] or p["phase"] or "—",
            "Marketed": "✓" if p["is_marketed"] else "",
            "Trials": p["trial_count"],
            "Max enrollment": p["max_enrollment"] or 0,
        } for p in programs])
        st.dataframe(table, use_container_width=True, hide_index=True)

    crowding = landscape["crowding"]["by_indication"]
    if crowding:
        st.divider()
        st.subheader("Crowding")
        left, right = st.columns([1, 1])
        with left:
            df = pd.DataFrame(crowding).rename(
                columns={"indication": "Indication", "programs": "Programs"})
            st.altair_chart(
                alt.Chart(df).mark_bar().encode(
                    x=alt.X("Programs:Q"),
                    y=alt.Y("Indication:N", sort="-x", title=None),
                    tooltip=["Indication", "Programs"],
                ).properties(height=min(30 * len(df) + 40, 400)),
                use_container_width=True)
            st.caption("Distinct industry programs, not trials.")
        with right:
            matrix = pd.DataFrame(landscape["crowding"]["matrix"])
            if not matrix.empty:
                st.altair_chart(
                    alt.Chart(matrix).mark_rect().encode(
                        x=alt.X("mechanism_class:N", title=None,
                                axis=alt.Axis(labelAngle=-40)),
                        y=alt.Y("indication:N", title=None, sort="-color"),
                        color=alt.Color("programs:Q", title="Programs",
                                        scale=alt.Scale(scheme="blues")),
                        tooltip=["indication", "mechanism_class", "programs"],
                    ).properties(height=min(30 * matrix["indication"].nunique() + 60, 400)),
                    use_container_width=True)
                st.caption("Indication × mechanism. A crowded row where one "
                           "column dominates is a me-too space.")

    attrition = landscape["attrition"]
    if attrition["detail"]:
        st.divider()
        st.subheader("Attrition")
        st.caption(
            f"{attrition['stopped_trials']['value']} retrieved trials stopped; "
            f"{attrition['with_stated_reason']['value']} gave a reason. The "
            f"registry only fills in `whyStopped` when something went wrong.")
        st.dataframe(
            pd.DataFrame(attrition["detail"])[
                ["program", "sponsor", "nct_id", "phase", "status", "why_stopped"]
            ].rename(columns={"program": "Program", "sponsor": "Company",
                              "nct_id": "Trial", "phase": "Phase",
                              "status": "Status", "why_stopped": "Reason given"}),
            use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Trials
# ---------------------------------------------------------------------------

with tab_trials:
    st.subheader(f"{len(result['results'])} ranked trials")
    details = trial_details(tuple(nct_ids)).set_index("nct_id")

    ranked = pd.DataFrame([{
        "Rank": i,
        "Trial": r["nct_id"],
        "Title": (details.loc[r["nct_id"], "title"] or "")[:80]
                 if r["nct_id"] in details.index else "",
        "Phase": details.loc[r["nct_id"], "phase"] if r["nct_id"] in details.index else "",
        "Status": details.loc[r["nct_id"], "status"] if r["nct_id"] in details.index else "",
        "Score": r["final_score"],
        "Keyword": r["structured_score"],
        "Semantic": r["semantic_normalized"],
        "Found by": "+".join(r["found_by"]),
    } for i, r in enumerate(result["results"], 1)])
    st.dataframe(ranked, use_container_width=True, hide_index=True,
                 column_config={
                     "Score": st.column_config.ProgressColumn(
                         "Score", min_value=0, max_value=1, format="%.3f"),
                 })

    st.divider()
    st.subheader("Why did a trial rank there?")
    pick = st.selectbox("Trial", nct_ids, format_func=lambda n: (
        f"{n} — {(details.loc[n, 'title'] or '')[:70]}" if n in details.index else n))
    chosen = next(r for r in result["results"] if r["nct_id"] == pick)

    a, b = st.columns([1, 1])
    with a:
        st.metric("Final score", f"{chosen['final_score']:.4f}")
        st.write(f"**Keyword (structured):** {chosen['structured_score']:.3f}")
        st.write(f"**Semantic, raw cosine:** {chosen['semantic_cosine_raw']:.3f}")
        st.write(f"**Semantic, pool-normalised:** {chosen['semantic_normalized']:.3f}")
        st.write(f"**Surfaced by:** {', '.join(chosen['found_by'])}")
        st.link_button("Open on ClinicalTrials.gov",
                       f"https://clinicaltrials.gov/study/{pick}")
    with b:
        if chosen["per_criterion"]:
            st.write("**Per-criterion breakdown**")
            st.dataframe(pd.DataFrame([
                {"Criterion": k, "Score": v["score"], "Matched in": v["tier"],
                 "Term": v["matched_term"], "Weight": v["weight"]}
                for k, v in chosen["per_criterion"].items()
            ]), use_container_width=True, hide_index=True)
        else:
            st.info("No structured criteria given — this trial was ranked "
                    "entirely by semantic similarity.")


# ---------------------------------------------------------------------------
# How retrieval worked
# ---------------------------------------------------------------------------

with tab_retrieval:
    st.subheader("The two halves of the search")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Corpus", f"{result['corpus_size']:,}")
    c2.metric("Candidate pool", result["pool_size"])
    c3.metric("Keyword only", result["pool_structured_only"])
    c4.metric("Semantic only", result["pool_semantic_only"],
              help="Trials no keyword filter would have found.")

    st.markdown(f"""
The pool is the **union** of both halves, not a keyword filter that semantic
then re-sorts. If semantic only re-ranked, it could never add a trial —
and **{result['pool_semantic_only']} trials here were found by semantic
similarity alone**.

Final score = `{result['fusion']['structured_weight']} × keyword +
{result['fusion']['semantic_weight']} × semantic`, with the semantic score
min-max normalised inside the pool first. That normalisation matters:
`{encoder.model_id}` puts almost every trial between 0.72 and 0.77 raw
cosine, so without rescaling the semantic half would move the final score by
about 0.02 and effectively do nothing.
""")

    pool_df = pd.DataFrame([
        {"Arm": "keyword only", "Trials": result["pool_structured_only"]},
        {"Arm": "both", "Trials": result["pool_both"]},
        {"Arm": "semantic only", "Trials": result["pool_semantic_only"]},
    ])
    st.altair_chart(
        alt.Chart(pool_df).mark_bar().encode(
            x=alt.X("Trials:Q"), y=alt.Y("Arm:N", sort="-x", title=None),
            color=alt.Color("Arm:N", legend=None), tooltip=["Arm", "Trials"],
        ).properties(height=150), use_container_width=True)

    st.caption(f"Encoder: `{encoder.model_id}` · fusion: "
               f"`{result['fusion']['mode']}` · normalised: "
               f"`{result['fusion']['normalize_semantic']}`")


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------

with tab_quality:
    st.subheader("What this result set does not know")
    st.caption("Reported so the charts above are read with their gaps visible.")
    coverage = landscape["coverage"]
    cols = st.columns(3)
    for i, (key, label) in enumerate([
        ("trials_without_phase", "No phase reported"),
        ("trials_without_enrollment", "No enrollment"),
        ("trials_status_unknown", "Status UNKNOWN"),
        ("trials_without_program", "No resolved asset"),
        ("programs_without_phase", "Programs off the phase axis"),
        ("phase_coverage_pct", "Phase coverage %"),
    ]):
        cols[i % 3].metric(label, coverage[key]["value"],
                           help=coverage[key]["formula"])

    st.divider()
    st.subheader("Corpus")
    c1, c2, c3 = st.columns(3)
    c1.metric("Trials ingested", f"{stats['trials']:,}")
    c2.metric("Programs resolved", f"{stats['programs']:,}",
              help="(sponsor, asset) pairs; most are investigator-initiated")
    c3.metric("Distinct assets", stats["assets"])
    st.caption(f"Cached embedding models: {', '.join(stats['models'])}. "
               f"Latest trial record change: {stats['last_ingest']}.")

    st.divider()
    st.markdown("""
**Pipeline** — every stage below is deterministic. No model writes a number
anywhere in this dashboard.

1. **Ingest** — six query dimensions against ClinicalTrials.gov (condition,
   intervention, sponsor, mechanism, related terms, host population), unioned.
   Wide on purpose; precision happens at step 3.
2. **Normalize** — parse to SQLite, resolve drug names to assets via a curated
   dictionary, collapse condition strings onto a controlled vocabulary.
3. **Retrieve** — keyword scoring with field-authority tiers, plus embedding
   similarity. Pool is the union.
4. **Analyze** — counts, crowding, superlatives. Each carries its formula,
   which is what the tooltips on the metrics above are showing.

Still to build: LLM query decomposition, executive summary, and claim
validation against these numbers.
""")
