from __future__ import annotations

import io
import zipfile
from pathlib import Path


def save_and_extract(blob: bytes, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(io.BytesIO(blob)):
        raw_zip = dest / "I90DIA.zip"
        raw_zip.write_bytes(blob)
        extract_dir = dest / "extracted"
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            zf.extractall(extract_dir)
        return [p for p in extract_dir.rglob("*") if p.is_file()]

    raw = dest / "I90DIA_download"
    raw.write_bytes(blob)
    return [raw]


def csv_candidates(paths: list[Path]) -> list[Path]:
    csvs = [p for p in paths if p.suffix.lower() == ".csv"]
    if csvs:
        return csvs

    # Some downloads may have no extension despite containing CSV text.
    return [p for p in paths if p.is_file()]
