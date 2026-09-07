from __future__ import annotations

import io
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

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



def _flatten_record(record: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in record.items():
        name = _norm(f"{prefix}_{key}" if prefix else key)
        if isinstance(value, dict):
            out.update(_flatten_record(value, name))
        elif isinstance(value, list):
            if all(not isinstance(x, (dict, list)) for x in value):
                out[name] = "|".join(_clean(x) for x in value)
            else:
                out[name] = json.dumps(value, ensure_ascii=False)
        else:
            out[name] = value
    return out


def _records_frame(records: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([_flatten_record(r) for r in records]).drop_duplicates()


def _iter_record_lists(obj: object, path: tuple[str, ...] = ()):
    if isinstance(obj, list):
        if obj and sum(isinstance(x, dict) for x in obj) >= max(1, int(len(obj) * 0.8)):
            yield path, [x for x in obj if isinstance(x, dict)]
        for item in obj[:3]:
            yield from _iter_record_lists(item, path)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from _iter_record_lists(value, path + (str(key),))


def _get_json_path(obj: object, path: tuple[str, ...]) -> object:
    current = obj
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _json_total_hint(obj: object) -> int | None:
    candidates: list[int] = []

    def walk(value: object):
        if isinstance(value, dict):
            for key, child in value.items():
                nk = _norm(key)
                if isinstance(child, (int, float)) and any(
                    token in nk
                    for token in (
                        "total",
                        "count",
                        "records_total",
                        "total_elements",
                        "total_count",
                        "number_of_elements",
                    )
                ):
                    try:
                        candidates.append(int(child))
                    except Exception:
                        pass
                walk(child)
        elif isinstance(value, list):
            for child in value[:5]:
                walk(child)

    walk(obj)
    positive = [x for x in candidates if x > 0]
    return max(positive) if positive else None


def _json_next_url(obj: object, base_url: str) -> str | None:
    found: list[str] = []

    def walk(value: object):
        if isinstance(value, dict):
            for key, child in value.items():
                nk = _norm(key)
                if isinstance(child, str) and child.strip() and any(
                    token in nk
                    for token in (
                        "next",
                        "next_url",
                        "next_page",
                        "next_page_url",
                    )
                ):
                    candidate = child.strip()
                    if candidate.startswith(("http://", "https://", "/", "?")):
                        found.append(urljoin(base_url, candidate))
                walk(child)
        elif isinstance(value, list):
            for child in value[:5]:
                walk(child)

    walk(obj)
    return found[0] if found else None


def _network_record_score(
    records: list[dict],
    kind: str,
    visible_values: list[str],
    url: str,
) -> int:
    if not records:
        return -10_000

    flattened = [_flatten_record(r) for r in records[:25]]
    key_text = " ".join(sorted({k for row in flattened for k in row}))

    hints = {
        "programming_units": (
            "codigo",
            "code",
            "program",
            "descripcion",
            "description",
            "potencia",
            "power",
            "sujeto",
            "subject",
            "produccion",
            "production",
        ),
        "physical_units": (
            "codigo",
            "code",
            "fisic",
            "physical",
            "vinculacion",
            "program",
            "produccion",
            "production",
            "potencia",
            "power",
        ),
        "market_subjects": (
            "codigo",
            "code",
            "sujeto",
            "subject",
            "nombre",
            "name",
            "eic",
            "tipo",
            "type",
        ),
    }[kind]

    score = sum(2 for hint in hints if hint in key_text)

    url_norm = _norm(url)
    url_hints = {
        "programming_units": ("program", "unit", "up"),
        "physical_units": ("physical", "fisic", "unit", "uf"),
        "market_subjects": ("subject", "sujeto", "market"),
    }[kind]
    score += sum(1 for hint in url_hints if hint in url_norm)

    sample = json.dumps(flattened[:10], ensure_ascii=False).upper()
    for value in visible_values[:4]:
        token = _clean(value)
        if len(token) >= 3 and token.upper() in sample:
            score += 8

    score += min(len(records), 100) // 10
    return score


def _sanitize_request_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = {
        "accept",
        "content-type",
        "x-requested-with",
        "referer",
        "origin",
    }
    return {
        k: v
        for k, v in headers.items()
        if k.lower() in allowed
    }


def _modify_pagination_mapping(
    mapping: dict,
    page_index: int,
    page_size: int,
) -> tuple[dict, bool]:
    size_names = {
        "limit",
        "size",
        "page_size",
        "pagesize",
        "per_page",
        "perpage",
        "length",
        "page_length",
        "page_limit",
        "elements_per_page",
    }
    page_names = {
        "page",
        "pagina",
        "page_number",
        "pagenumber",
        "page_index",
        "pageindex",
        "current_page",
    }
    offset_names = {
        "offset",
        "start",
        "from",
        "first",
        "skip",
    }

    changed = False
    result = {}

    for key, value in mapping.items():
        nk = _norm(key)

        if isinstance(value, dict):
            child, child_changed = _modify_pagination_mapping(
                value,
                page_index,
                page_size,
            )
            result[key] = child
            changed = changed or child_changed
            continue

        if nk in size_names or any(nk.endswith("_" + x) for x in size_names):
            result[key] = page_size
            changed = True
        elif nk in offset_names or any(nk.endswith("_" + x) for x in offset_names):
            result[key] = page_index * page_size
            changed = True
        elif nk in page_names or any(nk.endswith("_" + x) for x in page_names):
            try:
                original = int(value)
            except Exception:
                original = 0
            base = 0 if original == 0 else 1
            result[key] = page_index + base
            changed = True
        else:
            result[key] = value

    return result, changed


def _query_variant(
    url: str,
    page_index: int,
    page_size: int,
) -> tuple[str, bool]:
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    mapping = {k: v for k, v in pairs}
    modified, changed = _modify_pagination_mapping(
        mapping,
        page_index,
        page_size,
    )
    if not changed:
        return url, False
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(modified, doseq=True),
            parts.fragment,
        )
    ), True


def _post_variant(
    post_data: str | None,
    page_index: int,
    page_size: int,
) -> tuple[str | None, bool]:
    if not post_data:
        return post_data, False

    try:
        payload = json.loads(post_data)
    except Exception:
        payload = None

    if isinstance(payload, dict):
        modified, changed = _modify_pagination_mapping(
            payload,
            page_index,
            page_size,
        )
        return json.dumps(modified), changed

    try:
        pairs = parse_qsl(post_data, keep_blank_values=True)
        mapping = {k: v for k, v in pairs}
        modified, changed = _modify_pagination_mapping(
            mapping,
            page_index,
            page_size,
        )
        return urlencode(modified), changed
    except Exception:
        return post_data, False


def _request_json(
    page,
    request_meta: dict,
    *,
    override_url: str | None = None,
    override_post_data: str | None = None,
) -> object:
    url = override_url or request_meta["url"]
    method = request_meta["method"]
    headers = request_meta.get("headers") or {}
    kwargs: dict = {
        "method": method,
        "headers": headers,
        "timeout": 90_000,
    }
    body = (
        override_post_data
        if override_post_data is not None
        else request_meta.get("post_data")
    )
    if method.upper() != "GET" and body is not None:
        kwargs["data"] = body

    response = page.request.fetch(url, **kwargs)
    if not response.ok:
        raise RuntimeError(
            f"eSIOS API replay returned HTTP {response.status} for {url}"
        )
    return response.json()


def _select_network_dataset(
    network_events: list[dict],
    kind: str,
    visible_rows: list[list[str]],
) -> dict | None:
    visible_values = [
        _clean(value)
        for row in visible_rows[:3]
        for value in row[:4]
        if _clean(value)
    ]

    scored: list[dict] = []
    for event in network_events:
        payload = event.get("payload")
        for path, records in _iter_record_lists(payload):
            score = _network_record_score(
                records,
                kind,
                visible_values,
                event["url"],
            )
            scored.append(
                {
                    **event,
                    "record_path": path,
                    "records": records,
                    "score": score,
                    "total_hint": _json_total_hint(payload),
                    "next_url": _json_next_url(payload, event["url"]),
                }
            )

    if not scored:
        return None

    scored.sort(
        key=lambda item: (
            item["score"],
            len(item["records"]),
        ),
        reverse=True,
    )

    best = scored[0]
    if best["score"] < 8:
        return None
    return best


def _collect_all_network_records(
    page,
    candidate: dict,
    kind: str,
    minimum_rows: int,
) -> tuple[list[dict], dict]:
    records = list(candidate["records"])
    record_path = tuple(candidate["record_path"])
    total_hint = candidate.get("total_hint")

    # eSIOS /archives/<id>/download_json returns the complete JSON value of
    # that archive in one response. These structural archives do not expose
    # page/total metadata because they are snapshots, not paginated APIs.
    # Accept the captured payload as complete only when it is a download_json
    # archive and it clears the dataset-specific minimum-row quality gate.
    is_archive_download_json = "/download_json" in candidate["url"].lower()

    if (
        (total_hint and len(records) >= total_hint)
        or (
            is_archive_download_json
            and len(records) >= minimum_rows
            and not candidate.get("next_url")
        )
    ):
        return records, {
            "api_pagination_strategy": (
                "esios_archive_download_json_complete"
                if is_archive_download_json
                else "initial_payload_complete"
            ),
            "api_pages": 1,
            "api_total_hint": total_hint,
            "api_archive_snapshot": bool(is_archive_download_json),
        }

    # First preference: explicit next links in the API payload.
    next_url = candidate.get("next_url")
    if next_url:
        seen_urls = {candidate["url"]}
        pages = 1

        while next_url and next_url not in seen_urls and pages < 500:
            seen_urls.add(next_url)
            payload = _request_json(
                page,
                candidate,
                override_url=next_url,
            )
            batch = _get_json_path(payload, record_path)
            if not isinstance(batch, list):
                break
            batch = [x for x in batch if isinstance(x, dict)]
            if not batch:
                break
            records.extend(batch)
            pages += 1

            if total_hint and len(records) >= total_hint:
                break
            next_url = _json_next_url(payload, next_url)

        unique = {
            json.dumps(_flatten_record(r), sort_keys=True, ensure_ascii=False): r
            for r in records
        }
        records = list(unique.values())

        if len(records) >= minimum_rows and (
            not total_hint or len(records) >= total_hint
        ):
            return records, {
                "api_pagination_strategy": "next_link",
                "api_pages": pages,
                "api_total_hint": total_hint,
            }

    page_size = 500

    # Second preference: replay the exact request while modifying its existing
    # page/size/offset parameters (query and/or POST/GraphQL variables).
    first_url, query_changed = _query_variant(
        candidate["url"],
        0,
        page_size,
    )
    first_post, post_changed = _post_variant(
        candidate.get("post_data"),
        0,
        page_size,
    )

    if query_changed or post_changed:
        collected: list[dict] = []
        pages = 0

        for page_index in range(500):
            url_variant, _ = _query_variant(
                candidate["url"],
                page_index,
                page_size,
            )
            post_variant, _ = _post_variant(
                candidate.get("post_data"),
                page_index,
                page_size,
            )
            payload = _request_json(
                page,
                candidate,
                override_url=url_variant,
                override_post_data=post_variant,
            )
            batch = _get_json_path(payload, record_path)
            if not isinstance(batch, list):
                break
            batch = [x for x in batch if isinstance(x, dict)]
            if not batch:
                break

            collected.extend(batch)
            pages += 1

            current_total = _json_total_hint(payload) or total_hint
            if current_total and len(collected) >= current_total:
                total_hint = current_total
                break
            if len(batch) < page_size:
                break

        unique = {
            json.dumps(_flatten_record(r), sort_keys=True, ensure_ascii=False): r
            for r in collected
        }
        collected = list(unique.values())

        if len(collected) >= minimum_rows:
            return collected, {
                "api_pagination_strategy": "replay_existing_pagination",
                "api_pages": pages,
                "api_total_hint": total_hint,
            }

    # Last resort: probe standard GET pagination conventions. A convention is
    # accepted only if page 0 and page 1 return different record sets.
    if candidate["method"].upper() == "GET":
        probes = [
            ("page_limit", {"page": 1, "limit": page_size}),
            ("page_size", {"page": 0, "size": page_size}),
            ("page_page_size", {"page": 1, "page_size": page_size}),
            ("offset_limit", {"offset": 0, "limit": page_size}),
        ]

        parts = urlsplit(candidate["url"])
        original_pairs = parse_qsl(parts.query, keep_blank_values=True)

        for strategy, extra in probes:
            def make_url(index: int) -> str:
                params = dict(original_pairs)
                if strategy == "page_limit":
                    params.update({"page": index + 1, "limit": page_size})
                elif strategy == "page_size":
                    params.update({"page": index, "size": page_size})
                elif strategy == "page_page_size":
                    params.update({"page": index + 1, "page_size": page_size})
                else:
                    params.update({"offset": index * page_size, "limit": page_size})
                return urlunsplit(
                    (
                        parts.scheme,
                        parts.netloc,
                        parts.path,
                        urlencode(params),
                        parts.fragment,
                    )
                )

            try:
                payload0 = _request_json(
                    page,
                    candidate,
                    override_url=make_url(0),
                )
                payload1 = _request_json(
                    page,
                    candidate,
                    override_url=make_url(1),
                )
            except Exception:
                continue

            batch0 = _get_json_path(payload0, record_path)
            batch1 = _get_json_path(payload1, record_path)
            if not isinstance(batch0, list) or not isinstance(batch1, list):
                continue

            batch0 = [x for x in batch0 if isinstance(x, dict)]
            batch1 = [x for x in batch1 if isinstance(x, dict)]
            if not batch0 or not batch1:
                continue

            sig0 = json.dumps(
                [_flatten_record(x) for x in batch0[:3]],
                sort_keys=True,
                ensure_ascii=False,
            )
            sig1 = json.dumps(
                [_flatten_record(x) for x in batch1[:3]],
                sort_keys=True,
                ensure_ascii=False,
            )
            if sig0 == sig1:
                continue

            collected = list(batch0)
            pages = 1
            total_hint = _json_total_hint(payload0) or total_hint

            for page_index in range(1, 500):
                payload = payload1 if page_index == 1 else _request_json(
                    page,
                    candidate,
                    override_url=make_url(page_index),
                )
                batch = _get_json_path(payload, record_path)
                if not isinstance(batch, list):
                    break
                batch = [x for x in batch if isinstance(x, dict)]
                if not batch:
                    break

                collected.extend(batch)
                pages += 1

                if total_hint and len(collected) >= total_hint:
                    break
                if len(batch) < page_size:
                    break

            unique = {
                json.dumps(_flatten_record(r), sort_keys=True, ensure_ascii=False): r
                for r in collected
            }
            collected = list(unique.values())

            if len(collected) >= minimum_rows:
                return collected, {
                    "api_pagination_strategy": f"probe_{strategy}",
                    "api_pages": pages,
                    "api_total_hint": total_hint,
                }

    raise RuntimeError(
        f"Captured eSIOS API endpoint but could not exhaust it: "
        f"kind={kind}, endpoint={candidate['url']}, "
        f"method={candidate['method']}, "
        f"initial_records={len(candidate['records'])}, "
        f"total_hint={total_hint}, "
        f"record_path={'.'.join(record_path) or '$'}"
    )


def _render_esios_datatable(
    url: str,
    kind: str,
) -> tuple[pd.DataFrame, dict]:
    """
    Capture the XHR/fetch request that feeds the visible eSIOS structural table,
    then replay that API request until all pages have been collected.
    """
    from playwright.sync_api import sync_playwright

    minimum_rows = {
        "programming_units": 1000,
        "physical_units": 1000,
        "market_subjects": 100,
    }[kind]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1800, "height": 1200},
            locale="es-ES",
        )

        network_events: list[dict] = []

        def on_response(resp):
            try:
                request = resp.request
                if request.resource_type not in {"xhr", "fetch"}:
                    return

                content_type = (resp.headers.get("content-type") or "").lower()
                if "json" not in content_type:
                    return

                payload = resp.json()
                network_events.append(
                    {
                        "url": resp.url,
                        "method": request.method,
                        "post_data": request.post_data,
                        "headers": _sanitize_request_headers(request.headers),
                        "status": resp.status,
                        "payload": payload,
                    }
                )
            except Exception:
                return

        page.on("response", on_response)

        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=120_000,
        )

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
        page.wait_for_timeout(2_500)

        descriptors = _table_descriptors(page)
        candidates = [
            desc
            for desc in descriptors
            if desc["visible"]
            and desc["rows"] > 0
            and len(desc["headers"]) > 0
        ]
        candidates.sort(
            key=lambda desc: _semantic_table_score(desc, kind),
            reverse=True,
        )

        if not candidates:
            browser.close()
            raise RuntimeError(f"No visible populated eSIOS table for {kind}")

        best = candidates[0]
        best_index = int(best["index"])
        table = page.locator("table").nth(best_index)

        visible_rows = table.locator("tbody tr").evaluate_all(
            """rows => rows.map(
                tr => Array.from(tr.querySelectorAll('td'))
                    .map(td => (td.innerText || td.textContent || '').trim())
            )"""
        )

        candidate = _select_network_dataset(
            network_events,
            kind,
            visible_rows,
        )

        if candidate is None:
            diagnostics = [
                {
                    "url": event["url"],
                    "method": event["method"],
                    "status": event["status"],
                    "record_lists": [
                        {
                            "path": ".".join(path) or "$",
                            "rows": len(records),
                        }
                        for path, records in _iter_record_lists(event["payload"])
                    ][:8],
                }
                for event in network_events
            ]
            browser.close()
            raise RuntimeError(
                f"No matching eSIOS XHR/fetch JSON found for {kind}. "
                f"Captured={json.dumps(diagnostics, ensure_ascii=False)[:6000]}"
            )

        all_records, api_meta = _collect_all_network_records(
            page,
            candidate,
            kind,
            minimum_rows,
        )

        frame = _records_frame(all_records)
        final_url = page.url
        browser.close()

    return frame, {
        "url": final_url,
        "status": response.status if response else None,
        "source_mode": "captured_xhr_api",
        "rows": int(len(frame)),
        "columns": list(frame.columns),
        "visible_first_page_rows": len(visible_rows),
        "api_endpoint": candidate["url"],
        "api_method": candidate["method"],
        "api_record_path": ".".join(candidate["record_path"]) or "$",
        "api_initial_records": len(candidate["records"]),
        "api_candidate_score": candidate["score"],
        **api_meta,
    }


ALIASES = {
    "up_code": [
        "unidad_de_programacion",
        "codigo_unidad_de_programacion",
        "codigo_up",
        "cod_up",
        "up",
        "codigo",
        "codigo_de_up",
        "programming_unit_code",
        "programming_unit",
        "unit_code",
        "code",
        "vinculacion_con_up",
        "vinculacion_up",
        "unidad_programacion",
    ],
    "up_name": [
        "descripcion",
        "denominacion",
        "nombre",
        "nombre_unidad_de_programacion",
        "descripcion_corta",
        "short_description",
        "short_name",
        "long_description",
        "name",
    ],
    "uf_code": [
        "unidad_fisica",
        "codigo_unidad_fisica",
        "codigo_uf",
        "cod_uf",
        "uf",
        "codigo",
        "codigo_de_uf",
        "physical_unit_code",
        "physical_unit",
        "unit_code",
        "code",
    ],
    "uf_name": [
        "descripcion",
        "denominacion",
        "nombre",
        "nombre_unidad_fisica",
        "descripcion_corta",
        "short_description",
        "short_name",
        "long_description",
        "name",
    ],
    "subject_code": [
        "codigo_sujeto",
        "sujeto",
        "participante",
        "codigo_participante",
        "codigo_agente",
        "codigo_de_sujeto",
        "market_subject_code",
        "subject_code",
        "market_subject",
        "subject",
        "vinculacion_con_sm",
        "vinculacion_sm",
        "sujeto_del_mercado",
    ],
    "legal_entity": [
        "razon_social",
        "nombre_sujeto",
        "denominacion",
        "nombre",
        "sujeto_del_mercado",
        "market_subject_name",
        "subject_name",
        "legal_name",
        "company_name",
        "name",
    ],
    "technology": [
        "tecnologia",
        "tipo_produccion",
        "tipo_de_produccion",
        "production_type",
        "generation_type",
        "technology",
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

            minimum_complete_rows = {
                "programming_units": 1000,
                "physical_units": 1000,
                "market_subjects": 100,
            }[kind]

            complete = bool(
                usable_rows >= minimum_complete_rows
                and meta.get("source_mode") == "captured_xhr_api"
                and int(meta.get("api_pages") or 0) >= 1
            )

            meta["canonical_rows"] = int(len(canonical))
            meta["canonical_usable_key_rows"] = usable_rows
            meta["canonical_key"] = required_key
            meta["canonical_columns"] = list(canonical.columns)
            meta["minimum_complete_rows"] = minimum_complete_rows
            meta["complete"] = complete

            if not complete:
                raise RuntimeError(
                    f"eSIOS {kind} pagination incomplete: "
                    f"usable_rows={usable_rows}, "
                    f"minimum_complete_rows={minimum_complete_rows}, "
                    f"source_mode={meta.get('source_mode')}, "
                    f"api_endpoint={meta.get('api_endpoint')}, "
                    f"api_pages={meta.get('api_pages')}, "
                    f"api_total_hint={meta.get('api_total_hint')}"
                )

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
