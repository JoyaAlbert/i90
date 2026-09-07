from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd


@dataclass
class CsvReadResult:
    path: str
    encoding: str
    separator: str
    rows: int
    columns: list[str]


def read_csv_robust(path: Path) -> tuple[pd.DataFrame, CsvReadResult]:
    attempts = [
        ("utf-8-sig", ";"),
        ("utf-8-sig", ","),
        ("utf-8", ";"),
        ("utf-8", ","),
        ("latin-1", ";"),
        ("latin-1", ","),
        ("cp1252", ";"),
        ("cp1252", ","),
    ]
    errors: list[str] = []

    for encoding, sep in attempts:
        try:
            df = pd.read_csv(path, encoding=encoding, sep=sep, low_memory=False)
            if len(df.columns) <= 1:
                errors.append(f"{encoding}/{sep}: only one column")
                continue
            return df, CsvReadResult(
                path=str(path),
                encoding=encoding,
                separator=sep,
                rows=len(df),
                columns=[str(c) for c in df.columns],
            )
        except Exception as exc:
            errors.append(f"{encoding}/{sep}: {type(exc).__name__}: {exc}")

    raise RuntimeError("Unable to parse CSV. Attempts: " + " | ".join(errors))


def schema_for(df: pd.DataFrame) -> dict:
    return {
        "row_count": int(len(df)),
        "column_count": int(len(df.columns)),
        "columns": [
            {
                "name": str(col),
                "dtype": str(df[col].dtype),
                "non_null": int(df[col].notna().sum()),
                "unique": int(df[col].nunique(dropna=True)),
            }
            for col in df.columns
        ],
    }
