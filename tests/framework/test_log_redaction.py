from __future__ import annotations

import json
from typing import Any

import pytest

from plugin.framework.log_redaction import is_secret_key, redact_secret_pairs_in_text, redact_urls_in_text


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
