from i90_ingest.pipeline import is_no_values_payload


def test_is_no_values_payload_detects_esios_no_values_message():
    blob = b'{\n  "message": "No values for specified archive"\n}'
    assert is_no_values_payload(blob)


def test_is_no_values_payload_ignores_regular_csv_content():
    blob = b"UP;P1;P2\nABC;1;2\n"
    assert not is_no_values_payload(blob)


def test_is_no_values_payload_ignores_non_json_binary_content():
    blob = b"\x50\x4b\x03\x04binary"
    assert not is_no_values_payload(blob)
