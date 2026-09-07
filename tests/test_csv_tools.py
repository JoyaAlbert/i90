from pathlib import Path

from i90_ingest.csv_tools import read_csv_robust, schema_for


def test_semicolon_csv(tmp_path: Path):
    p = tmp_path / "sample.csv"
    p.write_text("UP;P1;P2\nABC;1;2\nDEF;3;4\n", encoding="utf-8")
    df, info = read_csv_robust(p)
    assert list(df.columns) == ["UP", "P1", "P2"]
    assert info.separator == ";"
    schema = schema_for(df)
    assert schema["row_count"] == 2
    assert schema["column_count"] == 3
