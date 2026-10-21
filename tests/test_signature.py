from prharness.signature import sign, verify_signature


def test_valid_signature() -> None:
    assert verify_signature("s", b"body", sign("s", b"body"))


def test_rejects_wrong_secret_missing_header_and_empty_secret() -> None:
    assert not verify_signature("s", b"body", sign("other", b"body"))
    assert not verify_signature("s", b"body", None)
    assert not verify_signature("s", b"body", "sha1=abc")
    assert not verify_signature("", b"body", sign("", b"body"))
