import pandas as pd

from i90_ingest.structural import assign_group, build_master


def test_group_axpo():
    group, confidence, source = assign_group("AXPO IBERIA, S.L.")
    assert group == "Axpo"
    assert confidence == "verified_corporate"


def test_group_naturgy():
    group, confidence, source = assign_group(
        "GAS NATURAL COMERCIALIZADORA"
    )
    assert group == "Naturgy"
    assert confidence == "verified_corporate"


def test_shared_ownership_is_not_collapsed():
    programming = pd.DataFrame()
    physical = pd.DataFrame()
    subjects = pd.DataFrame()
    omie = pd.DataFrame(
        [
            {
                "up_code": "ALZ1",
                "up_name_omie": "C.N. ALMARAZ 1",
                "legal_entity": "ENDESA GENERACIÓN, S.A.",
                "ownership_pct": "36,021",
                "ownership_pct_numeric": 36.021,
                "unit_type_omie": "GENERACION",
                "zone_omie": "ZONA ESPAÑOLA",
                "technology_omie": "Nuclear",
            },
            {
                "up_code": "ALZ1",
                "up_name_omie": "C.N. ALMARAZ 1",
                "legal_entity": "IBERDROLA ENERGÍA ESPAÑA S..A.",
                "ownership_pct": "52,687",
                "ownership_pct_numeric": 52.687,
                "unit_type_omie": "GENERACION",
                "zone_omie": "ZONA ESPAÑOLA",
                "technology_omie": "Nuclear",
            },
        ]
    )

    master = build_master(programming, physical, subjects, omie)
    assert len(master) == 2
    assert set(master["owner_count"]) == {2}


def test_physical_unit_join():
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
                "ownership_pct_numeric": 100.0,
                "unit_type_omie": "GENERACION",
                "zone_omie": "ZONA ESPAÑOLA",
                "technology_omie": "Gas",
            }
        ]
    )

    master = build_master(programming, physical, subjects, omie)
    row = master.iloc[0]

    assert row["legal_entity"] == "ABOÑO GENERACIONES ELECTRICAS SLU"
    assert row["uf_count"] == 1
    assert row["uf_codes"] == "UF001"

def test_semantic_table_score_prefers_physical_units():
    from i90_ingest.structural import _semantic_table_score

    physical = {
        "rows": 500,
        "headers": ["Código", "Unidad física", "Unidad de programación", "Tecnología"],
        "headerText": "Código Unidad física Unidad de programación Tecnología",
    }
    navigation = {
        "rows": 50,
        "headers": ["Fecha", "Valor"],
        "headerText": "Fecha Valor",
    }

    assert (
        _semantic_table_score(physical, "physical_units")
        > _semantic_table_score(navigation, "physical_units")
    )


def test_semantic_table_score_prefers_market_subjects():
    from i90_ingest.structural import _semantic_table_score

    subjects = {
        "rows": 200,
        "headers": ["Código sujeto", "Sujeto del mercado", "Nombre"],
        "headerText": "Código sujeto Sujeto del mercado Nombre",
    }

    assert _semantic_table_score(subjects, "market_subjects")[0] >= 2

def test_esios_completion_thresholds_are_not_first_page_only():
    from pathlib import Path
    import i90_ingest.structural as structural

    source = Path(structural.__file__).read_text(encoding="utf-8")
    assert '"programming_units": 1000' in source
    assert '"physical_units": 1000' in source
    assert '"market_subjects": 100' in source
    assert "generic_exhausted" in source

