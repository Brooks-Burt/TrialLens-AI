"""
normalize/programs.py

Asset resolution: cluster trials into PROGRAMS, where a program is one
(sponsor, asset) pair.

WHY THIS IS THE HARDEST PART OF v2
==================================

CT.gov has trials. A competitive landscape needs programs. Five caspofungin
trials run by Merck are ONE competitor, not five, and without this layer
every headline number the V2 proposal asks for -- "14 companies, 8
therapeutic programs", "most crowded indication" -- is uncomputable.

v1 listed asset-level normalization as an explicit non-goal. This is the
piece that changes.

The difficulty is entirely in the strings. The corpus contains 2,899
distinct intervention names across ~3,000 trials, and one molecule appears
as many of them:

    "Itraconazole"  "itraconazole"  "Itraconazole 200 mg"
    "Itraconazole capsule"  "Sporanox"  "SUBA-itraconazole"  "R51211"

    "AmBisome"  "liposomal amphotericin B"  "L-AmB"
    "Amphotericin B liposome"  "AmBisome®"

Three failure modes to design against, in order of how badly they hurt:

1. OVER-MERGING is the dangerous one. Collapsing two distinct assets
   understates how crowded a landscape is, and "this space has 6 programs,
   not 11" is precisely the conclusion a user would act on. So resolution
   is conservative: no fuzzy string distance, no edit-distance matching, no
   model judgment. An unmatched name becomes its own single-trial program
   flagged `unresolved`, and the unresolved rate is reported as a number.
   Under-merging is visible and countable; over-merging is silent.

2. PLACEBO ARMS. 114 distinct placebo-like strings appear across 319
   instances in this corpus. Without the non_assets list, "Placebo" would
   resolve to one of the largest programs in the landscape.

3. NON-ANTIFUNGAL CO-MEDICATIONS. Rifampin, midazolam and carbamazepine are
   in the top 30 interventions because azoles are CYP3A4 probes in
   interaction studies. They are real interventions in real trials, but they
   are not antifungal assets, and the allowlist is what keeps them out --
   see the note on stem matching below.

WHY AN ALLOWLIST, NOT STEM MATCHING
===================================
Measured, not assumed. Matching on the stem "azole" -- which looks obviously
correct -- pulled omeprazole, esomeprazole, rabeprazole, lansoprazole,
pantoprazole, metronidazole, tinidazole, sulfamethoxazole, letrozole and
anastrozole into the antifungal asset list. Two of those were in the top 30
"antifungal" interventions by count. There is no shared suffix across
fluconazole, clotrimazole and bifonazole that excludes omeprazole, so the
allowlist in normalize/asset_aliases.json is not a shortcut here; it is the
only correct approach.

LONGEST MATCH WINS
==================
Load-bearing, because amphotericin B formulations are deliberately separate
assets. "liposomal amphotericin B" must resolve to the liposomal program,
not to conventional amphotericin B -- they differ in toxicity, route and
commercial position, and merging them would put a 1960s generic and an
active development program in one row. Matching the longest asset name
first is what makes that work.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import ASSET_ALIASES_PATH, DB_PATH  # noqa: E402

# Does an unmatched string even LOOK like an antifungal? This separates a
# real dictionary gap from a co-medication that correctly matched nothing.
#
# Without the split, the report said "56.8% unresolved", which reads as a
# broken resolver. It is not: 3,719 of those strings are azacitidine,
# venetoclax, tacrolimus, vancomycin and oxycodone -- AML conditioning
# regimens, transplant immunosuppression, antibacterials and DDI probes,
# all of which SHOULD match no antifungal asset. Only 102 strings (91
# distinct) look antifungal and failed, and that is the only number that is
# a worklist.
#
# Enumerating every non-antifungal drug in the exclusions list is not
# possible -- the corpus has 2,590 distinct such strings and the real world
# has no bound. So "not in the dictionary" is the correct outcome for a
# co-medication, and the metric has to say so.
LOOKS_ANTIFUNGAL_RE = re.compile(
    r"conazol|fungin|amphoteric|fungal|fungus|candid|aspergill|terbinafin"
    r"|butenafin|naftifin|griseofulvin|flucytosin|nystatin|natamycin"
    r"|ciclopirox|tavaborol|amorolfin|tolnaftat|nikkomycin|olorofim"
    r"|manogepix|ibrexafunger|azole",
    re.I,
)

# Dose and unit strings. The unit group does NOT require a trailing word
# boundary: "%" is not a word character, so \b after it never matches and
# "Ketoconazole 2%" kept a stray "2" in an earlier version of this regex.
DOSE_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*"
    r"(?:mg/kg|mg/m2|mg/ml|mcg|mg|µg|ug|g|ml|l|iu|u|%|percent|units?|doses?)",
    re.I,
)

# Formulation and route words. Plurals are explicit -- "tablets" and
# "capsules" both appear, and matching only the singular left
# "terbinafine tablets" and "itraconazole capsules" as separate assets.
FORM_RE = re.compile(
    r"\b(?:tablets?|capsules?|creams?|gels?|solutions?|ointments?|lacquers?"
    r"|shampoos?|suspensions?|injections?|infusions?|powders?|sprays?|lotions?"
    r"|films?|troches?|lozenges?|pessar(?:y|ies)|suppositor(?:y|ies)|granules"
    r"|sachets?|syrups?|drops?|pastes?|foams?|patches?|implants?|inserts?"
    r"|iv|i\.v\.|oral|orally|topical|topically|intravenous|intravenously"
    r"|inhaled|inhalation|nebulized|nebulised|vaginal|buccal|nail|ophthalmic"
    r"|systemic|extended|immediate|release|delayed|modified|coated|micronized"
    r"|liquid|dry|sterile|preservative|free|arm|group|cohort|dose|dosing"
    r"|high|low|standard|conventional)\b",
    re.I,
)

# Salt and ester forms. The salt is not a different asset.
SALT_RE = re.compile(
    r"\b(?:acetate|nitrate|hydrochlorides?|hcl|hydrate|citrate|sodium|potassium"
    r"|phosphate|isethionate|deoxycholate|besylate|mesylate|maleate|tartrate"
    r"|succinate|sulfate|sulphate|fumarate|lactate|gluconate|olamine|cyclodextrin"
    r"|sbecd|hydroxypropyl|betadex|disodium|monohydrate|dihydrate)\b",
    re.I,
)

PAREN_RE = re.compile(r"\([^)]*\)")
TRADEMARK_RE = re.compile(r"[®™©]")

SPONSOR_SUFFIX_RE = re.compile(
    r"\b(?:inc|inc\.|llc|l\.l\.c\.|corp|corp\.|corporation|company|co|co\."
    r"|ltd|ltd\.|limited|gmbh|ag|a\.g\.|sa|s\.a\.|s\.a\.s|sas|bv|b\.v\.|nv|n\.v\."
    r"|plc|a/s|ab|as|oy|kg|kgaa|spa|s\.p\.a\.|srl|s\.r\.l\.|pty|pte|kk|k\.k\."
    r"|holdings?|group|international|worldwide|global|usa|us|america|"
    r"r&d|research|development)\b",
    re.I,
)


def normalize_asset(name: str) -> str:
    """
    Reduce an intervention string to a comparable form.

    Order matters. Parentheticals go first because they frequently contain a
    dose or a brand name that would otherwise survive as noise. Salt forms
    go before formulation words because "caspofungin acetate for injection"
    needs both stripped and the salt pattern is the more specific one.
    """
    s = name.lower()
    s = TRADEMARK_RE.sub("", s)
    s = PAREN_RE.sub(" ", s)
    s = DOSE_RE.sub(" ", s)
    s = SALT_RE.sub(" ", s)
    s = FORM_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9+\- ]", " ", s)
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s+", " ", s).strip(" -")
    return s


def normalize_sponsor(name: str) -> str:
    """
    Strip legal-entity noise so 'Merck Sharp & Dohme LLC' and 'Merck Sharp &
    Dohme Corp.' are one company.

    Deliberately does NOT merge subsidiaries onto parents. 'Astellas Pharma
    Global Development' and 'Astellas Pharma Inc' stay distinct unless an
    explicit sponsor_aliases entry says otherwise, because deciding that two
    differently-named legal entities are commercially one sponsor is a
    judgment, and guessing it from string similarity is exactly the
    over-merging this module is built to avoid. The run summary reports the
    top sponsors so the real cases can be added by hand.
    """
    s = name.lower().replace("&", " and ")
    s = TRADEMARK_RE.sub("", s)
    s = PAREN_RE.sub(" ", s)
    s = re.sub(r"[.,]", " ", s)
    s = SPONSOR_SUFFIX_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9\- ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -")
    return s


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class AssetResolver:
    """
    Deterministic intervention-name -> asset resolution.

    Outcomes are distinguishable on purpose:

        ('asset',      key)   resolved to a dictionary asset
        ('non_asset',  label) placebo / control / vehicle arm
        ('excluded',   name)  a listed look-alike, e.g. omeprazole
        ('unresolved', norm)  nothing matched

    'excluded' and 'unresolved' both mean "no program", but they mean
    different things when reviewing the unresolved rate: one is a name the
    dictionary deliberately rejects, the other is a gap in the dictionary.
    Collapsing them would hide the second behind the first.
    """

    def __init__(self, spec: dict):
        self.assets: dict[str, dict] = spec["assets"]
        self.exclusions = {normalize_asset(n) for n in spec["exclusions"]["names"]}
        self.non_assets = {normalize_asset(n) for n in spec["non_assets"]["patterns"]}
        self.sponsor_aliases = {
            k: v for k, v in (spec.get("sponsor_aliases") or {}).items()
            if not k.startswith("_")
        }

        # name -> asset key, longest name first. Sorting by descending length
        # is what makes "liposomal amphotericin b" win over "amphotericin b";
        # without it the liposomal program silently merges into conventional.
        lookup: dict[str, str] = {}
        for key, meta in self.assets.items():
            for name in [key, meta["canonical"], *meta.get("aliases", [])]:
                norm = normalize_asset(name)
                if norm:
                    lookup.setdefault(norm, key)
        self.lookup = lookup
        self.ordered = sorted(lookup, key=len, reverse=True)

    def resolve(self, raw_name: str) -> tuple[str, str]:
        norm = normalize_asset(raw_name)
        if not norm:
            return ("non_asset", "empty")

        # Non-assets are matched on the whole string or its leading token,
        # never as a substring: "placebo-controlled voriconazole" is a
        # voriconazole arm, and substring matching would discard it.
        if norm in self.non_assets:
            return ("non_asset", norm)
        head = norm.split(" ")[0].split("-")[0]
        if head in self.non_assets and len(norm.split()) <= 2:
            return ("non_asset", norm)

        if norm in self.lookup:
            return ("asset", self.lookup[norm])
        if norm in self.exclusions:
            return ("excluded", norm)

        # Containment, longest first. Word-boundary anchored so "azole" can
        # never match inside "omeprazole".
        for candidate in self.ordered:
            if re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", norm):
                return ("asset", self.lookup[candidate])

        for excluded in self.exclusions:
            if re.search(rf"(?<![a-z0-9]){re.escape(excluded)}(?![a-z0-9])", norm):
                return ("excluded", norm)

        return ("unresolved", norm)

    def match_method(self, raw_name: str, asset_key: str) -> str:
        norm = normalize_asset(raw_name)
        if norm == normalize_asset(asset_key) or norm == normalize_asset(
            self.assets[asset_key]["canonical"]
        ):
            return "exact_normalized"
        aliases = {normalize_asset(a) for a in self.assets[asset_key].get("aliases", [])}
        if norm in aliases:
            return "alias_dictionary"
        return "alias_dictionary"


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

def build_programs(conn: sqlite3.Connection, resolver: AssetResolver,
                   dry_run: bool = False) -> dict:
    """
    Resolve every trial's interventions and write programs + program_trials.

    Reads trials.interventions (the parsed display column) rather than
    raw_json, because that column is already the list of intervention NAMES
    and nothing here needs the nested record. That is also why parse_study's
    output is a documented interface rather than an implementation detail.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = {
        "trials": 0, "trials_with_program": 0, "interventions": 0,
        "asset": 0, "non_asset": 0, "excluded": 0, "unresolved": 0,
        "gap": 0, "not_an_asset": 0,
    }
    unresolved_names: dict[str, int] = {}
    programs: dict[str, dict] = {}
    links: list[tuple[str, str, str]] = []

    rows = conn.execute(
        "SELECT nct_id, sponsor, interventions FROM trials ORDER BY nct_id"
    ).fetchall()

    for nct_id, sponsor, interventions_json in rows:
        stats["trials"] += 1
        sponsor = sponsor or "(sponsor not reported)"
        sponsor_norm = normalize_sponsor(sponsor) or "unknown"
        sponsor_norm = resolver.sponsor_aliases.get(sponsor_norm, sponsor_norm)
        matched_any = False

        for raw_name in json.loads(interventions_json or "[]"):
            if not raw_name:
                continue
            stats["interventions"] += 1
            kind, value = resolver.resolve(raw_name)
            stats[kind] += 1

            if kind == "unresolved":
                if LOOKS_ANTIFUNGAL_RE.search(raw_name):
                    stats["gap"] += 1
                    unresolved_names[raw_name.strip()] = (
                        unresolved_names.get(raw_name.strip(), 0) + 1)
                else:
                    stats["not_an_asset"] += 1
                continue
            if kind != "asset":
                continue

            meta = resolver.assets[value]
            program_id = f"{slugify(sponsor_norm)}__{slugify(value)}"
            programs.setdefault(program_id, {
                "program_id": program_id,
                "asset_name": meta["canonical"],
                "asset_norm": normalize_asset(value),
                "sponsor": sponsor,
                "sponsor_norm": sponsor_norm,
                "drug_class": meta.get("class"),
                "match_method": resolver.match_method(raw_name, value),
            })
            links.append((program_id, nct_id, raw_name))
            matched_any = True

        if matched_any:
            stats["trials_with_program"] += 1

    stats["programs"] = len(programs)
    stats["links"] = len(links)
    stats["unresolved_distinct"] = len(unresolved_names)
    stats["unresolved_names"] = unresolved_names

    if dry_run:
        return stats

    for program in programs.values():
        conn.execute(
            """
            INSERT INTO programs (program_id, asset_name, asset_norm, sponsor,
                                  sponsor_norm, drug_class, match_method,
                                  created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(program_id) DO UPDATE SET
                asset_name   = excluded.asset_name,
                drug_class   = excluded.drug_class,
                match_method = excluded.match_method,
                updated_at   = excluded.updated_at
            """,
            (program["program_id"], program["asset_name"], program["asset_norm"],
             program["sponsor"], program["sponsor_norm"], program["drug_class"],
             program["match_method"], now, now),
        )

    for program_id, nct_id, matched_on in links:
        conn.execute(
            """
            INSERT OR IGNORE INTO program_trials (program_id, nct_id, matched_on)
            VALUES (?, ?, ?)
            """,
            (program_id, nct_id, matched_on),
        )

    conn.commit()
    return stats


def report(conn: sqlite3.Connection, stats: dict, show_unresolved: int) -> None:
    total = stats["interventions"] or 1
    print("=== Asset resolution ===")
    print(f"Trials read:            {stats['trials']}")
    print(f"Intervention strings:   {stats['interventions']}")
    print(f"  resolved to an asset: {stats['asset']:>5}  ({100*stats['asset']/total:.1f}%)")
    print(f"  placebo / control:    {stats['non_asset']:>5}  ({100*stats['non_asset']/total:.1f}%)")
    print(f"  excluded look-alikes: {stats['excluded']:>5}  ({100*stats['excluded']/total:.1f}%)")
    print(f"  not an antifungal:    {stats['not_an_asset']:>5}  "
          f"({100*stats['not_an_asset']/total:.1f}%)  co-medications, correct to skip")
    print(f"  DICTIONARY GAP:       {stats['gap']:>5}  "
          f"({100*stats['gap']/total:.1f}%)  [{stats['unresolved_distinct']} distinct]"
          f"  <- looks antifungal, did not resolve")
    print()
    print(f"Programs created:       {stats['programs']}")
    print(f"Program-trial links:    {stats['links']}")
    print(f"Trials in >=1 program:  {stats['trials_with_program']} of {stats['trials']}"
          f"  ({100*stats['trials_with_program']/max(stats['trials'],1):.0f}%)")

    names = stats["unresolved_names"]
    if names and show_unresolved:
        print()
        print(f"--- top {min(show_unresolved, len(names))} dictionary gaps ---")
        print("    (strings that look antifungal but matched no asset -- the real worklist)")
        for name, count in sorted(names.items(), key=lambda kv: -kv[1])[:show_unresolved]:
            print(f"  {count:>4}  {name[:70]}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--aliases", type=Path, default=ASSET_ALIASES_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and report without writing programs.")
    parser.add_argument("--show-unresolved", type=int, default=30)
    args = parser.parse_args()

    if not args.db_path.exists():
        print(f"No database at {args.db_path}. Run normalize/build_db.py first.")
        sys.exit(1)

    spec = json.loads(args.aliases.read_text())
    resolver = AssetResolver(spec)
    print(f"Assets in dictionary:   {len(resolver.assets)}")
    print(f"Lookup keys (incl. aliases): {len(resolver.lookup)}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'WRITE'}")
    print()

    conn = sqlite3.connect(args.db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    stats = build_programs(conn, resolver, args.dry_run)
    report(conn, stats, args.show_unresolved)
    conn.close()
