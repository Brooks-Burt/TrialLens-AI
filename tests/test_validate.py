"""
tests/test_validate.py

Offline tests for stage 2 (pipeline/validate.py) using a fake registry
client, so every branch -- including a failed call -- runs with no network.

Run:
    python tests/test_validate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.ctgov_client import CTGovError, clean_term  # noqa: E402
from pipeline.validate import (  # noqa: E402
    CONDITION_ONLY, DROPPED, KEPT, NO_CONDITIONS, NOTHING_TO_SEARCH, OK, REGISTRY_UNAVAILABLE, UNVERIFIED,
    validate,
)

FAIL = object()  # sentinel: this term's call raises


class FakeClient:
    """count() answers from a dict; FAIL raises like a timeout would."""

    def __init__(self, conditions: dict, interventions: dict):
        self.c, self.i = conditions, interventions

    def count(self, intervention=None, condition=None):
        table, key = (self.c, condition) if condition else (self.i, intervention)
        clean_term(key)  # like the real client: invalid terms raise ValueError before any call
        value = table.get(key, 0)
        if value is FAIL:
            raise CTGovError("Failed to fetch page after 3 attempts: read timeout")
        return value


def status(v, term, kind=None):
    return next(c.status for c in v.checks if c.term == term and (kind is None or c.kind == kind))


def check() -> list[tuple[bool, str]]:
    r = []
    client = FakeClient({"psoriasis": 900, "plaque psoriasis": 400},
                        {"secukinumab": 120, "fakemab": 0, "drugA": 0, "DA-1": 5, "zorblaxumab": 0})

    v = validate(["psoriasis", "plaque psoriasis"], {"secukinumab": [], "fakemab": []}, ["zorblaxumab"], client)
    r.append((v.outcome == OK and list(v.interventions) == ["secukinumab"], "zero-hit drug dropped, real one kept"))
    r.append((status(v, "fakemab") == DROPPED and status(v, "secukinumab") == KEPT, "statuses recorded"))
    r.append((status(v, "zorblaxumab", "exclude") == DROPPED and v.exclude_terms == ["zorblaxumab"],
              "exclude term checked for the report, but still passed through"))
    r.append(("fakemab" in v.message and "zorblaxumab" in v.message,
              f"partial drops are told to the user, not only logged: {v.message!r}"))

    v = validate(["psoriasis"], {"drugA": ["DA-1"]}, [], client)
    r.append((v.interventions == {"drugA": ["DA-1"]}, "drug kept when only its alias exists"))

    v = validate(["psoriasis"], {"fakemab": []}, [], client)
    r.append((v.outcome == CONDITION_ONLY and v.conditions == ["psoriasis"] and v.message,
              "every drug dropped, condition survives -> condition-only, with a message"))

    v = validate(["glorbic syndrome"], {"secukinumab": []}, [], client)
    r.append((v.outcome == NO_CONDITIONS and "glorbic syndrome" in v.message,
              "every condition dropped -> stop with a message naming it"))

    v = validate([], {"fakemab": []}, [], client)
    r.append((v.outcome == NOTHING_TO_SEARCH and v.message, "drug-only question, drug dropped -> stop"))

    # The failed-call case: a timeout must NOT look like zero hits.
    flaky = FakeClient({"psoriasis": 900}, {"secukinumab": FAIL, "fakemab": 0})
    v = validate(["psoriasis"], {"secukinumab": [], "fakemab": []}, [], flaky)
    sec = next(c for c in v.checks if c.term == "secukinumab")
    r.append((sec.status == UNVERIFIED and sec.hits is None and "timeout" in (sec.error or ""),
              "failed call -> unverified with the error, hits=None (not 0)"))
    r.append((list(v.interventions) == ["secukinumab"] and v.outcome == OK,
              "unverified drug is kept, not silently dropped"))
    r.append(("Could not check secukinumab" in v.message, "unverified term is told to the user"))

    v = validate([], {}, [], client)
    r.append((v.outcome == NOTHING_TO_SEARCH and "()" not in v.message and not v.checks,
              "empty input -> nothing to search, no calls, readable message"))

    from common.ctgov_client import CTGovClient
    long_term = "x" * 300
    v = validate(["psoriasis"], {long_term: []}, [], FakeClient({"psoriasis": 900}, {}))
    bad = next(c for c in v.checks if c.term == long_term)
    r.append((bad.status == DROPPED and bad.hits is None and "limit" in (bad.error or "") and not v.interventions,
              "over-long term -> dropped as invalid (not 'unverified, searched anyway')"))
    # And confirm the real client really does raise before any network call.
    try:
        CTGovClient().count(intervention=long_term)
        raised = False
    except ValueError:
        raised = True
    r.append((raised, "real client rejects an over-long term before any network call"))

    down = FakeClient({"psoriasis": FAIL}, {"secukinumab": FAIL})
    v = validate(["psoriasis"], {"secukinumab": []}, [], down)
    r.append((v.outcome == REGISTRY_UNAVAILABLE and v.message, "every call failed -> registry unavailable message"))
    return r


def test_validate():
    failures = [msg for ok, msg in check() if not ok]
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    results = check()
    for ok, msg in results:
        print(f"{'ok  ' if ok else 'FAIL'}  {msg}")
    test_validate()
    print(f"\nall {len(results)} cases pass")
