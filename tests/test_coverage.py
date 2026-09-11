"""
tests/test_coverage.py

Turns v1 bug #6 from a documented discipline into an enforced constraint.

v1 bug #6 was this: the query planner's system prompt said its scope was
"antifungals and anti-infectives," but ingest/query_config.json contained
fungal terms only. A question about bacterial pneumonia was therefore read
as in-scope, the planner confidently built a fully validated query for real
antibiotics, and it returned nothing -- with no indication to the user why.
Nothing was broken. The model's judgment was reasonable. The two sources of
truth had simply drifted apart.

The v1 scope doc recorded the mitigation as "a manual discipline, not an
enforced constraint." That is an accurate description of a gap, not a fix.
This file is the fix.

Three properties are checked:

  1. STRUCTURAL   -- the config parses, has the fields ingest expects, and
                     contains no duplicate or whitespace-damaged terms.
  2. COVERAGE     -- every term in the config actually returned trials.
                     Read from data/raw/_manifest.json, which ingest writes
                     as a byproduct of per-term querying. No network needed.
  3. LIVE (opt-in) -- every term returns non-zero hits against the registry
                     right now, via count-only calls. Catches a typo or a
                     renamed compound before a multi-hour ingest, and catches
                     drift after one.

Run offline (default -- structural + coverage):
    python tests/test_coverage.py
    pytest tests/test_coverage.py

Run including live registry checks:
    python tests/test_coverage.py --live
    TRIALLENS_LIVE=1 pytest tests/test_coverage.py

Live mode is opt-in because it makes one HTTP request per term (~160) and
a test suite that needs the network to pass is a test suite people stop
running. The count-only call shape is the cheapest CT.gov offers -- the
same one v1's term validation used.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import SkipTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import QUERY_CONFIG_PATH, RAW_MANIFEST_PATH  # noqa: E402

# Only groups that ingest actually QUERIES belong here, because every
# coverage assertion in this file is "did this term return trials?" -- a
# question that is meaningless for a term no query ever ran.
#
# pathogen_lexicon is excluded for that reason. It used to be a queried
# group; the first antifungal dry run showed CT.gov does not index
# conditions at species level (6 of 20 organisms returned zero, survivors
# returned 3-4 trials each), so pathogens became a lexicon matched against
# trial text instead. Leaving it here would have produced 20 permanently
# failing assertions and the honest fix would have been to delete them,
# which is how a suite stops being trusted.
TERM_GROUPS = {
    "condition_terms": "condition",
    "intervention_terms": "intervention",
    "sponsor_terms": "sponsor",
    "mechanism_terms": "mechanism",
    "related_terms": "related",
    "population_terms": "population",
}

# Validated for shape, not for hit counts.
LEXICON_KEYS = ("pathogen_lexicon",)

# unittest.SkipTest is used rather than an early `return` for the
# manifest-dependent tests, and rather than pytest.skip so the file runs
# without pytest installed. pytest honours SkipTest natively.
#
# The distinction matters more than it looks. An early return makes a test
# that cannot run report PASS, and a suite that reports "7 passed" when the
# data it checks does not exist is actively misleading -- it is the same
# failure mode as v1 bug #2, where a broken check reported success because
# it was never actually comparing anything. A skipped test is honest about
# knowing nothing; a vacuous pass claims knowledge it does not have.


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def load_config(enabled_only: bool = False) -> dict:
    """
    `enabled_only` splits the suite deliberately.

    STRUCTURAL tests run against ALL areas, including disabled ones. A typo
    or a duplicate in antibacterials is still a defect today -- it is just a
    defect that has not been ingested yet, and the whole point of keeping
    those areas in the file is that re-enabling them should be a one-line
    change rather than a debugging session.

    COVERAGE tests run against enabled areas only, because a disabled area
    has no manifest entries by definition. Enforcing coverage there would
    fail permanently and the honest response would be to delete the
    assertion, which is how a test suite stops being trusted.
    """
    with open(QUERY_CONFIG_PATH) as f:
        config = json.load(f)
    areas = {k: v for k, v in config.items() if not k.startswith("_")}
    if enabled_only:
        areas = {k: v for k, v in areas.items() if v.get("enabled", True) is not False}
    return areas


def load_manifest() -> dict | None:
    if not RAW_MANIFEST_PATH.exists():
        return None
    with open(RAW_MANIFEST_PATH) as f:
        return json.load(f)


def require_manifest() -> dict:
    """Return the manifest, or skip -- never silently pass."""
    manifest = load_manifest()
    if manifest is None:
        raise SkipTest(
            f"no manifest at {RAW_MANIFEST_PATH}; run ingest/pull_trials.py first"
        )
    return manifest


def iter_terms(config: dict):
    """Yield (area, group, term) for every configured term."""
    for area, spec in config.items():
        for config_key, group in TERM_GROUPS.items():
            for term in spec.get(config_key) or []:
                yield area, group, term


# --------------------------------------------------------------------------
# 1. structural
# --------------------------------------------------------------------------

def test_config_parses_and_has_required_fields():
    config = load_config()
    assert config, "query_config.json contains no therapeutic areas"

    for area, spec in config.items():
        assert isinstance(spec, dict), f"{area}: not an object"

        # An area with no condition and no intervention terms would be
        # silently skipped by ingest, producing an empty area that the
        # dashboard still offers as a filter.
        has_terms = any(spec.get(k) for k in TERM_GROUPS)
        assert has_terms, f"{area}: no terms in any group -- ingest would pull nothing"

        combine = spec.get("combine", "or")
        assert combine in ("or", "and"), f"{area}: combine must be 'or' or 'and', got {combine!r}"

        # A truthy string like "false" would read as enabled. is_enabled()
        # in ingest checks `is not False`, so only a real JSON boolean
        # disables an area -- anything else silently ingests it.
        enabled = spec.get("enabled", True)
        assert isinstance(enabled, bool), (
            f"{area}: enabled must be a JSON boolean, got {enabled!r}. "
            "A non-boolean reads as enabled and the area is ingested anyway."
        )

    enabled_areas = [a for a, s in config.items()
                     if s.get("enabled", True) is not False]
    assert enabled_areas, (
        "No area has enabled:true. '--area all' would ingest nothing and "
        "every coverage test would skip, so the suite would report success "
        "while checking no data at all."
    )


def test_lexicons_are_well_formed():
    """
    Lexicon entries are validated for SHAPE only -- never for hit counts.

    This is the distinction the pathogen finding forced. A queried term can
    be wrong in a way the registry will tell you about (zero hits). A
    lexicon entry cannot: it is matched against trial text downstream, so
    "does CT.gov return anything for this string?" is not a question that
    has meaning here. Asserting hits on a lexicon is how you end up with 20
    permanently red assertions and a suite nobody trusts.

    What CAN be checked is that entries are clean, unique strings -- a
    duplicate double-counts a pathogen in the landscape, and stray
    whitespace breaks the text match silently.
    """
    config = load_config()
    problems = []

    for area, spec in config.items():
        for key in LEXICON_KEYS:
            entries = spec.get(key)
            if entries is None:
                continue
            if not isinstance(entries, list):
                problems.append(f"{area}/{key}: expected a list, got {type(entries).__name__}")
                continue

            seen = set()
            for entry in entries:
                if not isinstance(entry, str) or not entry.strip():
                    problems.append(f"{area}/{key}: bad entry {entry!r}")
                    continue
                if entry != entry.strip():
                    problems.append(f"{area}/{key}: {entry!r} has surrounding whitespace")
                folded = entry.lower()
                if folded in seen:
                    problems.append(f"{area}/{key}: duplicate {entry!r}")
                seen.add(folded)

    assert not problems, "Lexicon problems:\n  " + "\n  ".join(problems)


def test_lexicon_terms_are_not_also_queried():
    """
    A string in both a queried term list and a lexicon means the pathogen
    mistake is creeping back: it would select the corpus AND be derived from
    text, double-counting the trial's attribution and reintroducing exactly
    the species-level condition queries that returned zero.
    """
    config = load_config()
    problems = []

    for area, spec in config.items():
        queried = {t.strip().lower() for _, _, t in iter_terms({area: spec})}
        for key in LEXICON_KEYS:
            for entry in spec.get(key) or []:
                if isinstance(entry, str) and entry.strip().lower() in queried:
                    problems.append(f"{area}: {entry!r} is in both {key} and a queried term list")

    assert not problems, "Lexicon/term overlap:\n  " + "\n  ".join(problems)


def test_no_duplicate_terms_within_an_area():
    """
    A duplicate term is a wasted request and a double-counted coverage
    number. Checked case-insensitively across ALL groups in an area, not
    per group: the same string appearing in both condition_terms and
    pathogen_terms would be queried twice against the same field and
    recorded under two different match reasons, which makes the manifest's
    attribution wrong rather than merely redundant.
    """
    config = load_config()
    problems = []

    for area, spec in config.items():
        seen: dict[str, str] = {}
        for config_key, group in TERM_GROUPS.items():
            for term in spec.get(config_key) or []:
                key = term.strip().lower()
                if key in seen:
                    problems.append(f"{area}: {term!r} in both {seen[key]} and {config_key}")
                seen[key] = config_key

    assert not problems, "Duplicate terms:\n  " + "\n  ".join(problems)


def test_terms_are_clean_strings():
    """
    Leading/trailing whitespace survives into the quoted query string and
    changes the match. An empty term matches everything. Neither errors.
    """
    config = load_config()
    problems = []

    for area, group, term in iter_terms(config):
        if not isinstance(term, str):
            problems.append(f"{area}/{group}: {term!r} is not a string")
        elif not term.strip():
            problems.append(f"{area}/{group}: empty term")
        elif term != term.strip():
            problems.append(f"{area}/{group}: {term!r} has surrounding whitespace")

    assert not problems, "Malformed terms:\n  " + "\n  ".join(problems)


# --------------------------------------------------------------------------
# 2. coverage, from the ingest manifest
# --------------------------------------------------------------------------

def test_manifest_exists():
    """
    The one manifest-dependent test that FAILS rather than skips when the
    manifest is missing, so an un-ingested corpus is loud once instead of
    silent seven times.
    """
    assert load_manifest() is not None, (
        f"No manifest at {RAW_MANIFEST_PATH}.\n"
        "Run `python ingest/pull_trials.py --area all` first. Coverage cannot "
        "be verified from the config alone -- that is precisely the gap that "
        "let v1 bug #6 through."
    )


def test_every_configured_term_was_queried():
    """
    A term in the config with no entry in the manifest means ingest never
    ran it. That happens when a term is added to the config after the last
    pull -- which is the exact state v1 bug #6 lived in, except in reverse.
    """
    manifest = require_manifest()
    config = load_config(enabled_only=True)
    term_hits = manifest.get("term_hits", {})
    missing = [
        f"{area}/{group}/{term}"
        for area, group, term in iter_terms(config)
        if term not in term_hits.get(area, {}).get(group, {})
    ]

    assert not missing, (
        f"{len(missing)} configured term(s) never queried -- re-run ingest:\n  "
        + "\n  ".join(missing)
    )


def test_no_dead_terms():
    """
    The core assertion. A term with zero hits contributes nothing to the
    corpus while making the config look broader than the data is. That
    mismatch between stated and actual scope IS v1 bug #6.

    A term may be exempted via the area's `known_zero` map, but only with a
    written reason. That mechanism exists because some zeros are legitimate
    -- a real pathogen the registry does not index at species level is not
    the same defect as a misspelled compound -- and the alternatives were
    both worse: deleting the term destroys the evidence that the question
    was asked, and loosening the assertion to a warning means nobody reads
    it. Requiring a reason keeps the test strict while letting a documented
    exception through, and `test_known_zero_entries_are_documented` below
    fails if an exemption is ever added without one.
    """
    manifest = require_manifest()
    config = load_config(enabled_only=True)
    term_hits = manifest.get("term_hits", {})

    dead = []
    exempt = []

    for area, group, term in iter_terms(config):
        hits = term_hits.get(area, {}).get(group, {}).get(term)
        if hits != 0:
            continue
        if f"{group}/{term}" in (config[area].get("known_zero") or {}):
            exempt.append(f"{area}/{group}/{term}")
        else:
            dead.append(f"{area}/{group}/{term}")

    if exempt:
        print(f"  {len(exempt)} documented zero-hit term(s): {', '.join(exempt)}")

    assert not dead, (
        f"{len(dead)} term(s) returned zero trials:\n  "
        + "\n  ".join(dead)
        + "\n\nEach is a typo, a renamed or discontinued compound, or a term "
        "the registry does not index.\nFix it, remove it, or -- if the zero is "
        "legitimate -- add it to that area's `known_zero` map in\n"
        "ingest/query_config.json with a reason. Leaving it undocumented means "
        "the config claims coverage the corpus does not have."
    )


def test_known_zero_entries_are_documented():
    """
    An exemption without a reason is just a silenced test. Each `known_zero`
    key must name a term that actually exists in the config, and carry a
    non-trivial explanation.
    """
    config = load_config()
    problems = []

    for area, spec in config.items():
        configured = {
            f"{group}/{term}"
            for a, group, term in iter_terms({area: spec})
            if a == area
        }
        for key, reason in (spec.get("known_zero") or {}).items():
            if key not in configured:
                problems.append(
                    f"{area}: known_zero key {key!r} matches no configured term "
                    "-- stale exemption, or a typo in the key"
                )
            if not isinstance(reason, str) or len(reason.strip()) < 40:
                problems.append(
                    f"{area}: known_zero[{key!r}] needs a real explanation, "
                    f"got {reason!r}"
                )

    assert not problems, "known_zero problems:\n  " + "\n  ".join(problems)


def test_every_area_has_trials():
    manifest = require_manifest()
    config = load_config(enabled_only=True)
    per_area: dict[str, int] = {area: 0 for area in config}
    for areas in manifest.get("trials", {}).values():
        for area in areas:
            if area in per_area:
                per_area[area] += 1

    empty = [area for area, count in per_area.items() if count == 0]
    assert not empty, (
        f"Area(s) with no ingested trials: {', '.join(empty)}. "
        "The dashboard would offer an area filter that returns nothing."
    )


# --------------------------------------------------------------------------
# 3. live registry check -- opt-in
# --------------------------------------------------------------------------

def _live_enabled() -> bool:
    return os.environ.get("TRIALLENS_LIVE") == "1" or "--live" in sys.argv


def test_every_term_exists_in_the_live_registry():
    """
    Count-only calls, one per term. Same guardrail v1 applied to
    model-proposed drug names, pointed at our own config instead.

    v1 validated what Claude invented but took its own config on trust.
    The config is hand-written, which makes it exactly as capable of
    containing a plausible-looking compound that does not exist.
    """
    if not _live_enabled():
        return

    from common.ctgov_client import CTGovClient

    client = CTGovClient()
    config = load_config()
    dead = []
    checked = 0

    for area, group, term in iter_terms(config):
        kwargs = (
            {"intervention": term} if group == "intervention" else {"condition": term}
        )
        try:
            count = client.count(**kwargs)
        except RuntimeError as exc:
            print(f"  {area}/{group}/{term}: request failed — {exc}")
            continue

        checked += 1
        if count == 0:
            dead.append(f"{area}/{group}/{term}")
        print(f"  {area}/{group}/{term:<45} {count:>6}")

    print(f"\n  checked {checked} term(s) against the live registry")
    assert not dead, (
        f"{len(dead)} term(s) return zero hits in the registry right now:\n  "
        + "\n  ".join(dead)
    )


# --------------------------------------------------------------------------
# standalone runner
# --------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_config_parses_and_has_required_fields,
        test_no_duplicate_terms_within_an_area,
        test_terms_are_clean_strings,
        test_lexicons_are_well_formed,
        test_lexicon_terms_are_not_also_queried,
        test_manifest_exists,
        test_every_configured_term_was_queried,
        test_no_dead_terms,
        test_known_zero_entries_are_documented,
        test_every_area_has_trials,
        test_every_term_exists_in_the_live_registry,
    ]

    config = load_config()
    total_terms = sum(1 for _ in iter_terms(config))
    print(f"Config:   {QUERY_CONFIG_PATH}")
    print(f"Areas:    {', '.join(config)}")
    print(f"Terms:    {total_terms}")
    print(f"Manifest: {RAW_MANIFEST_PATH} "
          f"({'present' if RAW_MANIFEST_PATH.exists() else 'ABSENT'})")
    print(f"Live:     {'ON' if _live_enabled() else 'off (pass --live to enable)'}")
    print()

    failed = 0
    skipped = 0
    passed = 0

    for test in tests:
        name = test.__name__
        if name == "test_every_term_exists_in_the_live_registry" and not _live_enabled():
            skipped += 1
            print(f"SKIP  {name}  (pass --live to enable)")
            continue
        try:
            test()
        except SkipTest as exc:
            skipped += 1
            print(f"SKIP  {name}")
            print(f"      {exc}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}")
            print(f"      {exc}")
        else:
            passed += 1
            print(f"PASS  {name}")

    print()
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
