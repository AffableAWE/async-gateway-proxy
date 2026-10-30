"""API key helpers: hashing, and a small CLI to make new keys.

Create a key for a client:
    python -m app.security mobile-app --routes /api /cpp

It prints the key once (give it to the client) and a config.yaml snippet
holding only the key's SHA-256 hash. The gateway never stores the key.
"""

import argparse
import hashlib
import secrets

# A fixed prefix makes leaked keys easy to spot: secret scanners (GitHub's,
# for one) and humans can grep for it, the same way GitHub tokens start
# with ghp_.
KEY_PREFIX = "gw_"


def hash_api_key(key: str) -> str:
    """SHA-256 of the key, as lowercase hex.

    Why a plain SHA-256 and not bcrypt or argon2: those are slow on purpose,
    to protect low-entropy human passwords from guessing. Our keys are 256
    random bits, so guessing is hopeless however fast the hash is, and a
    slow hash would add ~100 ms to every single request.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)  # 32 random bytes = 256 bits


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create a new gateway API key.")
    parser.add_argument("client", help="client name, e.g. mobile-app")
    parser.add_argument(
        "--routes", nargs="+", default=["/api"], help="route prefixes this key may use"
    )
    args = parser.parse_args(argv)

    key = new_api_key()
    routes = ", ".join(args.routes)
    print("API key (shown once; give it to the client, don't commit it):")
    print(f"  {key}")
    print()
    print("Add this under auth.keys in config.yaml:")
    print(f"  - client: {args.client}")
    print(f"    key_sha256: {hash_api_key(key)}")
    print(f"    routes: [{routes}]")


if __name__ == "__main__":
    main()
