import pytest

from chaosloop.tools.redact import REDACTED, redact, redact_text


@pytest.mark.parametrize(
    "secret, text",
    [
        ("AKIAIOSFODNN7EXAMPLE", "env AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE loaded"),
        ("hunter2", "password: hunter2"),
        ("hunter2", 'DB_PASSWORD="hunter2"'),
        ("s3cr3t-value", "api_key=s3cr3t-value"),
        ("abc123def456ghi789", "Authorization: Bearer abc123def456ghi789"),
        ("sk-abcdefghijklmnopqrstuvwxyz", "OPENAI_KEY=sk-abcdefghijklmnopqrstuvwxyz"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        ),
    ],
)
def test_secret_shaped_strings_are_removed(secret: str, text: str):
    cleaned = redact_text(text)
    assert secret not in cleaned
    assert REDACTED in cleaned


@pytest.mark.parametrize(
    "text, keep",
    [
        (
            "failed to fetch anonymous token: unexpected status from GET request: 403 Forbidden",
            "unexpected status",
        ),
        ("the secret: payment-db is referenced but not mounted", "payment-db"),
    ],
)
def test_prose_mentioning_a_secret_is_not_scrubbed(text: str, keep: str):
    """Over-redaction destroys evidence: this wording is what identifies an auth failure."""
    assert keep in redact_text(text)


def test_private_key_block_removed():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nkey\n-----END RSA PRIVATE KEY-----"
    assert "MIIEow" not in redact_text(pem)


def test_image_digest_survives():
    """Digests are long and opaque but are evidence, not secrets."""
    digest = "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    assert digest in redact_text(f'image: "app@{digest}"')


def test_nested_structures_are_walked():
    payload = {
        "spec": {
            "containers": [
                {"name": "app", "env": [{"name": "TOKEN", "value": "token=abcdefghijklmnop"}]}
            ]
        }
    }
    value = redact(payload)["spec"]["containers"][0]["env"][0]["value"]
    assert "abcdefghijklmnop" not in value


def test_sensitive_keys_are_blanked():
    cleaned = redact({"password": "hunter2", "client_secret": "abc", "replicas": 3})
    assert cleaned["password"] == REDACTED
    assert cleaned["client_secret"] == REDACTED
    assert cleaned["replicas"] == 3


def test_secret_references_stay_visible():
    """The agent must see that a Secret is referenced, just not its value (HLD §11)."""
    spec = {"secretKeyRef": {"name": "payment-db", "key": "password"}}
    cleaned = redact(spec)
    assert cleaned["secretKeyRef"]["name"] == "payment-db"
    assert cleaned["secretKeyRef"]["key"] == "password"


def test_non_string_scalars_untouched():
    assert redact({"count": 3, "ready": True, "ratio": 0.5}) == {
        "count": 3,
        "ready": True,
        "ratio": 0.5,
    }
