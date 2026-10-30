from app.security import KEY_PREFIX, hash_api_key, main, new_api_key


def test_hash_is_sha256_hex():
    # Known SHA-256 test vector for "abc".
    assert hash_api_key("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_new_keys_are_prefixed_long_and_unique():
    keys = {new_api_key() for _ in range(100)}
    assert len(keys) == 100
    assert all(k.startswith(KEY_PREFIX) and len(k) >= 40 for k in keys)


def test_cli_prints_the_key_and_a_matching_hash(capsys):
    main(["mobile-app", "--routes", "/api", "/cpp"])
    out = capsys.readouterr().out
    key = out.split("\n")[1].strip()
    assert key.startswith(KEY_PREFIX)
    assert f"key_sha256: {hash_api_key(key)}" in out
    assert "client: mobile-app" in out
    assert "routes: [/api, /cpp]" in out
