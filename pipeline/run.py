"""
pipeline/run.py -- runs the five stages in order and returns one Answer that
the UI can render. This is the only module that imports the stages; the
stages themselves never import each other's functions.

    question -> plan -> validate -> retrieve -> organize -> summarize

answer() never raises. Every exit is a status plus a plain-language message,
so no stack trace can reach the page (the traceback goes to the console).

Run directly (calls Anthropic and the registry):
    .venv/bin/python pipeline/run.py "what is being tested for gout?"
"""

from __future__ import annotations

import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.organize import Facts, breadth_note, lead_sentence, organize, render_facts  # noqa: E402
from pipeline.planner import ERROR, PLAN, Plan, plan_question  # noqa: E402
from pipeline.retrieve import TrialGroup, count_matching, retrieve  # noqa: E402
from pipeline.summarize import Summary, summarize  # noqa: E402
from pipeline.validate import CONDITION_ONLY, OK, Validation, validate  # noqa: E402

ANSWERED = "answered"    # lead sentence + summary (or a note that it failed) + table
DECLINED = "declined"    # unsupported / too vague / invalid input: reason + example rephrasings
STOPPED = "stopped"      # validation or search left nothing to show
NO_TRIALS = "no_trials"  # real terms, but nothing matched (e.g. filters)
FAILED = "error"


@dataclass
class Answer:
    question: str
    status: str
    message: str = ""                       # plain-language notes for the user
    plan: Plan | None = None
    validation: Validation | None = None
    groups: list[TrialGroup] = field(default_factory=list)
    total_query: dict = field(default_factory=dict)  # the registry query behind M
    facts: Facts | None = None
    facts_text: str = ""                    # exactly what the writer saw, for the trace panel
    lead: str = ""
    summary: Summary | None = None
    seconds: dict = field(default_factory=dict)
    tokens: dict = field(default_factory=dict)


def _tokens(a: Answer) -> dict:
    calls = {"planner": a.plan.usage if a.plan else {}, "writer": a.summary.usage if a.summary else {}}
    keys = ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]
    calls["total"] = {k: sum(c.get(k, 0) for c in calls.values()) for k in keys}
    return calls


def answer(question: str) -> Answer:
    try:
        a = _answer(question)
    except Exception as e:  # last resort: the page must never show a stack trace
        traceback.print_exc()
        a = Answer(question, FAILED, message=f"Something went wrong while answering ({type(e).__name__}). "
                                             "The details are in the server log.")
    a.tokens = _tokens(a)
    return a


def _answer(question: str) -> Answer:
    t0 = time.monotonic()
    plan = plan_question(question)
    a = Answer(question, DECLINED, plan=plan)
    a.seconds["plan"] = plan.seconds
    if plan.result == ERROR:
        a.status, a.message = FAILED, plan.reason
        return a
    if plan.result != PLAN:
        a.message = plan.reason
        return a

    t = time.monotonic()
    v = validate(plan.conditions, {c.term: c.aliases for c in plan.candidates}, plan.exclude_terms)
    a.validation, a.seconds["validate"] = v, round(time.monotonic() - t, 1)
    a.message = v.message
    if v.outcome not in (OK, CONDITION_ONLY):
        a.status = STOPPED
        return a

    t = time.monotonic()
    a.groups = retrieve(v.conditions, list(v.interventions), plan.phases, plan.statuses,
                        plan.exclude_terms, aliases=v.interventions)
    all_names = [n for term, aliases in v.interventions.items() for n in [term, *aliases]]
    total, a.total_query = count_matching(v.conditions, all_names, plan.phases, plan.statuses)
    a.seconds["retrieve"] = round(time.monotonic() - t, 1)
    if all(g.error for g in a.groups):
        a.status = STOPPED
        a.message = " ".join(filter(None, [a.message, "The registry search failed, so there are no trials to show. "
                                                      "Please try again shortly."]))
        return a

    a.facts = organize(a.groups, total)
    a.lead = lead_sentence(a.facts)
    if a.facts.retrieved == 0:
        a.status = NO_TRIALS
        filters = [f for f in [", ".join(plan.phases), ", ".join(plan.statuses)] if f]
        hint = f" with the filters {' and '.join(filters)}" if filters else ""
        a.message = " ".join(filter(None, [a.message, f"No trials in the registry matched this search{hint}."]))
        return a

    a.message = " ".join(filter(None, [a.message, breadth_note(a.facts)]))
    a.facts_text = render_facts(plan.interpretation, a.facts, plan.exclude_terms)
    a.summary = summarize(a.facts_text)
    a.seconds["summarize"] = a.summary.seconds
    a.seconds["total"] = round(time.monotonic() - t0, 1)
    a.status = ANSWERED
    if a.summary.error:
        a.message = " ".join(filter(None, [a.message, "The written summary could not be produced "
                                                      f"({a.summary.error}); the trials are listed below."]))
    return a


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What is being tested for gout?"
    a = answer(q)
    print(f"\nSTATUS: {a.status}")
    if a.plan and a.plan.interpretation:
        print(f"I read this as: {a.plan.interpretation}")
    if a.message:
        print(f"NOTE: {a.message}")
    if a.plan and a.plan.example_rephrasings:
        print("TRY:", *a.plan.example_rephrasings, sep="\n  - ")
    if a.lead:
        print(f"\n{a.lead} {a.summary.text if a.summary else ''}")
    if a.facts:
        print("\nGROUPS:")
        for g in a.facts.groups:
            print(f"  {g.term or '(condition-only)'}: registry {g.registry_total}, core {g.core}, "
                  f"comparator {g.comparator}, off-topic {g.off_topic}, active {g.active}, top {g.top_phase}"
                  + (f", ERROR {g.error[:60]}" if g.error else ""))
        print(f"  table rows: {len(a.facts.rows)}; sent to writer: {len(a.facts.summary_trials)}")
    print(f"\nSECONDS: {a.seconds}")
    print(f"TOKENS: {a.tokens['total']}")
