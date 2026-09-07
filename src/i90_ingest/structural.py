from __future__ import annotations

import io
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import pdfplumber

TZ = ZoneInfo("Europe/Madrid")

ESIOS_STRUCTURAL_URLS = {
    "programming_units": "https://www.esios.ree.es/es/unidades-de-programacion",
    "physical_units": "https://www.esios.ree.es/es/unidades-fisicas",
    "market_subjects": "https://www.esios.ree.es/es/sujetos-del-mercado",
}

OMIE_UNITS_URL = "https://www.grupoomi.eu/sites/default/files/dados/listados/LISTA_UNIDADES.PDF"


@dataclass(frozen=True)
class GroupRule:
    pattern: str
    group_name: str
    confidence: str
    source_url: str


GROUP_RULES = [
    GroupRule(
        r"\bAXPO\b",
        "Axpo",
        "verified_corporate",
        "https://www.axpo.com/es/es/about-us/axpo-iberia.html",
    ),
    GroupRule(
        r"\bNATURGY\b|\bGAS NATURAL COMERCIALIZADORA\b",
        "Naturgy",
        "verified_corporate",
        "https://www.naturgy.com/app/uploads/2025/02/CCAA-Individuales-y-Consolidadas-NATURGY-2024.pdf",
    ),
    GroupRule(r"\bENDESA\b", "Endesa", "derived_from_legal_name", "https://www.endesa.com/"),
    GroupRule(r"\bIBERDROLA\b", "Iberdrola", "derived_from_legal_name", "https://www.iberdrola.com/"),
    GroupRule(r"\bREPSOL\b", "Repsol", "derived_from_legal_name", "https://www.repsol.com/"),
    GroupRule(r"\bEDP\b", "EDP", "derived_from_legal_name", "https://www.edp.com/"),
    GroupRule(r"\bACCIONA\b", "Acciona", "derived_from_legal_name", "https://www.acciona.com/"),
    GroupRule(r"\bTOTALENERGIES\b", "TotalEnergies", "derived_from_legal_name", "https://totalenergies.com/"),
    GroupRule(r"\bPETROGAL\b|\bGALP\b", "Galp", "derived_from_legal_name", "https://www.galp.com/"),
    GroupRule(r"\bENGIE\b", "ENGIE", "derived_from_legal_name", "https://www.engie.com/"),
    GroupRule(r"\bMOEVE\b|\bCEPSA\b", "Moeve", "derived_from_legal_name", "https://www.moeveglobal.com/"),
]


def _clean(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", " ").strip()
    if text.lower() == "nan":
        return ""
    return re.sub(r"\s+", " ", text)


def _norm(value: object) -> str:
    text = _clean(value).lower()
    text = (
        text.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ü", "u")
        .replace("ñ", "n")
    )
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def assign_group(legal_entity: str) -> tuple[str, str, str]:
    upper = _clean(legal_entity).upper()
    for rule in GROUP_RULES:
        if re.search(rule.pattern, upper):
            return rule.group_name, rule.confidence, rule.source_url
    return "", "", ""


def fetch_bytes(
    url: str,
    api_key: str | None = None,
    timeout: float = 90.0,
) -> tuple[bytes, dict]:
    headers = {
        "User-Agent": "JoyaAlbert/i90 structural mapping",
        "Accept": "application/pdf,application/json,text/html,*/*",
    }
    if api_key:
        headers["x-api-key"] = api_key

    with httpx.Client(
        headers=headers,
        follow_redirects=True,
        timeout=timeout,
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content, {
            "url": str(response.url),
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "bytes": len(response.content),
        }


# ---------------------------------------------------------------------------
# OMIE: exact operational unit -> owner -> technology
# ---------------------------------------------------------------------------

OMIE_X_COLUMNS = [
    ("up_code", 100.0, 143.0),
    ("up_name_omie", 143.0, 247.0),
    ("legal_entity", 247.0, 431.0),
    ("ownership_pct", 431.0, 490.0),
    ("unit_type_omie", 490.0, 572.0),
    ("zone_omie", 572.0, 644.0),
    ("technology_omie", 644.0, 760.0),
]


def _parse_omie_pdf(pdf_blob: bytes) -> pd.DataFrame:
    """
    Parse OMIE's fixed-layout official PDF using word coordinates.

    Table extraction was intentionally avoided: merged PDF cells can shift
    AGENTE/TECNOLOGIA columns. Coordinates are stable in the official layout
    and are validated on every row.
    """
    records: list[dict[str, object]] = []

    with pdfplumber.open(io.BytesIO(pdf_blob)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(
                use_text_flow=False,
                keep_blank_chars=False,
            )

            # Rows have a shared top coordinate. A 0.8pt tolerance captures
            # the fixed-layout line without merging consecutive records.
            groups: list[tuple[float, list[dict]]] = []
            for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
                if word["top"] < 135:
                    continue
                if not groups or abs(groups[-1][0] - word["top"]) > 0.8:
                    groups.append((word["top"], [word]))
                else:
                    groups[-1][1].append(word)

            for top, row_words in groups:
                values: dict[str, object] = {
                    "omie_page": page_no,
                    "omie_y": round(float(top), 3),
                }

                for name, x0, x1 in OMIE_X_COLUMNS:
                    values[name] = _clean(
                        " ".join(
                            word["text"]
                            for word in row_words
                            if x0 <= word["x0"] < x1
                        )
                    )

                code = str(values["up_code"]).replace(" ", "")
                if not re.fullmatch(r"[A-Z0-9]{2,14}", code):
                    continue

                values["up_code"] = code

                if not values["legal_entity"]:
                    raise RuntimeError(
                        f"OMIE row {code} page {page_no} has empty legal entity"
                    )
                if not values["ownership_pct"]:
                    raise RuntimeError(
                        f"OMIE row {code} page {page_no} has empty ownership percentage"
                    )
                if not values["unit_type_omie"]:
                    raise RuntimeError(
                        f"OMIE row {code} page {page_no} has empty unit type"
                    )

                records.append(values)

    if not records:
        raise RuntimeError("OMIE PDF parsed but no unit rows were detected")

    frame = pd.DataFrame(records)

    # Keep multiple owner rows: shared plants such as Almaraz/Trillo/Vandellós
    # must not be collapsed into a single company.
    frame = frame.drop_duplicates(
        subset=[
            "up_code",
            "legal_entity",
            "ownership_pct",
            "up_name_omie",
        ]
    )

    frame["ownership_pct_numeric"] = pd.to_numeric(
        frame["ownership_pct"].astype(str).str.replace(",", ".", regex=False),
        errors="coerce",
    )

    return frame


# ---------------------------------------------------------------------------
# eSIOS SPA lists: render in Chromium, then extract the DataTable
# ---------------------------------------------------------------------------

def _render_esios_datatable(url: str) -> tuple[pd.DataFrame, dict]:
    """
    eSIOS structural pages are SPA shells: the initial HTML has no <table>.
    Render the page with Chromium and collect the DataTable page by page.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=120_000,
        )

        # Give application JS and the first DataTable request time to settle.
        page.wait_for_timeout(5_000)
        page.wait_for_selector("table", timeout=60_000)

        table_count = page.locator("table").count()
        if table_count < 1:
            raise RuntimeError(f"No rendered table found at {url}")

        # Pick the rendered table with the largest current body.
        best_index = 0
        best_rows = -1
        for idx in range(table_count):
            count = page.locator("table").nth(idx).locator("tbody tr").count()
            if count > best_rows:
                best_index = idx
                best_rows = count

        table = page.locator("table").nth(best_index)
        headers = [
            _clean(x)
            for x in table.locator("thead th").all_inner_texts()
        ]

        if not headers:
            raise RuntimeError(f"Rendered eSIOS table at {url} has no headers")

        # Locate the DataTables wrapper that belongs to the chosen table.
        table_id = table.get_attribute("id")
        if table_id:
            table_js = f"document.getElementById({json.dumps(table_id)})"
        else:
            table_js = f"document.querySelectorAll('table')[{best_index}]"

        has_dt = page.evaluate(
            f"""() => {{
                const el = {table_js};
                return !!(
                    window.jQuery &&
                    jQuery.fn &&
                    jQuery.fn.dataTable &&
                    jQuery.fn.dataTable.isDataTable(el)
                );
            }}"""
        )

        rows: list[list[str]] = []

        if has_dt:
            # Increase page size to reduce requests, then iterate all pages.
            page.evaluate(
                f"""() => {{
                    const dt = jQuery({table_js}).DataTable();
                    dt.page.len(500).draw();
                }}"""
            )
            page.wait_for_timeout(2_000)

            info = page.evaluate(
                f"""() => jQuery({table_js}).DataTable().page.info()"""
            )
            pages = max(1, int(info.get("pages", 1)))

            for idx in range(pages):
                page.evaluate(
                    f"""() => {{
                        const dt = jQuery({table_js}).DataTable();
                        dt.page({idx}).draw('page');
                    }}"""
                )
                page.wait_for_timeout(700)

                chunk = table.locator("tbody tr").evaluate_all(
                    """rows => rows.map(
                        tr => Array.from(tr.querySelectorAll('td'))
                            .map(td => td.innerText.trim())
                    )"""
                )
                rows.extend(chunk)
        else:
            rows = table.locator("tbody tr").evaluate_all(
                """rows => rows.map(
                    tr => Array.from(tr.querySelectorAll('td'))
                        .map(td => td.innerText.trim())
                )"""
            )

        final_url = page.url
        browser.close()

    # Normalize width: DataTables sometimes adds an action/details column.
    clean_rows = []
    for row in rows:
        row = [_clean(x) for x in row]
        if not any(row):
            continue
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        if len(row) > len(headers):
            row = row[: len(headers)]
        clean_rows.append(row)

    frame = pd.DataFrame(clean_rows, columns=[_norm(h) for h in headers])
    frame = frame.drop_duplicates()

    return frame, {
        "url": final_url,
        "status": response.status if response else None,
        "rendered_table_index": best_index,
        "rows": int(len(frame)),
        "columns": list(frame.columns),
    }


ALIASES = {
    "up_code": [
        "unidad_de_programacion",
        "codigo_unidad_de_programacion",
        "codigo_up",
        "cod_up",
        "up",
        "codigo",
    ],
    "up_name": [
        "descripcion",
        "denominacion",
        "nombre",
        "nombre_unidad_de_programacion",
    ],
    "uf_code": [
        "unidad_fisica",
        "codigo_unidad_fisica",
        "codigo_uf",
        "cod_uf",
        "uf",
        "codigo",
    ],
    "uf_name": [
        "descripcion",
        "denominacion",
        "nombre",
        "nombre_unidad_fisica",
    ],
    "subject_code": [
        "codigo_sujeto",
        "sujeto",
        "participante",
        "codigo_participante",
        "codigo_agente",
    ],
    "legal_entity": [
        "razon_social",
        "nombre_sujeto",
        "denominacion",
        "nombre",
        "sujeto_del_mercado",
    ],
    "technology": [
        "tecnologia",
        "tipo_produccion",
        "tipo_de_produccion",
    ],
}


def _pick_column(
    df: pd.DataFrame,
    aliases: list[str],
    forbid: tuple[str, ...] = (),
) -> str | None:
    cols = list(df.columns)

    for alias in aliases:
        if alias in cols and not any(token in alias for token in forbid):
            return alias

    for col in cols:
        if any(alias in col for alias in aliases) and not any(
            token in col for token in forbid
        ):
            return col

    return None


def canonicalize_esios(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    if kind == "programming_units":
        mapping = {
            "up_code": _pick_column(df, ALIASES["up_code"], ("fisic",)),
            "up_name": _pick_column(df, ALIASES["up_name"]),
            "subject_code": _pick_column(df, ALIASES["subject_code"]),
            "technology_esios": _pick_column(df, ALIASES["technology"]),
        }
    elif kind == "physical_units":
        mapping = {
            "uf_code": _pick_column(df, ALIASES["uf_code"]),
            "uf_name": _pick_column(df, ALIASES["uf_name"]),
            "up_code": _pick_column(df, ALIASES["up_code"], ("fisic",)),
            "subject_code": _pick_column(df, ALIASES["subject_code"]),
            "technology_esios": _pick_column(df, ALIASES["technology"]),
        }
    else:
        mapping = {
            "subject_code": _pick_column(df, ALIASES["subject_code"]),
            "legal_entity": _pick_column(df, ALIASES["legal_entity"]),
        }

    out = pd.DataFrame(index=df.index)
    for target, source in mapping.items():
        out[target] = df[source].map(_clean) if source else ""

    return out.loc[~(out == "").all(axis=1)].drop_duplicates()


def _aggregate_physical_units(physical: pd.DataFrame) -> pd.DataFrame:
    columns = ["up_code", "uf_count", "uf_codes", "uf_names"]
    if physical.empty or "up_code" not in physical.columns:
        return pd.DataFrame(columns=columns)

    p = physical.copy()
    p = p[p["up_code"].astype(str).str.len() > 0]
    if p.empty:
        return pd.DataFrame(columns=columns)

    return (
        p.groupby("up_code", as_index=False)
        .agg(
            uf_count=("uf_code", lambda s: len(set(_clean(x) for x in s if _clean(x)))),
            uf_codes=("uf_code", lambda s: "|".join(sorted(set(_clean(x) for x in s if _clean(x))))),
            uf_names=("uf_name", lambda s: "|".join(sorted(set(_clean(x) for x in s if _clean(x))))),
        )
    )


def build_master(
    programming: pd.DataFrame,
    physical: pd.DataFrame,
    subjects: pd.DataFrame,
    omie: pd.DataFrame,
) -> pd.DataFrame:
    master = omie.copy()

    if not programming.empty and "up_code" in programming.columns:
        master = programming.merge(
            master,
            on="up_code",
            how="outer",
        )

    if (
        not subjects.empty
        and "subject_code" in master.columns
        and "subject_code" in subjects.columns
    ):
        subject_rows = subjects.drop_duplicates(subset=["subject_code"])
        master = master.merge(
            subject_rows,
            on="subject_code",
            how="left",
            suffixes=("", "_subject"),
        )
        if "legal_entity_subject" in master.columns:
            master["legal_entity"] = master["legal_entity"].where(
                master["legal_entity"].fillna("").astype(str).str.len() > 0,
                master["legal_entity_subject"],
            )

    physical_agg = _aggregate_physical_units(physical)
    if not physical_agg.empty:
        master = master.merge(
            physical_agg,
            on="up_code",
            how="left",
        )

    for col in [
        "up_name",
        "up_name_omie",
        "legal_entity",
        "technology_esios",
        "technology_omie",
        "unit_type_omie",
        "zone_omie",
        "ownership_pct",
        "ownership_pct_numeric",
        "uf_count",
        "uf_codes",
        "uf_names",
        "subject_code",
    ]:
        if col not in master.columns:
            master[col] = ""

    groups = master["legal_entity"].fillna("").map(assign_group)
    master[
        ["group_name", "group_confidence", "group_source_url"]
    ] = pd.DataFrame(groups.tolist(), index=master.index)

    master["technology"] = master["technology_esios"].where(
        master["technology_esios"].fillna("").astype(str).str.len() > 0,
        master["technology_omie"],
    )
    master["up_name_final"] = master["up_name"].where(
        master["up_name"].fillna("").astype(str).str.len() > 0,
        master["up_name_omie"],
    )

    master["mapping_source"] = "OMIE exact unit code"
    if not programming.empty:
        esios_codes = set(programming["up_code"].astype(str))
        master.loc[
            master["up_code"].astype(str).isin(esios_codes),
            "mapping_source",
        ] = "eSIOS UP + OMIE exact code"

    if not physical_agg.empty:
        uf_codes = set(physical_agg["up_code"].astype(str))
        master.loc[
            master["up_code"].astype(str).isin(uf_codes),
            "mapping_source",
        ] += " + eSIOS UF"

    owner_counts = (
        master.groupby("up_code")["legal_entity"]
        .transform(lambda s: s.replace("", pd.NA).dropna().nunique())
    )
    master["owner_count"] = owner_counts.fillna(0).astype(int)

    ordered = [
        "up_code",
        "up_name_final",
        "legal_entity",
        "group_name",
        "group_confidence",
        "ownership_pct",
        "ownership_pct_numeric",
        "owner_count",
        "technology",
        "unit_type_omie",
        "zone_omie",
        "uf_count",
        "uf_codes",
        "uf_names",
        "subject_code",
        "mapping_source",
        "group_source_url",
    ]

    return master[ordered].drop_duplicates()


def run_structural(
    public_root: Path = Path("public"),
    work_root: Path = Path("data/work/structural"),
) -> dict:
    now = datetime.now(TZ)
    day = now.date().isoformat()

    work_root.mkdir(parents=True, exist_ok=True)
    latest = public_root / "structural" / "latest"
    history = public_root / "structural" / "history" / day
    latest.mkdir(parents=True, exist_ok=True)
    history.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "generated_at": now.isoformat(),
        "sources": {},
        "warnings": [],
    }
    esios_frames: dict[str, pd.DataFrame] = {}

    for kind, url in ESIOS_STRUCTURAL_URLS.items():
        try:
            raw, meta = _render_esios_datatable(url)
            canonical = canonicalize_esios(raw, kind)
            esios_frames[kind] = canonical

            meta["canonical_rows"] = int(len(canonical))
            meta["canonical_columns"] = list(canonical.columns)
            manifest["sources"][kind] = meta

            for dest in (latest, history):
                raw.to_csv(dest / f"{kind}_raw.csv", index=False)
                canonical.to_csv(dest / f"{kind}.csv", index=False)
        except Exception as exc:
            manifest["sources"][kind] = {
                "url": url,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            manifest["warnings"].append(
                f"{kind}: {type(exc).__name__}: {exc}"
            )
            esios_frames[kind] = pd.DataFrame()

    omie_blob, omie_meta = fetch_bytes(OMIE_UNITS_URL)
    (work_root / "LISTA_UNIDADES.PDF").write_bytes(omie_blob)
    omie = _parse_omie_pdf(omie_blob)

    omie_meta["rows"] = int(len(omie))
    omie_meta["unique_unit_codes"] = int(omie["up_code"].nunique())
    omie_meta["rows_with_legal_entity"] = int(
        omie["legal_entity"].astype(str).str.len().gt(0).sum()
    )
    omie_meta["rows_with_technology"] = int(
        omie["technology_omie"].astype(str).str.len().gt(0).sum()
    )
    manifest["sources"]["omie_units"] = omie_meta

    for dest in (latest, history):
        omie.to_csv(dest / "omie_units.csv", index=False)

    master = build_master(
        esios_frames["programming_units"],
        esios_frames["physical_units"],
        esios_frames["market_subjects"],
        omie,
    )

    manifest["master"] = {
        "rows": int(len(master)),
        "unique_up_codes": int(master["up_code"].nunique()),
        "mapped_legal_entity": int(
            master["legal_entity"].fillna("").astype(str).str.len().gt(0).sum()
        ),
        "mapped_group": int(
            master["group_name"].fillna("").astype(str).str.len().gt(0).sum()
        ),
        "mapped_uf": int(
            pd.to_numeric(master["uf_count"], errors="coerce").fillna(0).gt(0).sum()
        ),
        "shared_ownership_up_codes": int(
            master.loc[master["owner_count"] > 1, "up_code"].nunique()
        ),
    }

    # Quality gates: OMIE mapping is mandatory and must be nearly complete.
    if manifest["sources"]["omie_units"]["rows_with_legal_entity"] < 4000:
        raise RuntimeError("OMIE legal-entity mapping unexpectedly incomplete")
    if manifest["sources"]["omie_units"]["unique_unit_codes"] < 4000:
        raise RuntimeError("OMIE unit-code mapping unexpectedly incomplete")

    for dest in (latest, history):
        master.to_csv(dest / "up_master.csv", index=False)
        (dest / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest
