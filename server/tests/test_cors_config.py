from app.config import load_settings


def test_default_cors_regex_covers_operator_map_and_private_booth(monkeypatch) -> None:
    monkeypatch.delenv("CORS_ORIGIN_REGEX", raising=False)
    regex = load_settings().cors_origin_regex

    assert regex is not None
    import re

    assert re.fullmatch(regex, "http://localhost:5555")
    assert re.fullmatch(regex, "http://127.0.0.1:3000")
    assert re.fullmatch(regex, "http://192.168.4.20:3000")
    assert re.fullmatch(regex, "http://172.20.10.12:5555")
    assert not re.fullmatch(regex, "https://attacker.example")
