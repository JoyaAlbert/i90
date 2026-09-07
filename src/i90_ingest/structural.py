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


def _http_headers(api_key: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": "JoyaAlbert/i90 structural mapping",
        "Accept": "text/html,application/xhtml+xml,application/pdf,application/json;q=0.9,*/*;q=0.8",
    }
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def fetch_bytes(
    url: str,
    api_key: str | None = None,
    timeout: float = 90.0,
) -> tuple[bytes, dict]:
    with httpx.Client(
        headers=_http_headers(api_key),
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


def _best_html_table(html_blob: bytes, kind: str) -> pd.DataFrame:
    try:
        tables = pd.read_html(io.BytesIO(html_blob))
    except Exception as exc:
        raise RuntimeError(f"No HTML tables parsed for {kind}: {exc}") from exc

    if not tables:
        raise RuntimeError(f"No HTML tables found for {kind}")

    hints = {
        "programming_units": ("program", "unidad", "codigo", "sujeto", "particip"),
        "physical_units": ("fisic", "unidad", "codigo", "program", "tecnolog"),
        "market_subjects": ("sujeto", "particip", "codigo", "nombre", "razon"),
    }[kind]

    def score(df: pd.DataFrame) -> tuple[int, int]:
        cols = " ".join(_norm(c) for c in df.columns)
        return sum(1 for hint in hints if hint in cols), len(df)

    table = max(tables, key=score).copy()
    table.columns = [_norm(c) or f"col_{i}" for i, c in enumerate(table.columns)]
    return table.dropna(how="all")


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


def _parse_omie_tables(pdf_blob: bytes) -> pd.DataFrame:
    records: list[dict[str, str]] = []

    settings = {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "snap_tolerance": 3,
        "join_tolerance": 3,
        "intersection_tolerance": 5,
        "text_tolerance": 2,
    }

    with pdfplumber.open(io.BytesIO(pdf_blob)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            for table in page.extract_tables(table_settings=settings) or []:
                for row in table:
                    cells = [_clean(x) for x in (row or [])]
                    if len(cells) < 6:
                        continue

                    joined = " | ".join(cells).upper()
                    if (
                        "CODIGO" in joined
                        or "CÓDIGO" in joined
                        or "LISTADO DE UNIDADES" in joined
                    ):
                        continue

                    code = cells[0].replace(" ", "")
                    if not re.fullmatch(r"[A-Z0-9]{2,14}", code):
                        continue

                    records.append(
                        {
                            "up_code": code,
                            "up_name_omie": cells[1] if len(cells) > 1 else "",
                            "legal_entity": cells[2] if len(cells) > 2 else "",
                            "ownership_pct": cells[3] if len(cells) > 3 else "",
                            "unit_type_omie": cells[4] if len(cells) > 4 else "",
                            "zone_omie": cells[5] if len(cells) > 5 else "",
                            "technology_omie": " ".join(cells[6:])
                            if len(cells) > 6
                            else "",
                            "omie_page": str(page_no),
                        }
                    )

    if not records:
        raise RuntimeError("OMIE PDF parsed but no unit rows were detected")

    return pd.DataFrame(records).drop_duplicates(
        subset=["up_code", "legal_entity", "up_name_omie"]
    )


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
            uf_count=(
                "uf_code",
                lambda s: int(
                    pd.Series([x for x in s if _clean(x)]).nunique()
                ),
            ),
            uf_codes=(
                "uf_code",
                lambda s: "|".join(
                    sorted(set(_clean(x) for x in s if _clean(x)))
                ),
            ),
            uf_names=(
                "uf_name",
                lambda s: "|".join(
                    sorted(set(_clean(x) for x in s if _clean(x)))
                ),
            ),
        )
    )


def build_master(
    programming: pd.DataFrame,
    physical: pd.DataFrame,
    subjects: pd.DataFrame,
    omie: pd.DataFrame,
) -> pd.DataFrame:
    # OMIE states that a market offer unit corresponds to a programming unit.
    # Only exact code equality is used; no prefix/name guessing is allowed.
    master = omie.copy()

    if not programming.empty and "up_code" in programming.columns:
        master = programming.merge(master, on="up_code", how="outer")

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
        master = master.merge(physical_agg, on="up_code", how="left")

    required = [
        "up_name",
        "up_name_omie",
        "legal_entity",
        "technology_esios",
        "technology_omie",
        "unit_type_omie",
        "zone_omie",
        "uf_count",
        "uf_codes",
        "uf_names",
        "subject_code",
    ]
    for col in required:
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

    ordered = [
        "up_code",
        "up_name_final",
        "legal_entity",
        "group_name",
        "group_confidence",
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
    for col in ordered:
        if col not in master.columns:
            master[col] = ""

    return master[ordered].drop_duplicates()


def run_structural(
    public_root: Path = Path("public"),
    work_root: Path = Path("data/work/structural"),
) -> dict:
    now = datetime.now(TZ)
    day = now.date().isoformat()
    api_key = os.environ.get("ESIOS_API_KEY")

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
            blob, meta = fetch_bytes(url, api_key=api_key)
            (work_root / f"{kind}.html").write_bytes(blob)
            raw_table = _best_html_table(blob, kind)
            canonical = canonicalize_esios(raw_table, kind)
            esios_frames[kind] = canonical

            meta["rows"] = int(len(canonical))
            meta["columns"] = list(canonical.columns)
            manifest["sources"][kind] = meta

            for dest in (latest, history):
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
    omie = _parse_omie_tables(omie_blob)

    omie_meta["rows"] = int(len(omie))
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
        "mapped_legal_entity": int(
            master["legal_entity"]
            .fillna("")
            .astype(str)
            .str.len()
            .gt(0)
            .sum()
        ),
        "mapped_group": int(
            master["group_name"]
            .fillna("")
            .astype(str)
            .str.len()
            .gt(0)
            .sum()
        ),
        "mapped_uf": int(
            pd.to_numeric(master["uf_count"], errors="coerce")
            .fillna(0)
            .gt(0)
            .sum()
        ),
    }

    for dest in (latest, history):
        master.to_csv(dest / "up_master.csv", index=False)
        (dest / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest
