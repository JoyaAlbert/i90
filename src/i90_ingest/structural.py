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

def _table_descriptors(page) -> list[dict]:
    """Describe every DOM table without forcing hidden tables to become visible."""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('table')).map((table, index) => {
            const rect = table.getBoundingClientRect();
            const style = window.getComputedStyle(table);
            const visible =
                style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                Number(style.opacity || '1') !== 0 &&
                rect.width > 0 &&
                rect.height > 0;

            const headers = Array.from(table.querySelectorAll('thead th'))
                .map(th => (th.innerText || th.textContent || '').trim())
                .filter(Boolean);

            const rows = table.querySelectorAll('tbody tr').length;

            return {
                index,
                id: table.id || '',
                className: table.className || '',
                visible,
                rows,
                headers,
                headerText: headers.join(' ').toLowerCase(),
                width: rect.width,
                height: rect.height,
            };
        })"""
    )


def _semantic_table_score(desc: dict, kind: str) -> tuple[int, int, int]:
    header = _norm(desc.get("headerText", ""))
    headers = [_norm(x) for x in desc.get("headers", [])]

    hints = {
        "programming_units": (
            "program",
            "unidad",
            "codigo",
            "descripcion",
            "sujeto",
            "tipo",
        ),
        "physical_units": (
            "fisic",
            "unidad",
            "codigo",
            "program",
            "tecnolog",
            "tipo",
        ),
        "market_subjects": (
            "sujeto",
            "mercado",
            "codigo",
            "nombre",
            "denomin",
            "particip",
        ),
    }[kind]

    semantic = sum(1 for hint in hints if hint in header)

    # Penalise common layout/navigation tables even if they happen to be visible.
    negative = (
        "cookie",
        "menu",
        "footer",
        "calend",
        "legend",
        "leyenda",
    )
    semantic -= sum(2 for token in negative if token in header)

    return (
        semantic,
        int(desc.get("rows", 0)),
        len(headers),
    )


def _render_esios_datatable(
    url: str,
    kind: str,
) -> tuple[pd.DataFrame, dict]:
    """
    Render an eSIOS structural SPA and extract its actual visible data table.

    eSIOS currently mounts many hidden <table> elements. Playwright's plain
    wait_for_selector("table") waits on the first matching element and can
    therefore time out even though the page has already rendered the useful
    table. We wait for *any visible table with body rows*, inspect all DOM
    tables, then choose the visible candidate whose headers best match the
    requested dataset.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1800, "height": 1200},
            locale="es-ES",
        )

        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=120_000,
        )

        # Wait for the SPA itself, not for the first hidden table.
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll('table')).some(table => {
                const rect = table.getBoundingClientRect();
                const style = window.getComputedStyle(table);
                const visible =
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    Number(style.opacity || '1') !== 0 &&
                    rect.width > 0 &&
                    rect.height > 0;
                return visible && table.querySelectorAll('tbody tr').length > 0;
            })""",
            timeout=90_000,
        )

        # Let the first asynchronous DataTables draw settle.
        page.wait_for_timeout(2_000)

        descriptors = _table_descriptors(page)
        candidates = [
            desc
            for desc in descriptors
            if desc["visible"]
            and desc["rows"] > 0
            and len(desc["headers"]) > 0
        ]

        if not candidates:
            browser.close()
            raise RuntimeError(
                f"No visible populated table found at {url}; "
                f"DOM tables={len(descriptors)}"
            )

        candidates.sort(
            key=lambda desc: _semantic_table_score(desc, kind),
            reverse=True,
        )
        best = candidates[0]
        score = _semantic_table_score(best, kind)

        # At least one semantic hint must match. This prevents accidentally
        # accepting a visible navigation/calendar table.
        if score[0] < 1:
            diagnostics = [
                {
                    "index": d["index"],
                    "rows": d["rows"],
                    "headers": d["headers"],
                    "score": _semantic_table_score(d, kind),
                }
                for d in candidates[:8]
            ]
            browser.close()
            raise RuntimeError(
                f"No semantically compatible eSIOS table for {kind}. "
                f"Candidates={json.dumps(diagnostics, ensure_ascii=False)}"
            )

        best_index = int(best["index"])
        table = page.locator("table").nth(best_index)
        headers = [_clean(x) for x in best["headers"]]

        has_dt = page.evaluate(
            """index => {
                const el = document.querySelectorAll('table')[index];
                return !!(
                    el &&
                    window.jQuery &&
                    jQuery.fn &&
                    jQuery.fn.dataTable &&
                    jQuery.fn.dataTable.isDataTable(el)
                );
            }""",
            best_index,
        )

        rows: list[list[str]] = []
        datatable_pages = 1
        datatable_server_side = False

        if has_dt:
            dt_meta = page.evaluate(
                """index => {
                    const el = document.querySelectorAll('table')[index];
                    const dt = jQuery(el).DataTable();
                    const settings = dt.settings()[0];
                    const info = dt.page.info();
                    return {
                        serverSide: !!(settings && settings.oFeatures && settings.oFeatures.bServerSide),
                        pages: Math.max(1, info.pages || 1),
                        recordsTotal: info.recordsTotal || null,
                        pageLength: info.length || null,
                    };
                }""",
                best_index,
            )
            datatable_server_side = bool(dt_meta.get("serverSide"))

            # Ask for a large page. Works for client-side DataTables and most
            # server-side eSIOS tables; if the backend caps it, we paginate.
            page.evaluate(
                """index => {
                    const el = document.querySelectorAll('table')[index];
                    jQuery(el).DataTable().page.len(500).draw();
                }""",
                best_index,
            )
            page.wait_for_timeout(1_500)

            dt_meta = page.evaluate(
                """index => {
                    const el = document.querySelectorAll('table')[index];
                    const info = jQuery(el).DataTable().page.info();
                    return {
                        pages: Math.max(1, info.pages || 1),
                        recordsTotal: info.recordsTotal || null,
                        pageLength: info.length || null,
                    };
                }""",
                best_index,
            )
            datatable_pages = int(dt_meta.get("pages") or 1)

            # Safety guard against accidental infinite/huge pagination.
            if datatable_pages > 500:
                browser.close()
                raise RuntimeError(
                    f"Unexpected DataTables page count for {kind}: "
                    f"{datatable_pages}"
                )

            for page_index in range(datatable_pages):
                if page_index:
                    page.evaluate(
                        """args => {
                            const el = document.querySelectorAll('table')[args.index];
                            jQuery(el).DataTable().page(args.page).draw('page');
                        }""",
                        {"index": best_index, "page": page_index},
                    )
                    page.wait_for_timeout(600)

                chunk = table.locator("tbody tr").evaluate_all(
                    """rows => rows.map(
                        tr => Array.from(tr.querySelectorAll('td'))
                            .map(td => (td.innerText || td.textContent || '').trim())
                    )"""
                )
                rows.extend(chunk)
        else:
            rows = table.locator("tbody tr").evaluate_all(
                """rows => rows.map(
                    tr => Array.from(tr.querySelectorAll('td'))
                        .map(td => (td.innerText || td.textContent || '').trim())
                )"""
            )

        final_url = page.url
        browser.close()

    clean_rows: list[list[str]] = []
    for row in rows:
        row = [_clean(x) for x in row]
        if not any(row):
            continue
        if len(row) < len(headers):
            row += [""] * (len(headers) - len(row))
        elif len(row) > len(headers):
            row = row[: len(headers)]
        clean_rows.append(row)

    if not clean_rows:
        raise RuntimeError(f"Selected eSIOS table for {kind} yielded zero rows")

    frame = pd.DataFrame(
        clean_rows,
        columns=[_norm(h) or f"col_{i}" for i, h in enumerate(headers)],
    ).drop_duplicates()

    return frame, {
        "url": final_url,
        "status": response.status if response else None,
        "rendered_table_index": best_index,
        "rendered_table_id": best.get("id", ""),
        "semantic_score": score[0],
        "datatable": bool(has_dt),
        "datatable_server_side": datatable_server_side,
        "datatable_pages": datatable_pages,
        "rows": int(len(frame)),
        "columns": list(frame.columns),
        "visible_table_candidates": len(candidates),
        "dom_table_count": len(descriptors),
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
            raw, meta = _render_esios_datatable(url, kind)
            canonical = canonicalize_esios(raw, kind)

            required_key = {
                "programming_units": "up_code",
                "physical_units": "uf_code",
                "market_subjects": "legal_entity",
            }[kind]

            usable_rows = int(
                canonical[required_key]
                .fillna("")
                .astype(str)
                .str.len()
                .gt(0)
                .sum()
            ) if required_key in canonical.columns else 0

            if usable_rows == 0:
                raise RuntimeError(
                    f"eSIOS {kind} rendered but canonical key "
                    f"{required_key!r} has zero usable rows. "
                    f"Raw columns={list(raw.columns)}"
                )

            esios_frames[kind] = canonical

            meta["canonical_rows"] = int(len(canonical))
            meta["canonical_usable_key_rows"] = usable_rows
            meta["canonical_key"] = required_key
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
