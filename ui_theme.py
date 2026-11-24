"""
ui_theme.py -- presentation only: the page's CSS and small HTML blocks.

Nothing here decides anything. Every number and label shown comes from the
Answer object that pipeline.run.answer() returns; this file only lays it out.
All text that came from the model or the registry goes through esc() before
it is placed in HTML.
"""

from __future__ import annotations

import html

STAGES = [
    # key in Answer.seconds, name, who does it, colour group
    ("plan", "Plan", "Claude Haiku", "claude"),
    ("validate", "Validate", "Python + registry", "python"),
    ("retrieve", "Retrieve", "Python + registry", "python"),
    ("organize", "Organize", "Python", "python"),
    ("summarize", "Summarize", "Claude Sonnet", "claude"),
]

PHASE_ORDER = ["PHASE4", "PHASE3", "PHASE2", "PHASE1", "EARLY_PHASE1", "no phase listed"]
PHASE_NAME = {"PHASE4": "Phase 4", "PHASE3": "Phase 3", "PHASE2": "Phase 2", "PHASE1": "Phase 1",
              "EARLY_PHASE1": "Early phase 1", "no phase listed": "No phase listed"}

REPO_URL = "https://github.com/Brooks-Burt/TrialLens-AI"


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def model_name(model_id: str) -> str:
    """'claude-sonnet-5-5' -> 'Claude Sonnet 5.5'; anything else is shown as-is."""
    parts = (model_id or "").split("-")
    if len(parts) >= 4 and parts[0] == "claude" and parts[2].isdigit() and parts[3].isdigit():
        return f"Claude {parts[1].title()} {parts[2]}.{parts[3]}"
    return model_id or ""


# --- CSS -------------------------------------------------------------------

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --tl-bg: #070B14;
  --tl-surface: rgba(17, 25, 43, 0.72);
  --tl-surface-solid: #0F1729;
  --tl-border: rgba(148, 163, 184, 0.14);
  --tl-border-strong: rgba(148, 163, 184, 0.26);
  --tl-text: #E6EAF2;
  --tl-muted: #8B97AD;
  --tl-faint: #5B6780;
  --tl-teal: #2DD4BF;
  --tl-cyan: #38BDF8;
  --tl-violet: #A78BFA;
  --tl-red: #F87171;
  --tl-amber: #FBBF24;
  --tl-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
}

/* Font everywhere except icon glyphs and code */
.stApp, .stApp *:not([data-testid="stIconMaterial"]) {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
}
.stApp code, .stApp pre, .stApp code *, .stApp pre * { font-family: var(--tl-mono) !important; }

/* Page background: two soft glows on near-black */
.stApp {
  background:
    radial-gradient(1100px 560px at 12% -8%, rgba(45, 212, 191, 0.11), transparent 62%),
    radial-gradient(900px 520px at 96% -4%, rgba(167, 139, 250, 0.12), transparent 60%),
    var(--tl-bg);
}
[data-testid="stHeader"] { background: transparent; }
[data-testid="stDecoration"] { display: none; }
[data-testid="stMainBlockContainer"], .block-container {
  max-width: 1120px;
  padding-top: 2.6rem;
  padding-bottom: 4rem;
}

/* ---- hero ---- */
.tl-hero { position: relative; padding: 4px 0 6px; }
.tl-hero::before {
  content: ""; position: absolute; inset: -60px 0 -10px 0; z-index: 0; pointer-events: none;
  background-image:
    linear-gradient(rgba(148,163,184,.07) 1px, transparent 1px),
    linear-gradient(90deg, rgba(148,163,184,.07) 1px, transparent 1px);
  background-size: 44px 44px;
  -webkit-mask-image: radial-gradient(ellipse 70% 80% at 30% 0%, #000 20%, transparent 75%);
          mask-image: radial-gradient(ellipse 70% 80% at 30% 0%, #000 20%, transparent 75%);
}
.tl-hero > * { position: relative; z-index: 1; }
.tl-eyebrow {
  display: inline-flex; align-items: center; gap: 9px;
  padding: 6px 12px 6px 10px; border-radius: 999px;
  border: 1px solid var(--tl-border-strong); background: rgba(15, 23, 41, 0.7);
  color: #B8C2D6; font-size: 12.5px; font-weight: 500; letter-spacing: .01em;
}
.tl-live { width: 8px; height: 8px; border-radius: 50%; background: var(--tl-teal);
  box-shadow: 0 0 0 0 rgba(45,212,191,.6); animation: tl-live 2.2s infinite; }
@keyframes tl-live {
  0% { box-shadow: 0 0 0 0 rgba(45,212,191,.55); }
  70% { box-shadow: 0 0 0 9px rgba(45,212,191,0); }
  100% { box-shadow: 0 0 0 0 rgba(45,212,191,0); }
}
.tl-title {
  margin: 18px 0 10px; padding: 0;
  font-size: clamp(44px, 7vw, 68px); line-height: 1.02; font-weight: 800; letter-spacing: -0.035em;
  color: #F5F7FB;
}
.tl-title span {
  background: linear-gradient(92deg, var(--tl-teal) 0%, var(--tl-cyan) 45%, var(--tl-violet) 100%);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.tl-sub { max-width: 720px; margin: 0; color: #A9B4C8; font-size: 16.5px; line-height: 1.6; }
.tl-sub b { color: var(--tl-text); font-weight: 600; }

/* ---- pipeline strip ---- */
.tl-pipe { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px; margin-top: 28px; }
.tl-step {
  position: relative; padding: 12px 14px 12px; border-radius: 12px;
  background: linear-gradient(180deg, rgba(255,255,255,.035), rgba(255,255,255,.012));
  border: 1px solid var(--tl-border); overflow: visible;
  transition: border-color .3s, background .3s;
}
.tl-step::before {
  content: ""; position: absolute; left: 12px; right: 12px; top: -1px; height: 2px; border-radius: 2px;
  background: var(--tl-accent); opacity: .55;
}
.tl-step.claude { --tl-accent: var(--tl-violet); }
.tl-step.python { --tl-accent: var(--tl-teal); }
.tl-step:not(:last-child)::after {
  content: ""; position: absolute; right: -11px; top: 50%; width: 8px; height: 8px;
  border-top: 1.5px solid var(--tl-faint); border-right: 1.5px solid var(--tl-faint);
  transform: translateY(-50%) rotate(45deg);
}
.tl-step-n { font-family: var(--tl-mono) !important; font-size: 11px; color: var(--tl-faint); letter-spacing: .04em; }
.tl-step-name { font-size: 15px; font-weight: 600; color: var(--tl-text); margin-top: 2px; }
.tl-step-who { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--tl-muted); margin-top: 3px; white-space: nowrap; }
.tl-step-who i { width: 6px; height: 6px; border-radius: 50%; background: var(--tl-accent); display: inline-block; flex: none; }
.tl-step-t {
  position: absolute; top: 10px; right: 12px;
  font-family: var(--tl-mono) !important; font-size: 11px; color: var(--tl-accent);
}
.tl-step.done { border-color: color-mix(in srgb, var(--tl-accent) 40%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, var(--tl-accent) 9%, transparent), rgba(255,255,255,.01)); }
.tl-step.done::before { opacity: 1; }
.tl-step.skipped { opacity: .45; }
.tl-step.running { animation: tl-run 5s infinite; }
.tl-step.running:nth-child(1) { animation-delay: 0s; }
.tl-step.running:nth-child(2) { animation-delay: 1s; }
.tl-step.running:nth-child(3) { animation-delay: 2s; }
.tl-step.running:nth-child(4) { animation-delay: 3s; }
.tl-step.running:nth-child(5) { animation-delay: 4s; }
@keyframes tl-run {
  0%, 30%, 100% { border-color: var(--tl-border); box-shadow: none; }
  10% { border-color: var(--tl-accent); box-shadow: 0 0 0 1px var(--tl-accent), 0 0 28px -6px var(--tl-accent); }
}
.tl-legend { display: flex; flex-wrap: wrap; gap: 6px 22px; margin-top: 12px; font-size: 12.5px; color: var(--tl-muted); }
.tl-legend span { display: inline-flex; align-items: center; gap: 7px; }
.tl-legend i { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.tl-run-note { margin-top: 12px; font-size: 13px; color: var(--tl-teal); font-family: var(--tl-mono) !important; }

/* ---- search box ---- */
[data-testid="stForm"] { border: none; padding: 0; margin-top: 30px; }
[data-testid="stTextInput"] [data-baseweb="input"] {
  background: rgba(15, 23, 41, 0.85); border: 1px solid var(--tl-border-strong);
  border-radius: 12px; min-height: 52px; transition: border-color .2s, box-shadow .2s;
}
[data-testid="stTextInput"] [data-baseweb="input"]:focus-within {
  border-color: var(--tl-teal); box-shadow: 0 0 0 4px rgba(45, 212, 191, 0.14);
}
[data-testid="stTextInput"] input { font-size: 16px; color: var(--tl-text); padding-left: 16px; }
[data-testid="stTextInput"] input::placeholder { color: var(--tl-faint); }
[data-testid="stTextInputRootElement"], [data-testid="stTextInput"] [data-baseweb="input"] { height: 52px !important; }
[data-testid="stTextInput"] [data-baseweb="base-input"] { height: 100%; background: transparent; }
[data-testid="InputInstructions"] { display: none; }

button[kind="primaryFormSubmit"], button[kind="primary"],
[data-testid="stBaseButton-primaryFormSubmit"], [data-testid="stBaseButton-primary"] {
  min-height: 52px; border-radius: 12px; border: none;
  background: linear-gradient(135deg, #2DD4BF 0%, #38BDF8 100%);
  color: #04131A; font-weight: 650; letter-spacing: .01em;
  box-shadow: 0 8px 28px -10px rgba(45, 212, 191, .6);
  transition: transform .15s, box-shadow .2s, filter .2s;
}
button[kind="primaryFormSubmit"] p, button[kind="primary"] p { color: #04131A; font-weight: 650; }
button[kind="primaryFormSubmit"]:hover, button[kind="primary"]:hover {
  filter: brightness(1.08); transform: translateY(-1px); color: #04131A;
  box-shadow: 0 12px 32px -10px rgba(45, 212, 191, .75);
}

/* example questions as pills */
.tl-try { font-size: 12px; color: var(--tl-faint); text-transform: uppercase; letter-spacing: .09em; margin: 4px 0 8px; }
.st-key-tl_examples button, .st-key-tl_rephrase button {
  border-radius: 12px; border: 1px solid var(--tl-border); background: rgba(15, 23, 41, 0.55);
  min-height: 44px; padding: 8px 14px; text-align: left; justify-content: flex-start;
  transition: border-color .2s, background .2s;
}
.st-key-tl_examples button p, .st-key-tl_rephrase button p { font-size: 13.5px; color: #B8C2D6; text-align: left; }
.st-key-tl_examples button:hover, .st-key-tl_rephrase button:hover {
  border-color: rgba(45, 212, 191, .5); background: rgba(45, 212, 191, .06);
}
.st-key-tl_examples button:hover p, .st-key-tl_rephrase button:hover p { color: var(--tl-text); }

/* ---- shared cards ---- */
.tl-section { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin: 34px 0 12px; }
.tl-section h4 { margin: 0; padding: 0; font-size: 12.5px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; color: var(--tl-muted); }
.tl-section .tl-aside { font-size: 12.5px; color: var(--tl-faint); }
.tl-card {
  background: var(--tl-surface); border: 1px solid var(--tl-border); border-radius: 16px;
  padding: 18px 20px; backdrop-filter: blur(6px);
}
.tl-label { font-size: 11.5px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; color: var(--tl-muted); }
.tl-interp { display: flex; gap: 16px; align-items: flex-start; margin-top: 30px; }
.tl-interp-icon {
  flex: none; width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center;
  background: rgba(167, 139, 250, .12); border: 1px solid rgba(167, 139, 250, .35); color: var(--tl-violet);
}
.tl-interp-text { font-size: 16px; color: var(--tl-text); margin-top: 4px; line-height: 1.55; }
.tl-by { font-size: 12px; color: var(--tl-faint); margin-top: 6px; }

/* metrics */
.tl-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.tl-metric { background: var(--tl-surface); border: 1px solid var(--tl-border); border-radius: 14px; padding: 16px 18px; }
.tl-metric-v { font-size: 30px; font-weight: 700; letter-spacing: -.02em; color: #F5F7FB; font-variant-numeric: tabular-nums; line-height: 1.15; }
.tl-metric-v small { font-size: 15px; font-weight: 500; color: var(--tl-muted); margin-left: 2px; }
.tl-metric-l { font-size: 12.5px; color: var(--tl-muted); margin-top: 4px; line-height: 1.4; }
.tl-metric.hl .tl-metric-v {
  background: linear-gradient(92deg, var(--tl-teal), var(--tl-cyan));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}

/* registry check chips */
.tl-checks { display: flex; flex-wrap: wrap; gap: 8px; }
.tl-chip {
  display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px 6px 7px; border-radius: 999px;
  font-size: 13px; border: 1px solid; line-height: 1.2;
}
.tl-chip .ic { width: 20px; height: 20px; border-radius: 50%; display: grid; place-items: center; font-size: 11px; font-weight: 700; flex: none; }
.tl-chip .term { font-weight: 600; }
.tl-chip .kind { font-size: 11.5px; opacity: .7; }
.tl-chip .n { font-size: 12px; opacity: .9; font-variant-numeric: tabular-nums; }
.tl-chip.kept { background: rgba(45,212,191,.07); border-color: rgba(45,212,191,.32); color: #A7F3E8; }
.tl-chip.kept .ic { background: rgba(45,212,191,.18); color: var(--tl-teal); }
.tl-chip.dropped { background: rgba(248,113,113,.08); border-color: rgba(248,113,113,.45); color: #FECACA;
  box-shadow: 0 0 24px -8px rgba(248,113,113,.55); }
.tl-chip.dropped .ic { background: rgba(248,113,113,.2); color: var(--tl-red); }
.tl-chip.dropped .term { text-decoration: line-through; text-decoration-color: rgba(248,113,113,.8); }
.tl-chip.unverified { background: rgba(251,191,36,.07); border-color: rgba(251,191,36,.35); color: #FDE68A; }
.tl-chip.unverified .ic { background: rgba(251,191,36,.18); color: var(--tl-amber); }
.tl-check-note { font-size: 12.5px; color: var(--tl-faint); margin-top: 12px; line-height: 1.5; }
.tl-check-note b { color: var(--tl-muted); font-weight: 600; }

/* answer */
.tl-answer {
  position: relative; border-radius: 16px; padding: 22px 24px 18px;
  border: 1px solid rgba(45, 212, 191, .22);
  background: linear-gradient(180deg, rgba(45,212,191,.07), rgba(15,23,41,.65) 55%);
  box-shadow: 0 20px 60px -30px rgba(45, 212, 191, .35);
}
.tl-answer::before {
  content: ""; position: absolute; left: 24px; right: 24px; top: -1px; height: 1px;
  background: linear-gradient(90deg, transparent, var(--tl-teal), var(--tl-violet), transparent);
}
.tl-answer-text { font-size: 16.5px; line-height: 1.75; color: #DCE2EE; }
.tl-answer-text .lead { color: #F5F7FB; font-weight: 600; border-bottom: 1px dashed rgba(45,212,191,.55); }
.tl-attrib { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--tl-border); }
.tl-tag { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; color: var(--tl-muted);
  padding: 4px 10px; border-radius: 999px; border: 1px solid var(--tl-border); background: rgba(255,255,255,.02); }
.tl-tag i { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.tl-answer-note { font-size: 12px; color: var(--tl-faint); margin-top: 10px; }

/* phase bar */
.tl-bar { display: flex; height: 12px; border-radius: 999px; overflow: hidden; gap: 2px; background: rgba(255,255,255,.04); }
.tl-bar div { height: 100%; }
.tl-bar-legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 12px; font-size: 12.5px; color: var(--tl-muted); }
.tl-bar-legend span { display: inline-flex; align-items: center; gap: 7px; }
.tl-bar-legend i { width: 9px; height: 9px; border-radius: 3px; display: inline-block; }
.tl-bar-legend b { color: var(--tl-text); font-weight: 600; font-variant-numeric: tabular-nums; }

.tl-prow { display: grid; grid-template-columns: 120px 1fr 70px; align-items: center; gap: 14px; padding: 5px 0; }
.tl-prow-l { font-size: 13px; color: var(--tl-muted); }
.tl-prow-track { height: 10px; border-radius: 999px; background: rgba(255,255,255,.05); overflow: hidden; }
.tl-prow-track div { height: 100%; border-radius: 999px; }
.tl-prow-n { font-size: 13px; font-weight: 600; color: var(--tl-text); text-align: right; font-variant-numeric: tabular-nums; }

/* ---- Streamlit widgets ---- */
[data-testid="stDataFrame"] { border: 1px solid var(--tl-border); border-radius: 12px; overflow: hidden; }
[data-testid="stExpander"] details {
  background: var(--tl-surface); border: 1px solid var(--tl-border); border-radius: 14px;
}
[data-testid="stExpander"] summary { padding-top: 14px; padding-bottom: 14px; }
[data-testid="stExpander"] summary p { font-weight: 600; font-size: 14.5px; }
[data-testid="stAlertContainer"] { border-radius: 12px; border: 1px solid var(--tl-border); }
[data-testid="stCaptionContainer"] { color: var(--tl-faint); }
hr { border-color: var(--tl-border) !important; }

/* footer */
.tl-footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 10px 24px;
  margin-top: 56px; padding-top: 20px; border-top: 1px solid var(--tl-border); font-size: 12.5px; color: var(--tl-faint); }
.tl-footer a { color: var(--tl-muted); text-decoration: none; border-bottom: 1px solid var(--tl-border-strong); }
.tl-footer a:hover { color: var(--tl-teal); border-color: var(--tl-teal); }

@media (max-width: 760px) {
  .tl-pipe { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .tl-step:not(:last-child)::after { display: none; }
  .tl-step-t { position: static; display: block; margin-top: 4px; }
}
</style>
"""


# --- HTML blocks -------------------------------------------------------------

def hero(planner_model: str, writer_model: str, *, timings: dict | None = None, running: bool = False) -> str:
    """
    The title block plus the five-step strip. `timings` maps a stage key to
    the text shown on that step ("1.2s", or "" for done-but-untimed); a stage
    missing from it is shown as not reached. None means no question yet.
    """
    who = {"plan": model_name(planner_model) or "Claude", "summarize": model_name(writer_model) or "Claude"}
    steps = []
    for i, (key, name, default_who, kind) in enumerate(STAGES, 1):
        state = ""
        t = ""
        if running:
            state = "running"
        elif timings is not None:
            if key in timings:
                state, t = "done", timings[key]
            else:
                state = "skipped"
        label = who.get(key, default_who)
        steps.append(
            f'<div class="tl-step {kind} {state}">'
            f'<div class="tl-step-n">0{i}</div>'
            + (f'<div class="tl-step-t">{esc(t)}</div>' if t else "")
            + f'<div class="tl-step-name">{name}</div>'
            f'<div class="tl-step-who"><i></i>{esc(label)}</div></div>'
        )
    note = ('<div class="tl-run-note">Running: reading the question, checking terms against the registry, '
            'searching, writing...</div>') if running else (
        '<div class="tl-legend">'
        '<span><i style="background:var(--tl-violet)"></i>Claude: reads the question, writes the paragraph</span>'
        '<span><i style="background:var(--tl-teal)"></i>Python + live registry: checks every term, searches, counts, ranks</span>'
        '</div>')
    return (
        '<div class="tl-hero">'
        '<div class="tl-eyebrow"><span class="tl-live"></span>Live ClinicalTrials.gov search &middot; interventional studies</div>'
        '<h1 class="tl-title">Trial<span>Lens</span></h1>'
        '<p class="tl-sub">Ask a question about clinical trials. TrialLens searches the live registry, '
        '<b>checks every term the model proposes against it</b>, and writes a short answer from the trials it found, '
        'shown beside the trials themselves.</p>'
        f'<div class="tl-pipe">{"".join(steps)}</div>{note}'
        '</div>'
    )


def section(title: str, aside: str = "") -> str:
    return f'<div class="tl-section"><h4>{esc(title)}</h4><div class="tl-aside">{esc(aside)}</div></div>'


def interpretation(text: str, planner_model: str) -> str:
    icon = '<span style="font-size:22px;line-height:1;margin-top:8px;font-weight:700">&ldquo;</span>'
    return (
        f'<div class="tl-card tl-interp"><div class="tl-interp-icon">{icon}</div><div>'
        f'<div class="tl-label">I read this as</div>'
        f'<div class="tl-interp-text">{esc(text)}</div>'
        f'<div class="tl-by">Planner: {esc(model_name(planner_model))}</div></div></div>'
    )


def metrics(items: list[tuple[str, str, str, bool]]) -> str:
    """items: (value, unit, label, highlight)."""
    cards = "".join(
        f'<div class="tl-metric{" hl" if hl else ""}"><div class="tl-metric-v">{esc(v)}'
        + (f'<small>{esc(u)}</small>' if u else "")
        + f'</div><div class="tl-metric-l">{esc(label)}</div></div>'
        for v, u, label, hl in items
    )
    return f'<div class="tl-metrics">{cards}</div>'


def registry_checks(checks: list[tuple[str, str, str, int | None]]) -> str:
    """checks: (term, kind label, status kept|dropped|unverified, hits)."""
    icon = {"kept": "&#10003;", "dropped": "&#10005;", "unverified": "?"}
    chips = []
    for term, kind, status, hits in checks:
        if status == "dropped":
            n = "0 trials &middot; dropped"
        elif hits is None:
            n = "registry unreachable"
        else:
            n = f"{hits:,} trial{'s' if hits != 1 else ''}"
        chips.append(
            f'<span class="tl-chip {status}"><span class="ic">{icon.get(status, "?")}</span>'
            f'<span class="term">{esc(term)}</span><span class="kind">{esc(kind)}</span>'
            f'<span class="n">{n}</span></span>'
        )
    kept = sum(c[2] == "kept" for c in checks)
    dropped = sum(c[2] == "dropped" for c in checks)
    note = (f'<div class="tl-check-note"><b>{kept} kept, {dropped} dropped.</b> Each term the planner suggested was '
            'counted in the live registry before any search ran. A term with zero trials is dropped and shown here, '
            "whatever confidence the model gave it. These counts are for each term on its own (any phase, "
            "status or study type), not for your search.</div>")
    return f'<div class="tl-card"><div class="tl-checks">{"".join(chips)}</div>{note}</div>'


def answer_card(lead: str, summary: str, writer_model: str) -> str:
    paragraph = esc(summary).replace("\n\n", "<br><br>").replace("\n", " ")
    return (
        '<div class="tl-answer">'
        f'<div class="tl-answer-text"><span class="lead" title="Written by Python, not the model">{esc(lead)}</span> '
        f'{paragraph}</div>'
        '<div class="tl-attrib">'
        '<span class="tl-tag"><i style="background:var(--tl-teal)"></i>First sentence and every count: Python</span>'
        f'<span class="tl-tag"><i style="background:var(--tl-violet)"></i>Paragraph: {esc(model_name(writer_model))}, '
        'from the counted facts only</span>'
        '</div>'
        '<div class="tl-answer-note">The paragraph is not yet checked automatically: the table below is the check.</div>'
        '</div>'
    )


def phase_bar(phase_counts: dict[str, int]) -> str:
    colors = {"PHASE4": "#A78BFA", "PHASE3": "#2DD4BF", "PHASE2": "#38BDF8", "PHASE1": "#60A5FA",
              "EARLY_PHASE1": "#818CF8", "no phase listed": "#475569"}
    total = sum(phase_counts.values()) or 1
    keys = [k for k in PHASE_ORDER if phase_counts.get(k)] + [k for k in phase_counts if k not in PHASE_ORDER]
    bars = "".join(f'<div style="width:{100 * phase_counts[k] / total:.2f}%;background:{colors.get(k, "#475569")}"></div>'
                   for k in keys)
    legend = "".join(f'<span><i style="background:{colors.get(k, "#475569")}"></i>{esc(PHASE_NAME.get(k, k))} '
                     f'<b>{phase_counts[k]}</b></span>' for k in keys)
    return f'<div class="tl-card"><div class="tl-bar">{bars}</div><div class="tl-bar-legend">{legend}</div></div>'


def phase_rows(counts: dict[str, int | None], total: int | None) -> str:
    """One bar per phase, scaled to the whole search. Not stacked: a phase 2/3 trial counts in two rows."""
    colors = {"PHASE4": "#A78BFA", "PHASE3": "#2DD4BF", "PHASE2": "#38BDF8", "PHASE1": "#60A5FA",
              "EARLY_PHASE1": "#818CF8"}
    order = [k for k in reversed(PHASE_ORDER) if k in counts] + [k for k in counts if k not in PHASE_ORDER]
    scale = total or max([v for v in counts.values() if v] or [1])
    rows = []
    for k in order:
        n = counts[k]
        width = 0 if not n else max(1.5, 100 * n / scale)
        rows.append(
            f'<div class="tl-prow"><span class="tl-prow-l">{esc(PHASE_NAME.get(k, k))}</span>'
            f'<div class="tl-prow-track"><div style="width:{min(width, 100):.2f}%;background:{colors.get(k, "#475569")}"></div></div>'
            f'<span class="tl-prow-n">{"unknown" if n is None else f"{n:,}"}</span></div>'
        )
    return f'<div class="tl-card">{"".join(rows)}</div>'


def footer(planner_model: str, writer_model: str) -> str:
    return (
        '<div class="tl-footer">'
        f'<span>Data: ClinicalTrials.gov API v2, queried live for every question; nothing is stored. '
        f'Planner: {esc(model_name(planner_model))}. Writer: {esc(model_name(writer_model))}.</span>'
        f'<span><a href="{REPO_URL}" target="_blank" rel="noopener">Source on GitHub</a></span>'
        '</div>'
    )
