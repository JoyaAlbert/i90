import pandas as pd

from i90_ingest.structural import assign_group, build_master


def test_group_axpo():
    group, confidence, source = assign_group("AXPO IBERIA, S.L.")
    assert group == "Axpo"
    assert confidence == "verified_corporate"
    assert "axpo.com" in source


def test_group_naturgy_legacy_legal_name():
    group, confidence, source = assign_group(
        "GAS NATURAL COMERCIALIZADORA"
    )
    assert group == "Naturgy"
    assert confidence == "verified_corporate"
    assert "naturgy.com" in source


def test_master_exact_code_and_physical_units():
    programming = pd.DataFrame(
        [
            {
                "up_code": "ABO2G",
                "up_name": "ABOÑO 2 GAS",
                "subject_code": "",
                "technology_esios": "",
            }
        ]
    )
    physical = pd.DataFrame(
        [
            {
                "uf_code": "UF001",
                "uf_name": "ABOÑO 2",
                "up_code": "ABO2G",
                "subject_code": "",
                "technology_esios": "Gas",
            }
        ]
    )
    subjects = pd.DataFrame(columns=["subject_code", "legal_entity"])
    omie = pd.DataFrame(
        [
            {
                "up_code": "ABO2G",
                "up_name_omie": "ABOÑO 2 GAS",
                "legal_entity": "ABOÑO GENERACIONES ELECTRICAS SLU",
                "ownership_pct": "100",
                "unit_type_omie": "GENERACION",
                "zone_omie": "ZONA ESPAÑOLA",
                "technology_omie": "Gas",
                "omie_page": "1",
            }
        ]
    )

    master = build_master(programming, physical, subjects, omie)
    row = master.iloc[0]

    assert row["up_code"] == "ABO2G"
    assert row["legal_entity"] == "ABOÑO GENERACIONES ELECTRICAS SLU"
    assert row["uf_count"] == 1
    assert row["uf_codes"] == "UF001"
    assert "eSIOS UF" in row["mapping_source"]
