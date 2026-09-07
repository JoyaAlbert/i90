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
    assert "captured_xhr_api" in source
    assert "api_endpoint" in source
    assert "api_pagination_strategy" in source

def test_flatten_record_supports_nested_api_payload():
    from i90_ingest.structural import _flatten_record

    row = _flatten_record(
        {
            "code": "UP001",
            "market_subject": {"code": "SUBJ", "name": "Example"},
            "production_type": {"name": "Solar"},
        }
    )

    assert row["code"] == "UP001"
    assert row["market_subject_code"] == "SUBJ"
    assert row["market_subject_name"] == "Example"
    assert row["production_type_name"] == "Solar"


def test_select_network_dataset_matches_visible_code():
    from i90_ingest.structural import _select_network_dataset

    events = [
        {
            "url": "https://example.invalid/api/programming-units?page=0&size=25",
            "method": "GET",
            "post_data": None,
            "headers": {},
            "status": 200,
            "payload": {
                "content": [
                    {
                        "code": "UP001",
                        "short_description": "TEST UNIT",
                        "production_type": "Solar",
                    }
                ],
                "totalElements": 1200,
            },
        }
    ]

    selected = _select_network_dataset(
        events,
        "programming_units",
        [["UP001", "TEST UNIT"]],
    )

    assert selected is not None
    assert selected["record_path"] == ("content",)
    assert selected["total_hint"] == 1200


def test_query_variant_updates_known_pagination():
    from i90_ingest.structural import _query_variant

    url, changed = _query_variant(
        "https://example.invalid/api/items?page=0&size=25",
        page_index=3,
        page_size=500,
    )

    assert changed is True
    assert "page=3" in url
    assert "size=500" in url

def test_archive_download_json_is_complete_snapshot():
    from i90_ingest.structural import _collect_all_network_records

    class DummyPage:
        pass

    records = [{"codigo_de_up": f"UP{i:04d}"} for i in range(1200)]
    candidate = {
        "url": "https://api.esios.ree.es/archives/82/download_json?locale=es",
        "method": "GET",
        "post_data": None,
        "headers": {},
        "records": records,
        "record_path": ("UnidadesProgramacion",),
        "total_hint": None,
        "next_url": None,
    }

    result, meta = _collect_all_network_records(
        DummyPage(),
        candidate,
        "programming_units",
        1000,
    )

    assert len(result) == 1200
    assert meta["api_pagination_strategy"] == "esios_archive_download_json_complete"
    assert meta["api_archive_snapshot"] is True


def test_physical_unit_uses_explicit_up_and_subject_links():
    import pandas as pd
    from i90_ingest.structural import canonicalize_esios

    raw = pd.DataFrame(
        [
            {
                "codigo_de_uf": "UF001",
                "descripcion_corta": "Unidad física 1",
                "tipo_de_produccion": "Solar fotovoltaica",
                "vinculacion_con_up": "UP777",
                "vinculacion_con_sm": "SUBJ9",
            }
        ]
    )

    out = canonicalize_esios(raw, "physical_units")
    row = out.iloc[0]

    assert row["uf_code"] == "UF001"
    assert row["up_code"] == "UP777"
    assert row["subject_code"] == "SUBJ9"


def test_programming_unit_uses_market_subject_field():
    import pandas as pd
    from i90_ingest.structural import canonicalize_esios

    raw = pd.DataFrame(
        [
            {
                "codigo_de_up": "UP001",
                "descripcion_corta": "Unidad programación 1",
                "tipo_de_produccion": "Eólica",
                "sujeto_del_mercado": "SUBJ1",
            }
        ]
    )

    out = canonicalize_esios(raw, "programming_units")
    row = out.iloc[0]

    assert row["up_code"] == "UP001"
    assert row["subject_code"] == "SUBJ1"

