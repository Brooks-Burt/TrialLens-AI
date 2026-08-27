"""
view/app.py

Streamlit dashboard for TrialLens.

STRUCTURE: "Ask" is the landing page, not a tab. That's deliberate -- the
query planner is the interesting part of this project (Claude proposes
search terms, the live registry validates every one before it can reach a
result set), so it's the first thing a visitor sees. Search, Detail, and
Change feed are supporting views reachable from the sidebar.

READ-ONLY, WITH ONE EXCEPTION: every page except Ask touches nothing but
data/trials.db. Ask calls out to Claude and the live CT.gov API, and it
does so by importing cli/ask.py's `answer_question()` directly rather than
re-implementing any of it. The planning, validation, ranking, and landscape
logic lives in exactly one place; this file is a rendering layer over it.

Run with:
    streamlit run view/app.py

If you get "No module named 'anthropic'" on the Ask page, streamlit is
being launched by a different Python interpreter than the one that has the
project's dependencies installed. Launch it as `python -m streamlit run
view/app.py` from inside the virtualenv instead -- see docs/SETUP.md.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import streamlit as st

# view/ is a sibling of cli/, enrich/, normalize/, common/ -- add the repo
# root so `from cli.ask import ...` resolves regardless of the working
# directory streamlit was launched from. Python 3.3+ namespace packages
# make this work without __init__.py files, which the repo doesn't have.
HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DB_PATH = REPO_ROOT / "data" / "trials.db"

PHASES = ["PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]
STATUSES = ["RECRUITING", "ACTIVE_NOT_RECRUITING", "COMPLETED", "TERMINATED", "WITHDRAWN"]

PAGES = ["Ask", "Browse trials", "Trial detail", "Change feed"]

EXAMPLE_QUESTIONS = [
    "What's competing with rezafungin in refractory candidemia?",
    "Which antifungals are in phase 3 for invasive aspergillosis?",
    "What echinocandin trials have been terminated?",
]

# Status -> pill color. Kept as a plain dict rather than computed so the
# mapping is greppable and obvious; these are the only five statuses the
# schema's queries filter on.
STATUS_TONE = {
    "RECRUITING": "ok",
    "ACTIVE_NOT_RECRUITING": "info",
    "COMPLETED": "neutral",
    "TERMINATED": "bad",
    "WITHDRAWN": "bad",
}


# ---------------------------------------------------------------------
# Styling
#
# All colors are expressed against Streamlit's own theme CSS variables
# (--background-color, --text-color, --primary-color) plus color-mix(),
# rather than hardcoded hex values. That means the cards below follow
# whatever theme the user is running -- light, dark, or a custom
# config.toml -- instead of looking correct in one and unreadable in the
# other. color-mix() is supported in every browser Streamlit targets.
# ---------------------------------------------------------------------

CSS = """
<style>
:root {
  --tl-border: color-mix(in srgb, var(--text-color) 14%, transparent);
  --tl-surface: color-mix(in srgb, var(--text-color) 4%, var(--background-color));
  --tl-muted: color-mix(in srgb, var(--text-color) 60%, transparent);
  --tl-ok: #16a34a;
  --tl-bad: #dc2626;
  --tl-warn: #d97706;
  --tl-info: #2563eb;
}

/* Tighten Streamlit's default top padding so the hero sits higher. */
.block-container { padding-top: 2.2rem; max-width: 1100px; }

/* ---- Card ---- */
.tl-card {
  background: var(--tl-surface);
  border: 1px solid var(--tl-border);
  border-radius: 12px;
  padding: 1rem 1.15rem;
  margin-bottom: 0.85rem;
}
.tl-card-accent { border-left: 3px solid var(--primary-color); }
.tl-card-warn   { border-left: 3px solid var(--tl-warn); }
.tl-card-bad    { border-left: 3px solid var(--tl-bad); }

.tl-card h4 {
  margin: 0 0 0.4rem 0;
  font-size: 0.98rem;
  font-weight: 600;
  line-height: 1.35;
}
.tl-card p { margin: 0 0 0.5rem 0; font-size: 0.9rem; line-height: 1.55; }
.tl-card p:last-child { margin-bottom: 0; }

/* ---- Label above a card's content ---- */
.tl-label {
  font-size: 0.7rem;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: var(--tl-muted);
  margin-bottom: 0.3rem;
}

/* ---- Metric tile ---- */
.tl-metric {
  background: var(--tl-surface);
  border: 1px solid var(--tl-border);
  border-radius: 12px;
  padding: 0.8rem 0.95rem;
  height: 100%;
}
.tl-metric .tl-metric-value {
  font-size: 1.6rem;
  font-weight: 700;
  line-height: 1.1;
}
.tl-metric .tl-metric-label {
  font-size: 0.72rem;
  color: var(--tl-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-top: 0.2rem;
}

/* ---- Pills ---- */
.tl-pill {
  display: inline-block;
  font-size: 0.72rem;
  font-weight: 600;
  padding: 0.12rem 0.5rem;
  border-radius: 999px;
  margin-right: 0.3rem;
  border: 1px solid var(--tl-border);
  color: var(--tl-muted);
  white-space: nowrap;
}
.tl-pill-ok      { color: var(--tl-ok);   border-color: color-mix(in srgb, var(--tl-ok) 45%, transparent); }
.tl-pill-bad     { color: var(--tl-bad);  border-color: color-mix(in srgb, var(--tl-bad) 45%, transparent); }
.tl-pill-warn    { color: var(--tl-warn); border-color: color-mix(in srgb, var(--tl-warn) 45%, transparent); }
.tl-pill-info    { color: var(--tl-info); border-color: color-mix(in srgb, var(--tl-info) 45%, transparent); }
.tl-pill-neutral { color: var(--tl-muted); }

.tl-mono { font-family: var(--font-monospace, monospace); font-size: 0.8rem; }
.tl-muted { color: var(--tl-muted); font-size: 0.85rem; }

/* Make the hero question input feel like the primary control. */
div[data-testid="stTextInput"] input { font-size: 1.02rem; padding: 0.6rem 0.75rem; }
</style>
"""


def _esc(text) -> str:
    """
    Minimal HTML escape. Every card below is rendered with
    unsafe_allow_html=True, which means any registry text interpolated into
    it -- trial titles, sponsor names, why_stopped free text -- must be
    escaped first. CT.gov data is not adversarial, but titles do legitimately
    contain '<' and '&' (dose ranges, "A&B" combination arms), and those
    would silently mangle the layout if passed through raw.
    """
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def pill(text: str, tone: str = "neutral") -> str:
    return f'<span class="tl-pill tl-pill-{tone}">{_esc(text)}</span>'


def metric_tile(label: str, value) -> str:
    return (
        f'<div class="tl-metric"><div class="tl-metric-value">{_esc(value)}</div>'
        f'<div class="tl-metric-label">{_esc(label)}</div></div>'
    )


def card(body_html: str, variant: str = "") -> str:
    cls = f"tl-card {variant}".strip()
    return f'<div class="{cls}">{body_html}</div>'


def status_pill(status: str | None) -> str:
    if not status:
        return pill("status unknown")
    return pill(status.replace("_", " ").lower(), STATUS_TONE.get(status, "neutral"))


# ---------------------------------------------------------------------
# Data access
#
# A new connection per call, not a cached shared one. sqlite3 connections
# are cheap to open, every query here is read-only, and Streamlit runs each
# session in its own thread -- a single process-wide connection shared
# across threads with check_same_thread=False can interleave cursors and
# raise (or worse, return another thread's rows). The @st.cache_data layer
# above it means we aren't actually opening a connection per rerun anyway.
# ---------------------------------------------------------------------

@st.cache_data(ttl=30)
def load_trials() -> list[dict]:
    """
    Everything the local pages need, in one query. A few hundred trials is
    small enough to hold in memory and filter in Python rather than pushing
    every filter permutation into SQL -- consistent with the scope doc's
    "SQL LIKE beats anything fancier at this scale" position.
    """
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
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
    finally:
        conn.close()


def _json_list(raw: str | None) -> list[str]:
    """
    conditions/interventions/arms/primary_outcome_measures are stored as
    JSON strings (SQLite has no array type). build_db.py flattens each to a
    list of plain strings before dumping, so this returns strings -- but it
    falls back to str() per element rather than assuming, so a future
    build_db change that stores objects degrades to ugly instead of crashing.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [str(raw)]
    if isinstance(parsed, list):
        return [x if isinstance(x, str) else json.dumps(x) for x in parsed]
    return [str(parsed)]


def _text_blob(trial: dict) -> str:
    return " ".join(
        [trial.get("title") or "", trial.get("conditions") or "", trial.get("interventions") or ""]
    ).lower()


def _is_stale(trial: dict) -> bool:
    """A summary exists but was generated against an older materiality hash."""
    return bool(trial.get("scientific_summary")) and trial.get("summary_hash") != trial.get("materiality_hash")


def _fmt(value, fallback: str = "not reported") -> str:
    """
    Explicit None check, not a falsy check. `value or fallback` would
    report a real enrollment of 0 -- a withdrawn trial that enrolled nobody,
    which is exactly the kind of trial worth noticing -- as "not reported".
    """
    if value is None or value == "":
        return fallback
    return str(value)


def goto(page: str, nct_id: str | None = None):
    """
    Programmatic navigation, via a staging key rather than by writing the
    widget's key directly.

    Two Streamlit rules collide here and the staging key is what resolves
    them:

      1. A widget honors its `index=`/`default=` argument only on the run
         that first creates it. On every later rerun, whatever is stored
         under the widget's key wins. So you cannot navigate by recomputing
         an index -- you have to write the key.

      2. But you cannot write a widget's key *after* that widget has been
         instantiated in the current run: Streamlit raises
         StreamlitAPIException. The sidebar radio (key="nav") is created
         before any page body renders, so an "Open detail" button inside a
         page body is always in that forbidden window.

    So: stash the intent under a non-widget key, rerun, and let main() move
    it onto the widget key at the top of the next run -- before the radio
    exists. Same pattern for the example-question buttons on the Ask page.
    """
    if nct_id is not None:
        st.session_state["_pending_nct"] = nct_id
    st.session_state["_pending_nav"] = page
    st.rerun()


def apply_pending_state():
    """
    Called once at the top of main(), before any widget is created. Moves
    staged navigation intent onto the real widget keys. See goto().
    """
    if "_pending_nav" in st.session_state:
        st.session_state["nav"] = st.session_state.pop("_pending_nav")
    if "_pending_nct" in st.session_state:
        st.session_state["selected_nct"] = st.session_state.pop("_pending_nct")
    if "_pending_question" in st.session_state:
        st.session_state["ask_question"] = st.session_state.pop("_pending_question")


# ---------------------------------------------------------------------
# Ask -- the landing page
# ---------------------------------------------------------------------

def render_ask(trials: list[dict]):
    n_summarized = sum(1 for t in trials if t.get("scientific_summary"))

    st.markdown("## Ask the trial landscape")
    st.markdown(
        '<p class="tl-muted">Claude proposes candidate search terms. Every one is checked '
        "against the live ClinicalTrials.gov registry before it is allowed to reach a query, "
        "and any term that returns zero hits is dropped and shown to you below. "
        "Matching is then done locally and one competitive-landscape paragraph is written "
        "across the results.</p>",
        unsafe_allow_html=True,
    )

    question = st.text_input(
        "Your question",
        key="ask_question",
        placeholder="What's competing with rezafungin in refractory candidemia?",
        label_visibility="collapsed",
    )

    left, right = st.columns([1, 2])
    with left:
        run = st.button("Ask", type="primary", use_container_width=True, disabled=not question)
    with right:
        top_n = st.slider("Local matches to consider", 5, 30, 15, key="ask_top_n",
                          label_visibility="collapsed")

    st.markdown('<div class="tl-label">Try one of these</div>', unsafe_allow_html=True)
    ex_cols = st.columns(len(EXAMPLE_QUESTIONS))
    for col, example in zip(ex_cols, EXAMPLE_QUESTIONS):
        with col:
            # Stage, don't assign: the ask_question text_input already exists
            # by this point in the run, so writing its key directly would
            # raise StreamlitAPIException. See goto()/apply_pending_state().
            if st.button(example, key=f"ex_{example[:20]}", use_container_width=True):
                st.session_state["_pending_question"] = example
                st.rerun()

    st.markdown(
        f'<p class="tl-muted">Corpus: <b>{len(trials)}</b> trials pulled, '
        f"<b>{n_summarized}</b> with cached Claude summaries. Only summarized trials "
        "can enter a landscape synthesis.</p>",
        unsafe_allow_html=True,
    )

    if not run:
        return

    try:
        from cli.ask import answer_question  # lazy: touches Anthropic + network
    except ImportError as e:
        st.error(
            f"**Could not import `cli/ask.py`:** {e}\n\n"
            "This almost always means Streamlit is running under a different Python "
            "interpreter than the one holding the project's dependencies. From the repo "
            "root, with your virtualenv active, run:\n\n"
            "```\npip install -r requirements.txt\npython -m streamlit run view/app.py\n```\n\n"
            "`python -m streamlit` guarantees the same interpreter that has `anthropic` "
            "installed is the one running the app. See `docs/SETUP.md`."
        )
        return

    with st.spinner("Planning query, validating terms against the live registry, synthesizing..."):
        try:
            result = answer_question(question, top_n=top_n)
        except Exception as e:
            st.error(
                f"**The Ask pipeline raised an error:** `{e}`\n\n"
                "Most common cause: `ANTHROPIC_API_KEY` is missing from the `.env` file "
                "in the repo root."
            )
            return

    st.divider()
    render_ask_result(result)


def render_ask_result(result: dict):
    st.markdown(
        card(
            '<div class="tl-label">Interpretation</div>'
            f'<p>{_esc(result.get("interpretation")) or "—"}</p>',
            "tl-card-accent",
        ),
        unsafe_allow_html=True,
    )

    if result.get("out_of_scope"):
        st.markdown(
            card(
                '<div class="tl-label">Out of scope</div>'
                f'<p>{_esc(result["out_of_scope"])}</p>'
                '<p class="tl-muted">The planner declined rather than guessing. This dataset '
                "covers antifungal / anti-infective and oncology trials only.</p>",
                "tl-card-warn",
            ),
            unsafe_allow_html=True,
        )
        return

    dropped = result.get("dropped_terms") or []
    with_summary = result.get("with_summary") or []
    without_summary = result.get("without_summary") or []

    m1, m2, m3 = st.columns(3)
    m1.markdown(metric_tile("Local matches", result.get("total_matches", 0)), unsafe_allow_html=True)
    m2.markdown(metric_tile("In synthesis", len(with_summary)), unsafe_allow_html=True)
    m3.markdown(metric_tile("Terms dropped", len(dropped)), unsafe_allow_html=True)

    st.write("")

    if dropped:
        st.markdown(
            card(
                '<div class="tl-label">Dropped terms — the guardrail firing</div>'
                "<p>Claude proposed these; each returned zero hits against the live registry, "
                "so none of them reached a query:</p>"
                + "".join(pill(t, "bad") for t in dropped),
                "tl-card-bad",
            ),
            unsafe_allow_html=True,
        )

    if result.get("landscape"):
        st.markdown(
            card(
                '<div class="tl-label">Competitive landscape</div>'
                f'<p>{_esc(result["landscape"])}</p>'
                f'<p class="tl-muted">Synthesized in one Claude call across '
                f"{len(with_summary)} summarized trials — not one call per trial.</p>",
                "tl-card-accent",
            ),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            card(
                '<div class="tl-label">No synthesis</div>'
                "<p>No matching trial has a cached summary yet, so nothing was synthesized "
                "rather than something being invented. Run "
                "<code>enrich/run_enrichment.py</code>, then ask again.</p>",
                "tl-card-warn",
            ),
            unsafe_allow_html=True,
        )

    if with_summary:
        st.markdown(f"#### Trials in the synthesis ({len(with_summary)})")
        for t in with_summary:
            render_trial_card(t, show_open_button=True)

    if without_summary:
        st.markdown(f"#### Matched but not summarized ({len(without_summary)})")
        st.markdown(
            '<p class="tl-muted">Listed separately rather than quietly folded into the '
            "paragraph above. Run enrichment to include them next time.</p>",
            unsafe_allow_html=True,
        )
        for t in without_summary:
            render_trial_card(t, show_open_button=True, dim=True)


# ---------------------------------------------------------------------
# Shared trial card
# ---------------------------------------------------------------------

def render_trial_card(t: dict, show_open_button: bool = False, dim: bool = False):
    """
    One trial as a bordered card. `t` may come from either load_trials() or
    cli/ask.py's result dicts, which carry a narrower column set -- so every
    field is read with .get() and a fallback.
    """
    pills = "".join([
        pill(t.get("phase") or "phase n/a", "info"),
        status_pill(t.get("overall_status")),
        pill(t.get("sponsor") or "sponsor not reported"),
    ])
    if dim:
        pills += pill("not summarized", "warn")
    if _is_stale(t):
        pills += pill("summary stale", "warn")

    body = (
        f'<div class="tl-mono">{_esc(t.get("nct_id"))}</div>'
        f'<h4>{_esc(t.get("title")) or "Untitled record"}</h4>'
        f"<div>{pills}</div>"
    )
    st.markdown(card(body), unsafe_allow_html=True)

    if show_open_button and t.get("nct_id"):
        if st.button("Open detail", key=f"open_{t['nct_id']}"):
            goto("Trial detail", t["nct_id"])


# ---------------------------------------------------------------------
# Browse trials
# ---------------------------------------------------------------------

def render_browse(trials: list[dict]):
    st.markdown("## Browse trials")
    st.markdown(
        '<p class="tl-muted">Free text plus filters, run entirely against the local '
        "database. No network call and no Claude call happens on this page.</p>",
        unsafe_allow_html=True,
    )

    text = st.text_input(
        "Search title, conditions, or interventions",
        key="browse_text",
        placeholder="rezafungin, candidemia, aspergillosis...",
    )

    f1, f2 = st.columns(2)
    with f1:
        phase_sel = st.multiselect("Phase", PHASES, key="browse_phase")
        areas = sorted({t["therapeutic_area"] for t in trials if t.get("therapeutic_area")})
        area = st.selectbox("Therapeutic area", ["Any"] + areas, key="browse_area")
    with f2:
        status_sel = st.multiselect("Status", STATUSES, key="browse_status")
        sponsors = sorted({t["sponsor"] for t in trials if t.get("sponsor")})
        sponsor_sel = st.multiselect("Sponsor", sponsors, key="browse_sponsor")

    only_summarized = st.checkbox("Only trials with a cached summary", key="browse_summarized")

    results = trials
    if text:
        needle = text.lower()
        results = [t for t in results if needle in _text_blob(t)]
    if area != "Any":
        results = [t for t in results if t.get("therapeutic_area") == area]
    if phase_sel:
        results = [t for t in results if set((t.get("phase") or "").split("/")) & set(phase_sel)]
    if status_sel:
        results = [t for t in results if t.get("overall_status") in status_sel]
    if sponsor_sel:
        results = [t for t in results if t.get("sponsor") in sponsor_sel]
    if only_summarized:
        results = [t for t in results if t.get("scientific_summary")]

    m1, m2, m3 = st.columns(3)
    m1.markdown(metric_tile("Matching", len(results)), unsafe_allow_html=True)
    m2.markdown(metric_tile("In corpus", len(trials)), unsafe_allow_html=True)
    m3.markdown(
        metric_tile("Summarized", sum(1 for t in results if t.get("scientific_summary"))),
        unsafe_allow_html=True,
    )
    st.write("")

    view_mode = st.radio(
        "View", ["Cards", "Table"], horizontal=True, key="browse_view",
        label_visibility="collapsed",
    )

    if not results:
        st.markdown(
            card('<p class="tl-muted">Nothing matches those filters.</p>'),
            unsafe_allow_html=True,
        )
        return

    if view_mode == "Table":
        st.dataframe(
            [
                {
                    "NCT ID": t["nct_id"],
                    "Title": t.get("title") or "",
                    "Phase": t.get("phase") or "",
                    "Status": t.get("overall_status") or "",
                    "Sponsor": t.get("sponsor") or "not reported",
                    "Summarized": "yes" if t.get("scientific_summary") else "no",
                }
                for t in results
            ],
            hide_index=True,
        )
        return

    PAGE_SIZE = 25
    shown = results[:PAGE_SIZE]
    for t in shown:
        render_trial_card(t, show_open_button=True, dim=not t.get("scientific_summary"))
    if len(results) > PAGE_SIZE:
        st.markdown(
            f'<p class="tl-muted">Showing the first {PAGE_SIZE} of {len(results)}. '
            "Narrow the filters, or switch to Table view for the full list.</p>",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------
# Trial detail
# ---------------------------------------------------------------------

def render_detail(trials: list[dict]):
    st.markdown("## Trial detail")

    by_id = {t["nct_id"]: t for t in trials}
    options = sorted(by_id.keys())
    if not options:
        st.markdown(card('<p class="tl-muted">No trials in the database yet.</p>'),
                    unsafe_allow_html=True)
        return

    # Seed the widget's stored value before creating it, so a selection made
    # elsewhere (goto(), or the Ask page's "Open detail") is respected. Passing
    # index= here would be ignored on every rerun after the first.
    target = st.session_state.get("selected_nct")
    if target in by_id and st.session_state.get("detail_select") != target:
        st.session_state["detail_select"] = target

    nct_id = st.selectbox("NCT ID", options, key="detail_select")
    st.session_state["selected_nct"] = nct_id
    t = by_id[nct_id]

    header = (
        f'<div class="tl-mono">{_esc(nct_id)}</div>'
        f'<h4>{_esc(t.get("title")) or "Untitled record"}</h4>'
        f'<div>{pill(t.get("phase") or "phase n/a", "info")}{status_pill(t.get("overall_status"))}'
        f'{pill(t.get("therapeutic_area") or "untagged")}</div>'
    )
    st.markdown(card(header, "tl-card-accent"), unsafe_allow_html=True)
    st.markdown(
        f'<p class="tl-muted">'
        f'<a href="https://clinicaltrials.gov/study/{_esc(nct_id)}" target="_blank">'
        f"View on ClinicalTrials.gov ↗</a></p>",
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(metric_tile("Enrollment", _fmt(t.get("enrollment"), "n/r")), unsafe_allow_html=True)
    c2.markdown(metric_tile("Primary completion", _fmt(t.get("primary_completion_date"), "n/r")),
                unsafe_allow_html=True)
    c3.markdown(metric_tile("Results posted", "yes" if t.get("has_results") else "no"),
                unsafe_allow_html=True)
    c4.markdown(metric_tile("Last changed", _fmt(t.get("last_changed_at"), "n/r")[:10]),
                unsafe_allow_html=True)
    st.write("")

    if t.get("why_stopped"):
        st.markdown(
            card('<div class="tl-label">Why stopped</div>'
                 f'<p>{_esc(t["why_stopped"])}</p>', "tl-card-bad"),
            unsafe_allow_html=True,
        )

    reg = (
        '<div class="tl-label">Registry fields</div>'
        f'<p><b>Sponsor:</b> {_esc(_fmt(t.get("sponsor")))}<br>'
        f'<b>Conditions:</b> {_esc(", ".join(_json_list(t.get("conditions"))) or "not reported")}<br>'
        f'<b>Interventions:</b> {_esc(", ".join(_json_list(t.get("interventions"))) or "not reported")}'
        "</p>"
    )
    st.markdown(card(reg), unsafe_allow_html=True)

    with st.expander("Arms and primary outcome measures (raw registry text)"):
        st.markdown("**Arms**")
        arms = _json_list(t.get("arms"))
        st.markdown("\n".join(f"- {a}" for a in arms) if arms else "_not reported_")
        st.markdown("**Primary outcome measures**")
        outcomes = _json_list(t.get("primary_outcome_measures"))
        st.markdown("\n".join(f"- {o}" for o in outcomes) if outcomes else "_not reported_")

    st.markdown("### Claude-generated summary")

    if not t.get("scientific_summary"):
        st.markdown(
            card('<p>Not summarized yet. Run <code>enrich/run_enrichment.py</code> to '
                 "generate the three sections for this trial.</p>", "tl-card-warn"),
            unsafe_allow_html=True,
        )
        return

    if _is_stale(t):
        st.markdown(
            card('<div class="tl-label">Stale</div>'
                 "<p>This trial changed materially after its summary was generated, so the "
                 "text below describes an older version of the record. Re-run enrichment "
                 "to refresh it.</p>", "tl-card-warn"),
            unsafe_allow_html=True,
        )

    for label, key in [
        ("Scientific summary", "scientific_summary"),
        ("Potential risks", "potential_risks"),
        ("Business impact", "business_impact"),
    ]:
        st.markdown(
            card(f'<div class="tl-label">{label}</div>'
                 f'<p>{_esc(t.get(key)) or "not generated"}</p>'),
            unsafe_allow_html=True,
        )

    st.markdown(
        f'<p class="tl-muted">Generated {_esc(_fmt(t.get("generated_at"), "at an unknown time"))}. '
        "Cached against this trial's materiality hash — re-asking costs nothing.</p>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------
# Change feed
# ---------------------------------------------------------------------

def render_change_feed(trials: list[dict]):
    st.markdown("## Change feed")
    st.markdown(
        '<p class="tl-muted"><b>What this can and cannot show.</b> The schema stores one '
        "current materiality hash per trial, not a history of previous values — full "
        "snapshot history was cut as out of scope for a project with no scheduled runs "
        "(see the scope doc). So this feed can tell you <i>that</i> a trial changed and "
        "<i>when</i>, and it flags summaries that are now stale against the current hash. "
        "It cannot show a field-by-field diff, because the prior hash was never "
        "persisted.</p>",
        unsafe_allow_html=True,
    )

    changed = [
        t for t in trials
        if t.get("last_changed_at") and t["last_changed_at"] != t.get("first_seen_at")
    ]
    changed.sort(key=lambda t: t["last_changed_at"], reverse=True)

    # Set membership on nct_id, not `t in stale_list` -- the latter is a
    # linear dict-equality scan per row, quadratic over the feed.
    stale_ids = {t["nct_id"] for t in trials if _is_stale(t)}

    m1, m2, m3 = st.columns(3)
    m1.markdown(metric_tile("Changed since first pull", len(changed)), unsafe_allow_html=True)
    m2.markdown(metric_tile("Summaries now stale", len(stale_ids)), unsafe_allow_html=True)
    m3.markdown(
        metric_tile("Terminated / withdrawn",
                    sum(1 for t in trials if t.get("overall_status") in ("TERMINATED", "WITHDRAWN"))),
        unsafe_allow_html=True,
    )
    st.write("")

    if not changed:
        st.markdown(
            card('<p class="tl-muted">No trial has changed since it was first pulled — every '
                 "<code>last_changed_at</code> still equals its <code>first_seen_at</code>. "
                 "That is the expected state right after a first ingest.</p>"),
            unsafe_allow_html=True,
        )
        return

    for t in changed:
        variant = "tl-card-warn" if t["nct_id"] in stale_ids else ""
        flags = ""
        if t["nct_id"] in stale_ids:
            flags += pill("summary stale", "warn")
        elif not t.get("scientific_summary"):
            flags += pill("not summarized", "warn")
        body = (
            f'<div class="tl-mono">{_esc(t["nct_id"])} · changed {_esc(t["last_changed_at"])}</div>'
            f'<h4>{_esc(t.get("title")) or "Untitled record"}</h4>'
            f'<div>{pill(t.get("phase") or "phase n/a", "info")}'
            f"{status_pill(t.get('overall_status'))}{flags}</div>"
        )
        st.markdown(card(body, variant), unsafe_allow_html=True)
        if st.button("Open detail", key=f"chg_{t['nct_id']}"):
            goto("Trial detail", t["nct_id"])


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def render_sidebar(trials: list[dict]) -> str:
    with st.sidebar:
        st.markdown("### TrialLens")
        st.markdown(
            '<p class="tl-muted">Clinical trial intelligence over ClinicalTrials.gov, '
            "scoped to antifungal / anti-infective and oncology trials.</p>",
            unsafe_allow_html=True,
        )
        page = st.radio("Go to", PAGES, key="nav", label_visibility="collapsed")
        st.divider()
        st.markdown(
            f'<p class="tl-muted"><b>{len(trials)}</b> trials · '
            f'<b>{sum(1 for t in trials if t.get("scientific_summary"))}</b> summarized</p>',
            unsafe_allow_html=True,
        )
        if st.button("Refresh from disk", use_container_width=True):
            load_trials.clear()
            st.rerun()
        st.markdown(
            '<p class="tl-muted">Browse, Detail, and Change feed are local and free to '
            "reload. Ask calls Claude and the live registry.</p>",
            unsafe_allow_html=True,
        )
    return page


def main():
    st.set_page_config(page_title="TrialLens", page_icon="🔬", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    apply_pending_state()  # must run before any widget is created

    if not DB_PATH.exists():
        st.markdown("## TrialLens")
        st.error(
            f"No database found at `{DB_PATH}`.\n\n"
            "Run the pipeline first, from the repo root:\n\n"
            "```\npython ingest/pull_trials.py\npython normalize/build_db.py\n"
            "python enrich/run_enrichment.py --limit 10\n```"
        )
        st.stop()

    trials = load_trials()
    page = render_sidebar(trials)

    if page == "Ask":
        render_ask(trials)
    elif page == "Browse trials":
        render_browse(trials)
    elif page == "Trial detail":
        render_detail(trials)
    elif page == "Change feed":
        render_change_feed(trials)


if __name__ == "__main__":
    main()
