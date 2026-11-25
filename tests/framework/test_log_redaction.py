from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest

from plugin.framework.log_redaction import (
    RedactingFormatter,
    is_secret_key,
    redact_secret_pairs_in_text,
    redact_urls_in_text,
    register_known_secret,
)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("token", True),
        ("api_key", True),
        ("apiKey", True),
        ("x-api-key", True),
        ("X-Auth-Token", True),
        ("github_token", True),
        ("db_password", True),
        ("Authorization", True),
        ("clientSecret", True),
        ("APIToken", True),
        ("access_token", True),
        ("private_key", True),
        ("Cookie", True),
        ("credentials", True),
        ("max_tokens", False),
        ("tokenizer", False),
        ("author", False),
        ("secretary", False),
        ("key", False),
        ("sort_key", False),
        ("keyword", False),
        ("prompt_tokens", False),
        ("passwordless", False),
        ("authors", False),
    ],
)
def test_is_secret_key(key: str, expected: bool) -> None:
    assert is_secret_key(key) is expected


@pytest.mark.parametrize(
    ("secret", "depth"),
    [
        ("hunter2", 0),
        ("", 0),
        ('say "hi"\\', 0),
        ("line1\nline2", 0),
        ("hunter2", 1),
        ('say "hi"\\', 1),
        ("line1\nline2", 2),
        ('deep "quote"\\', 3),
    ],
)
def test_redact_secret_pairs_in_text_json_depths(secret: str, depth: int) -> None:
    inner: Any = {"name": "S", "password": secret, "current_password": secret + "_old"}
    for _ in range(depth):
        inner = {"payload": json.dumps(inner)}
    text = json.dumps(inner)

    redacted = redact_secret_pairs_in_text(text)
    if secret:
        assert secret not in redacted
        assert json.dumps(secret)[1:-1] not in redacted

    parsed = json.loads(redacted)
    for _ in range(depth):
        parsed = json.loads(parsed["payload"])
    assert parsed == {"name": "S", "password": "<redacted>", "current_password": "<redacted>"}


def test_redact_secret_pairs_in_text_variants() -> None:
    py_repr = "{'name': 'S', 'password': 's3cr\\'et\\\\', 'n': 1}"
    redacted_repr = redact_secret_pairs_in_text(py_repr)
    assert "s3cr" not in redacted_repr
    assert "{'name': 'S', 'password': '<redacted>', 'n': 1}" == redacted_repr

    num_json = '{"name": "S", "password": 1234, "count": 5}'
    redacted_num = redact_secret_pairs_in_text(num_json)
    assert "1234" not in redacted_num
    assert json.loads(redacted_num) == {"name": "S", "password": "<redacted>", "count": 5}

    unterminated = '{"password": "hunter2'
    redacted_unterm = redact_secret_pairs_in_text(unterminated)
    assert "hunter2" not in redacted_unterm
    assert redacted_unterm == '{"password": "<redacted>'


@pytest.mark.parametrize(
    "secret",
    [
        "it's",
        'a"TAIL',
        "b'\"TAIL",
        "trail\\",
    ],
)
@pytest.mark.parametrize(
    "encode",
    [
        lambda d: repr(d),
        lambda d: json.dumps({"e": str(d)}),
        lambda d: repr({"arguments": json.dumps(d)}),
    ],
)
def test_redact_secret_pairs_in_text_repr_and_mixed_layers(secret: str, encode: Any) -> None:
    text = encode({"name": "S", "password": secret})
    redacted = redact_secret_pairs_in_text(text)
    assert "TAIL" not in redacted
    assert "it's" not in redacted
    assert "trail" not in redacted
    assert "<redacted>" in redacted


def test_redact_secret_pairs_leaves_other_text() -> None:
    cases = [
        '{"max_tokens": 1024, "has_password": true, "password": null}',
        '{"password": {"type": "string", "description": "Section password"}}',
        "The user set the password to hunter2 in prose.",
    ]
    for text in cases:
        assert redact_secret_pairs_in_text(text) == text


def test_redact_urls_in_text() -> None:
    text = "Tunnel ready (running at http://bore.pub:9999/mcp.) and https://xyz.trycloudflare.com/mcp"
    redacted = redact_urls_in_text(text)
    assert "bore.pub" not in redacted
    assert "xyz.trycloudflare.com" not in redacted
    assert "Tunnel ready (running at " in redacted
    assert " and " in redacted
    assert "<redacted>" in redacted

    plain = "No URLs here, just localhost:18765"
    assert redact_urls_in_text(plain) == plain


def _format_with_arbitrary_logger(logger_name: str, msg: str, *args: Any, exc_info: Any = None) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(name)s | %(levelname)s | %(message)s"))
    logger = logging.getLogger(logger_name)
    prev_level = logger.level
    prev_propagate = logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.warning(msg, *args, exc_info=exc_info)
        return stream.getvalue()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.propagate = prev_propagate


def test_redacting_formatter_masks_urls_on_arbitrary_loggers() -> None:
    out1 = _format_with_arbitrary_logger(
        "test.arbitrary.tts",
        "TTS POST %s failed",
        "https://user:supersecret@api.example.com:8443/v1/chat?api_key=sk-query123&model=gpt-4#frag",
    )
    assert "supersecret" not in out1
    assert "sk-query123" not in out1
    assert "frag" not in out1
    assert "https://api.example.com:8443/v1/chat" in out1

    out2 = _format_with_arbitrary_logger(
        "test.arbitrary.images",
        "Downloading %s for model",
        "http://[::1]:11434/s/123e4567-e89b-12d3-a456-426614174000/v1/models/MyModel2026",
    )
    assert "123e4567-e89b-12d3-a456-426614174000" not in out2
    assert "http://[::1]:11434/s/<redacted>/v1/models/MyModel2026" in out2

    out3 = _format_with_arbitrary_logger(
        "test.arbitrary.badurl",
        "Error for %s: %s",
        "http://u:pw@h.example:1a34/s/123e4567-e89b-12d3-a456-426614174000/v1",
        "https://[::1/v1?e=a@b&key=hunter2",
    )
    assert "pw" not in out3
    assert "hunter2" not in out3
    assert "http://h.example:1a34/s/<redacted>/v1:" in out3


@pytest.mark.parametrize(
    ("raw_line", "hidden", "kept"),
    [
        ("POST https://user:pa'ss@h.example/v1", "pa'ss", "https://h.example/v1"),
        ("POST https://user:p)ss@h.example/v1", "p)ss", "https://h.example/v1"),
        ('POST https://user:pa"ss@h.example/v1', 'pa"ss', "https://h.example/v1"),
        ("GET https://h/x/abc)123e4567-e89b-12d3-a456-426614174000/z", "123e4567-e89b-12d3-a456-426614174000", "https://h/x/<redacted>/z"),
        ("/api/foo/chat?sig=ab'cd&x=1", "ab'cd", "/api/foo/chat?sig=<redacted>&x=1"),
        ("GET HTTPS://user:hunter2@h.example/v1", "hunter2", "https://h.example/v1"),
        ("WS ws://u:hunter2@h/x", "hunter2", "ws://h/x"),
        ("PROXY socks5://u:hunter2@h:1", "hunter2", "socks5://h:1"),
        ('{"url":"https://h/a?x=1","n":1,"text":"lost words","k":2}', "x=1", '{"url":"https://h/a","n":1,"text":"lost words","k":2}'),
        ("OPEN file:///home/k/Contract2026FinalVersion.odt", "<redacted>", "file:///home/k/Contract2026FinalVersion.odt"),
        (r'{"msg": "see https://h/a?b=1\n\nHello there"}', "b=1", r'see https://h/a\n\nHello there'),
    ],
)
def test_redacting_formatter_url_and_userinfo_spellings(raw_line: str, hidden: str, kept: str) -> None:
    out = _format_with_arbitrary_logger("test.arbitrary.spellings", "%s", raw_line)
    assert hidden not in out
    assert kept in out


def test_redacting_formatter_long_hex_run_with_url() -> None:
    blob = "ab" * 100_000
    out = _format_with_arbitrary_logger("test.arbitrary.longrun", "%s https://u:pw@h.example/v1?k=1", blob)
    assert "pw" not in out
    assert "k=1" not in out
    assert blob in out
    assert "https://h.example/v1" in out


def test_redacting_formatter_masks_known_secrets_across_arbitrary_loggers() -> None:
    register_known_secret("sk-known-secret-987654")
    register_known_secret("ollama")  # Short placeholder key (<8 chars); must not mask hosts/models/paths.

    out1 = _format_with_arbitrary_logger(
        "test.arbitrary.one",
        r"Auth failed with key sk-known-secret-987654 on http://ollama:11434 model=latest {\nsk-known-secret-987654 x%3Dsk-known-secret-987654 prefixsk-known-secret-987654",
    )
    assert "sk-known-secret-987654" not in out1
    assert "http://ollama:11434 model=latest" in out1
    assert r"{\n<redacted> x%3D<redacted> prefix<redacted>" in out1

    try:
        raise RuntimeError("Upstream rejected sk-known-secret-987654")
    except RuntimeError:
        out2 = _format_with_arbitrary_logger("test.arbitrary.two", "Call failed", exc_info=True)
    assert "sk-known-secret-987654" not in out2
    assert "Upstream rejected <redacted>" in out2


def test_resolve_auth_registers_known_secret() -> None:
    from plugin.framework.client.auth import resolve_auth_for_config

    resolve_auth_for_config({"endpoint": "https://api.openai.com/v1", "api_key": "sk-auth-registered-5555"})
    out = _format_with_arbitrary_logger("test.arbitrary.auth", "Echoed sk-auth-registered-5555")
    assert "sk-auth-registered-5555" not in out
    assert "Echoed <redacted>" in out


def test_config_registers_known_secrets(tmp_path: Any, monkeypatch: Any) -> None:
    from plugin.framework import config as fw_config

    monkeypatch.setattr(fw_config, "user_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(fw_config.keystore, "can_store", lambda: False)
    fw_config.save({"api_keys_by_endpoint": {"https://api.openai.com": "sk-config-secret-7788"}})
    assert fw_config.get_api_key_for_endpoint("https://api.openai.com") == "sk-config-secret-7788"

    out = _format_with_arbitrary_logger("test.arbitrary.cfg", "Upstream echoed sk-config-secret-7788")
    assert "sk-config-secret-7788" not in out
    assert "Upstream echoed <redacted>" in out


def test_shared_debug_file_handler_redacts_urls_and_known_secrets(tmp_path: Any) -> None:
    from plugin.framework import logging as fw_logging

    log_path = tmp_path / "writeragent_debug.log"
    shared = fw_logging._shared()
    old_path = shared["debug_log_path"]
    old_handler = fw_logging._debug_slot()["handler"]
    shared["debug_log_path"] = str(log_path)
    fw_logging._debug_slot()["handler"] = None
    try:
        handler = fw_logging._shared_debug_file_handler()
        register_known_secret("sk-file-handler-secret-42")
        record = logging.LogRecord(
            "writeragent.test",
            logging.WARNING,
            __file__,
            1,
            "Request to %s failed: %s",
            ("https://u:pw123456@api.example.com/v1?key=abc", "bad sk-file-handler-secret-42"),
            None,
        )
        handler.emit(record)
        handler.close()
        written = log_path.read_text(encoding="utf-8")
        assert "pw123456" not in written
        assert "key=abc" not in written
        assert "sk-file-handler-secret-42" not in written
        assert "https://api.example.com/v1" in written
        assert "bad <redacted>" in written
    finally:
        if fw_logging._debug_slot()["handler"] is not None:
            fw_logging._debug_slot()["handler"].close()
        fw_logging._debug_slot()["handler"] = old_handler
        shared["debug_log_path"] = old_path
