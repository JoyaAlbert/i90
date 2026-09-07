from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .api import EsiosClient
from .archive import csv_candidates, save_and_extract
from .csv_tools import read_csv_robust, schema_for

TZ = ZoneInfo("Europe/Madrid")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(
    out_root: Path = Path("data/work"),
    public_root: Path = Path("public"),
    max_backtrack_days: int = 7,
) -> dict:
    api_key = os.environ.get("ESIOS_API_KEY")
    if not api_key:
        raise RuntimeError("Missing ESIOS_API_KEY environment variable")

    now = datetime.now(TZ)
    today = now.date()
    target = today - timedelta(days=90)
    client = EsiosClient(api_key)

    selected_day = None
    metadata = {}
    blob = None
    download_meta = {}
    errors = []

    for back in range(max_backtrack_days + 1):
        day = target - timedelta(days=back)

        # 1) Metadata-driven route.
        try:
            candidate = client.get_candidate(day)
            if candidate:
                blob, download_meta = client.download(candidate.download_url)
                selected_day = candidate.data_date
                metadata = candidate.metadata
                break
        except Exception as exc:
            errors.append({
                "day": day.isoformat(),
                "stage": "metadata_or_metadata_download",
                "error": f"{type(exc).__name__}: {exc}",
            })

        # 2) Direct archive download fallback.
        try:
            direct = client.direct_download_url(day)
            blob, download_meta = client.download(direct)
            if blob:
                selected_day = day
                metadata = {
                    "id": 34,
                    "name": "I90DIA",
                    "resolved_via": "direct_download_fallback",
                }
                break
        except Exception as exc:
            errors.append({
                "day": day.isoformat(),
                "stage": "direct_download",
                "error": f"{type(exc).__name__}: {exc}",
            })

    if selected_day is None or blob is None:
        raise RuntimeError(
            "No downloadable I90DIA found in target/backtrack window. "
            + json.dumps(errors, ensure_ascii=False)
        )

    day_dir = out_root / selected_day.isoformat()
    paths = save_and_extract(blob, day_dir)
    candidates = csv_candidates(paths)

    parsed = None
    parse_info = None
    parse_errors = []
    for candidate_path in candidates:
        try:
            df, info = read_csv_robust(candidate_path)
            parsed = df
            parse_info = info
            break
        except Exception as exc:
            parse_errors.append({
                "path": str(candidate_path),
                "error": f"{type(exc).__name__}: {exc}",
            })

    if parsed is None or parse_info is None:
        raise RuntimeError(
            "Downloaded I90DIA but could not parse CSV. "
            + json.dumps(parse_errors, ensure_ascii=False)
        )

    manifest = {
        "generated_at": now.isoformat(),
        "today_madrid": today.isoformat(),
        "target_data_date": target.isoformat(),
        "selected_data_date": selected_day.isoformat(),
        "backtrack_days": (target - selected_day).days,
        "archive_id": 34,
        "archive_name": "I90DIA",
        "download": download_meta,
        "metadata": metadata,
        "sha256_download": sha256_bytes(blob),
        "csv": {
            "encoding": parse_info.encoding,
            "separator": parse_info.separator,
            "rows": parse_info.rows,
            "columns": parse_info.columns,
        },
        "acquisition_errors_before_success": errors,
    }

    schema = schema_for(parsed)

    latest = public_root / "latest"
    hist = public_root / "history" / selected_day.isoformat()
    latest.mkdir(parents=True, exist_ok=True)
    hist.mkdir(parents=True, exist_ok=True)

    for dest in (latest, hist):
        (dest / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (dest / "schema.json").write_text(
            json.dumps(schema, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        parsed.head(200).to_csv(dest / "preview.csv", index=False)

    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest
