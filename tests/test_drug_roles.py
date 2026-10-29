"""
tests/test_drug_roles.py

Offline tests for pipeline.retrieve.drug_role -- the rule that decides
whether a searched drug is what a trial tests, a comparator, background
therapy, or not in the drug list at all.

Each case is a cut-down copy of a real CT.gov record (cited by NCT ID,
checked live on 2026-10-29), keeping only the fields the rule reads. No
network, no data files.

Run:
    python tests/test_drug_roles.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.retrieve import (  # noqa: E402
    BOTH_ARMS, COMPARATOR_ONLY, NOT_IN_DRUG_LIST, OTHER_NAME, ROLE_UNCLEAR, TESTED,
    _parse, drug_role, is_head_to_head,
)


def study(arms: dict[str, str], drugs: list[tuple], mesh: list[str] = (), title: str = "") -> dict:
    """arms: label -> type. drugs: (name, [arm labels], [other names]?, intervention type?)."""
    return {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT00000000", "briefTitle": title},
            "armsInterventionsModule": {
                "armGroups": [{"label": l, "type": t} for l, t in arms.items()],
                "interventions": [
                    {"name": d[0], "armGroupLabels": d[1], "otherNames": d[2] if len(d) > 2 else [],
                     "type": d[3] if len(d) > 3 else "DRUG"}
                    for d in drugs
                ],
            },
        },
        "derivedSection": {"interventionBrowseModule": {"meshes": [{"term": m} for m in mesh]}},
    }


# NCT03667690: rezafungin vs caspofungin with oral fluconazole step-down.
REZAFUNGIN = study(
    {"Rezafungin": "EXPERIMENTAL", "Caspofungin": "ACTIVE_COMPARATOR"},
    [("Rezafungin for Injection", ["Rezafungin"]), ("Caspofungin", ["Caspofungin"]),
     ("Fluconazole", ["Caspofungin"]), ("oral placebo", ["Rezafungin"])],
    mesh=["Rezafungin", "Caspofungin", "Fluconazole"],
)
# NCT05178862: SCY-078 (ibrexafungerp) + echinocandin vs echinocandin -> fluconazole.
IBREXAFUNGERP = study(
    {"SCY-078": "EXPERIMENTAL", "Standard of care": "ACTIVE_COMPARATOR"},
    [("SCY-078", ["SCY-078"], ["ibrexafungerp"]), ("Fluconazole", ["Standard of care"]),
     ("Echinocandin", ["SCY-078", "Standard of care"])],
)
# NCT03363841: lists only "SCY-078", no other names; MeSH says ibrexafungerp.
CODE_NAME_ONLY = study({"SCY-078": "EXPERIMENTAL"}, [("SCY-078", ["SCY-078"])], mesh=["ibrexafungerp"])
# NCT07731256: drug-vs-placebo, drug arm mislabelled ACTIVE_COMPARATOR.
MISLABELLED = study(
    {"Semaglutide Group": "ACTIVE_COMPARATOR", "Placebo Group": "PLACEBO_COMPARATOR"},
    [("Semaglutide (administered by PDS290 pen-injector)", ["Semaglutide Group"]),
     ("Placebo (administered by PDS290 pen-injector)", ["Placebo Group"])],
)
# NCT07021937: semaglutide in every arm; adolescents (EXPERIMENTAL) vs adults (ACTIVE_COMPARATOR).
POPULATION_COMPARISON = study(
    {"Adolescent": "EXPERIMENTAL", "Adolescent - cessation": "EXPERIMENTAL",
     "Adult": "ACTIVE_COMPARATOR", "Adult - cessation": "ACTIVE_COMPARATOR"},
    [("Semaglutide 1.7mg subcutaneous", ["Adolescent", "Adolescent - cessation", "Adult", "Adult - cessation"]),
     ("Placebo", ["Adolescent - cessation", "Adult - cessation"])],
)
# NCT00509834: hLF1-11 + fluconazole; fluconazole appears only in descriptions.
DESCRIPTION_ONLY = study(
    {"hLF1-11": "EXPERIMENTAL", "Placebo": "PLACEBO_COMPARATOR"},
    [("hLF1-11", ["hLF1-11"]), ("Placebo", ["Placebo"])],
    mesh=["lactoferrin (1-11), human"],
)
# Synthetic: drug listed, but the record has no arm groups.
NO_ARMS = study({}, [("Fluconazole", [])])
# NCT07622992: empty drug list, drug named only in the title.
TITLE_ONLY = study({}, [], title="Preoperative Optimization With Tirzepatide, Ketogenic Diet...")
# NCT07900074: tirzepatide vs semaglutide, both arms EXPERIMENTAL.
HEAD_TO_HEAD = study(
    {"Semaglutide": "EXPERIMENTAL", "Tirzepatide": "EXPERIMENTAL"},
    [("Tirzepatide", ["Tirzepatide"]), ("Semaglutide", ["Semaglutide"])],
)
# NCT07253285: CagriSema vs semaglutide vs cagrilintide vs placebo, with a
# matching "Placebo semaglutide" that must not count as semaglutide.
CAGRISEMA = study(
    {"CagriSema": "EXPERIMENTAL", "Semaglutide": "EXPERIMENTAL",
     "Cagrilintide": "EXPERIMENTAL", "Placebo": "PLACEBO_COMPARATOR"},
    [("Cagrilintide", ["CagriSema", "Cagrilintide"]), ("Semaglutide", ["CagriSema", "Semaglutide"]),
     ("Placebo cagrilintide", ["CagriSema", "Cagrilintide", "Placebo"]),
     ("Placebo semaglutide", ["CagriSema", "Placebo", "Semaglutide"])],
)
# Synthetic: drug vs a behavioural programme -- two study arms, but not a drug head-to-head.
DRUG_VS_LIFESTYLE = study(
    {"Drug": "EXPERIMENTAL", "Lifestyle": "EXPERIMENTAL"},
    [("Semaglutide", ["Drug"]), ("Lifestyle coaching", ["Lifestyle"], [], "BEHAVIORAL")],
)

CASES = [
    (REZAFUNGIN, "rezafungin", TESTED),
    (REZAFUNGIN, "fluconazole", COMPARATOR_ONLY),
    (REZAFUNGIN, "caspofungin", COMPARATOR_ONLY),
    (IBREXAFUNGERP, "ibrexafungerp", TESTED),        # via otherNames
    (IBREXAFUNGERP, "echinocandin", BOTH_ARMS),      # background: SCY-078 is only in the study arm
    (IBREXAFUNGERP, "fluconazole", COMPARATOR_ONLY),
    (CODE_NAME_ONLY, "ibrexafungerp", OTHER_NAME),
    (MISLABELLED, "semaglutide", TESTED),
    (POPULATION_COMPARISON, "semaglutide", TESTED),
    (DESCRIPTION_ONLY, "fluconazole", NOT_IN_DRUG_LIST),
    (NO_ARMS, "fluconazole", ROLE_UNCLEAR),
    (TITLE_ONLY, "tirzepatide", ROLE_UNCLEAR),
    (TITLE_ONLY, "semaglutide", NOT_IN_DRUG_LIST),
    (REZAFUNGIN, "Rezafungin", TESTED),              # case-insensitive
    (REZAFUNGIN, "rezafung", NOT_IN_DRUG_LIST),      # whole words only
    (HEAD_TO_HEAD, "semaglutide", TESTED),
    (CAGRISEMA, "semaglutide", TESTED),              # was both_arms before the placebo-name fix
]

# (record, named drug, expected is_head_to_head)
H2H_CASES = [
    (HEAD_TO_HEAD, "semaglutide", True),
    (HEAD_TO_HEAD, "tirzepatide", True),
    (CAGRISEMA, "semaglutide", True),                # cagrilintide-only arm vs semaglutide arm
    (REZAFUNGIN, "rezafungin", False),               # other drugs only in comparator arm: a rezafungin trial
    (MISLABELLED, "semaglutide", False),             # vs placebo
    (POPULATION_COMPARISON, "semaglutide", False),   # same drug in every arm
    (DRUG_VS_LIFESTYLE, "semaglutide", False),       # other arm is not a drug
]


def _check() -> list[str]:
    lines = []
    for record, term, expected in CASES:
        got = drug_role(_parse(record), term)
        lines.append((got == expected, f"role  {term:<14} expected={expected:<17} got={got}"))
    for record, term, expected in H2H_CASES:
        got = is_head_to_head(_parse(record), term)
        lines.append((got == expected, f"h2h   {term:<14} expected={expected!s:<17} got={got}"))
    return lines


def test_drug_roles():
    failures = [line for ok, line in _check() if not ok]
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    lines = _check()
    for ok, line in lines:
        print(f"{'ok  ' if ok else 'FAIL'}  {line}")
    test_drug_roles()
    print(f"\nall {len(lines)} cases pass")
