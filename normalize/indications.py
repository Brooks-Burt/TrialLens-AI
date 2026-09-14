"""
normalize/indications.py

Collapse CT.gov condition strings onto a controlled vocabulary, and classify
everything else honestly.

WHY THIS EXISTS
===============
The corpus holds 2,196 distinct condition strings across 5,184 mentions --
a mean of 2.4 mentions each, so the tail is most of it. Four of those
strings describe one market:

    "Vulvovaginal Candidiasis"          32
    "Candidiasis, Vulvovaginal"         19
    "Recurrent Vulvovaginal Candidiasis" 13
    "Vaginal Candidiasis"               10

Counting distinct programs per raw string would split every indication
across its spellings and report a landscape as LESS crowded than it is.
That is the dangerous direction: "this space has room" is a conclusion
someone acts on.

FOUR OUTCOMES, NOT TWO
======================
Classifying a condition as indication-or-other throws away information the
wide-net ingest specifically collected. The most frequent conditions in this
corpus are Healthy (177), HIV Infections (144), Acute Myeloid Leukemia (44)
and Prostate Cancer (33) -- none a fungal indication, none equivalent to
each other:

    indication    a fungal disease. The landscape's y-axis.
    population    the SETTING a trial ran in. Healthy volunteers, AML,
                  transplant, ICU. Real and useful, but not a market.
    study_label   describes the STUDY, not a disease. "Pharmacokinetics"
                  appears 33 times as a condition value; treating it as an
                  indication would invent a market.
    off_topic     verifiably not fungal. Ketoconazole in prostate cancer
                  (anti-androgen at high dose), amphotericin B in visceral
                  leishmaniasis. Residue the wide net accepted on purpose.

Everything else is `unmapped`, reported with its count and never silently
dropped. That count is the maintenance signal: if it grows after a
re-ingest, the vocabulary has drifted behind the corpus. Same reasoning as
the dictionary-gap metric in normalize/programs.py.

UMBRELLA TERMS ARE EXCLUDED FROM CROWDING
=========================================
"Fungal Infection" (68), "Mycoses" (45), "Invasive Fungal Infections" (38)
and their variants are registry shorthand covering many markets at once --
too many mentions to discard, but not attributable to a specific market
without inventing the attribution. They are tagged `umbrella` and kept out
of crowding denominators, so "most crowded indication" never reports a
shorthand as the most contested space.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import ASSET_ALIASES_PATH, DB_PATH, INDICATION_VOCAB_PATH  # noqa: E402
from normalize.materiality import dig  # noqa: E402

KIND_INDICATION = "indication"
KIND_POPULATION = "population"
KIND_STUDY_LABEL = "study_label"
KIND_OFF_TOPIC = "off_topic"
KIND_UNMAPPED = "unmapped"


def deinvert(text: str) -> str:
    """
    Un-invert a MeSH descriptor: "Candidiasis, Oral" -> "oral candidiasis".

    CT.gov carries MeSH terms in inverted form, and this one mechanical
    transform multiplies the coverage of every alias in the vocabulary --
    "Meningitis, Cryptococcal", "Pneumonia, Pneumocystis Carinii" and
    "Candidiasis, Vulvovaginal" all resolve without needing their own
    entries.

    Only applied to single-comma strings. Two commas usually means a list
    of separate conditions rather than one inverted descriptor, and
    reordering a list produces a phrase that never existed.
    """
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2 or not all(parts):
        return text
    return f"{parts[1]} {parts[0]}"


def simplify(text: str) -> str:
    """Lowercase, drop punctuation and filler, collapse whitespace."""
    s = text.lower().strip()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9\s'/-]", " ", s)
    s = re.sub(r"\b(?:the|a|an|of|in|with|and|or|due to|caused by|related)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


class IndicationVocabulary:
    def __init__(self, spec: dict):
        self.spec = spec
        self.indications: dict[str, dict] = spec["indications"]

        # Longest alias first, so "invasive candidiasis" wins over
        # "candidiasis" and "invasive pulmonary aspergillosis" over
        # "aspergillosis". Without it every specific indication collapses
        # into its own umbrella and the crowding axis loses all resolution.
        self.lookup: dict[str, tuple[str, str]] = {}
        for key, meta in self.indications.items():
            for name in [key, meta["canonical"], *meta.get("aliases", [])]:
                self.lookup.setdefault(simplify(name), (KIND_INDICATION, key))

        for key, meta in spec["populations"].items():
            if key.startswith("_"):
                continue
            for name in [key, meta["canonical"], *meta.get("aliases", [])]:
                self.lookup.setdefault(simplify(name), (KIND_POPULATION, key))

        for pattern in spec["study_labels"]["patterns"]:
            self.lookup.setdefault(simplify(pattern), (KIND_STUDY_LABEL, pattern))
        for pattern in spec["off_topic"]["patterns"]:
            self.lookup.setdefault(simplify(pattern), (KIND_OFF_TOPIC, pattern))

        # Drug names appearing as CONDITION values. Sponsors do this:
        # "Voriconazole" is a condition on 5 trials and "Fluconazole" on 4.
        # The trial is about the drug's pharmacology, not a disease of that
        # name, so these are study_label -- and they are read from
        # asset_aliases.json rather than re-listed here, so one dictionary
        # stays the single source of drug names.
        if ASSET_ALIASES_PATH.exists():
            assets = json.loads(ASSET_ALIASES_PATH.read_text()).get("assets") or {}
            for key, meta in assets.items():
                for name in [key, meta["canonical"], *meta.get("aliases", [])]:
                    self.lookup.setdefault(simplify(name), (KIND_STUDY_LABEL, "drug name as condition"))

        self.ordered = sorted(self.lookup, key=len, reverse=True)

    def classify(self, raw: str) -> dict:
        """
        Returns {kind, key, canonical, parent, specificity, matched_on}.

        Exact match on the simplified string is tried before containment.
        Containment alone would let "aspergillosis" inside "allergic
        bronchopulmonary aspergillosis" win if alias order ever shifted;
        exact-first makes the frequent cases order-independent.
        """
        for candidate in (simplify(raw), simplify(deinvert(raw))):
            if candidate in self.lookup:
                return self._describe(*self.lookup[candidate], candidate)

        for candidate in (simplify(raw), simplify(deinvert(raw))):
            for name in self.ordered:
                if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", candidate):
                    return self._describe(*self.lookup[name], name)

        return {"kind": KIND_UNMAPPED, "key": None, "canonical": raw.strip(),
                "parent": None, "specificity": None, "matched_on": None}

    def _describe(self, kind: str, key: str, matched_on: str) -> dict:
        if kind == KIND_INDICATION:
            meta = self.indications[key]
            return {"kind": kind, "key": key, "canonical": meta["canonical"],
                    "parent": meta.get("parent"),
                    "specificity": meta.get("specificity", "specific"),
                    "matched_on": matched_on}
        canonical = key
        if kind == KIND_POPULATION:
            canonical = self.spec["populations"][key]["canonical"]
        return {"kind": kind, "key": key, "canonical": canonical,
                "parent": None, "specificity": None, "matched_on": matched_on}

    def ancestors(self, key: str) -> list[str]:
        """Roll-up chain: candidemia -> invasive candidiasis -> candidiasis."""
        chain, seen = [], set()
        current = self.indications.get(key, {}).get("parent")
        while current and current not in seen:
            seen.add(current)
            chain.append(current)
            current = self.indications.get(current, {}).get("parent")
        return chain


def load_vocabulary(path: Path | None = None) -> IndicationVocabulary:
    return IndicationVocabulary(
        json.loads((path or INDICATION_VOCAB_PATH).read_text())
    )


def build_trial_indications(conn: sqlite3.Connection,
                            vocab: IndicationVocabulary,
                            dry_run: bool = False) -> dict:
    """Classify every trial's conditions and write trial_indications."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = {k: 0 for k in (KIND_INDICATION, KIND_POPULATION, KIND_STUDY_LABEL,
                            KIND_OFF_TOPIC, KIND_UNMAPPED)}
    stats.update({"trials": 0, "trials_with_indication": 0, "mentions": 0, "rows": 0})
    unmapped: dict[str, int] = {}
    rows: list[tuple] = []

    for nct_id, raw_json in conn.execute(
        "SELECT nct_id, raw_json FROM trials ORDER BY nct_id"
    ):
        stats["trials"] += 1
        protocol = json.loads(raw_json).get("protocolSection", {}) or {}
        conditions = dig(protocol, "conditionsModule", "conditions", default=[]) or []

        # Two passes, direct matches before roll-ups. Order matters for a
        # semantic reason, not a performance one: with one pass, a trial
        # carrying both "Candidemia" and "Candidiasis" would record
        # candidiasis at whichever depth its condition happened to appear
        # first in the registry record. That makes rollup_depth = 0 an
        # unreliable filter for "directly stated", which is the one thing
        # analyze/ needs it for -- crowding must count trials that NAME an
        # indication, not trials that inherited it from a child.
        direct: list[tuple] = []
        inherited: list[tuple] = []
        has_indication = False

        for raw in conditions:
            if not raw or not raw.strip():
                continue
            stats["mentions"] += 1
            result = vocab.classify(raw)
            stats[result["kind"]] += 1

            if result["kind"] == KIND_UNMAPPED:
                unmapped[raw.strip()] = unmapped.get(raw.strip(), 0) + 1
                continue
            if result["kind"] == KIND_INDICATION:
                has_indication = True

            direct.append((result["kind"], result["key"], result["canonical"],
                           result["specificity"], 0, raw.strip(),
                           result["matched_on"]))

            if result["kind"] == KIND_INDICATION:
                for depth, parent in enumerate(vocab.ancestors(result["key"]), 1):
                    meta = vocab.indications.get(parent, {})
                    inherited.append((KIND_INDICATION, parent,
                                      meta.get("canonical", parent),
                                      meta.get("specificity"), depth,
                                      raw.strip(), result["matched_on"]))

        seen: set[tuple[str, str]] = set()
        for kind, key, canonical, specificity, depth, matched_from, matched_on in (
            direct + sorted(inherited, key=lambda r: r[4])
        ):
            dedupe = (kind, key)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            rows.append((nct_id, kind, key, canonical, specificity, depth,
                         matched_from, matched_on, now))

        if has_indication:
            stats["trials_with_indication"] += 1

    stats["rows"] = len(rows)
    stats["unmapped_distinct"] = len(unmapped)
    stats["unmapped_names"] = unmapped

    if dry_run:
        return stats

    conn.execute("DELETE FROM trial_indications")
    conn.executemany(
        """INSERT OR IGNORE INTO trial_indications
             (nct_id, kind, key, canonical, specificity, rollup_depth,
              matched_from, matched_on, assessed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--vocab", type=Path, default=INDICATION_VOCAB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show-unmapped", type=int, default=25)
    args = parser.parse_args()

    vocab = load_vocabulary(args.vocab)
    conn = sqlite3.connect(args.db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    stats = build_trial_indications(conn, vocab, args.dry_run)

    mentions = stats["mentions"] or 1
    print(f"Vocabulary: {len(vocab.indications)} indications, "
          f"{len(vocab.lookup)} lookup keys")
    print(f"Mode:       {'DRY RUN' if args.dry_run else 'WRITE'}")
    print()
    print("=== Condition classification ===")
    print(f"Trials:          {stats['trials']}")
    print(f"Condition strings: {stats['mentions']}")
    for kind in (KIND_INDICATION, KIND_POPULATION, KIND_STUDY_LABEL,
                 KIND_OFF_TOPIC, KIND_UNMAPPED):
        print(f"  {kind:<13}{stats[kind]:>6}  ({100*stats[kind]/mentions:>5.1f}%)")
    print(f"    unmapped distinct: {stats['unmapped_distinct']}")
    print()
    print(f"Trials with >=1 fungal indication: {stats['trials_with_indication']}"
          f" of {stats['trials']}"
          f"  ({100*stats['trials_with_indication']/max(stats['trials'],1):.0f}%)")
    print(f"Rows written (incl. roll-ups):     {stats['rows']}")

    names = stats["unmapped_names"]
    if names and args.show_unmapped:
        print()
        print(f"--- top {min(args.show_unmapped, len(names))} unmapped ---")
        print("    (maintenance signal: growth here means the vocabulary is behind)")
        for name, count in sorted(names.items(), key=lambda kv: -kv[1])[: args.show_unmapped]:
            print(f"  {count:>4}  {name[:66]}")
    conn.close()
