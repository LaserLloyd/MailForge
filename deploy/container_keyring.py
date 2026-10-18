"""Headless unlock for MailForge's encrypted credential store (containers).

MailForge falls back to ``keyrings.cryptfile`` when there is no desktop keyring
(src/mailforge/secrets.py). cryptfile asks for its password with getpass, which
cannot work in a container: stdin is not a terminal. Its documented
KEYRING_PROPERTY_* environment hook does not reach it either (its __init__ does
not run the base class's), so this subclass reads the password from the
environment instead.

Selected by PYTHON_KEYRING_BACKEND=container_keyring.EnvCryptFileKeyring (set in
the image). The password comes from MAILFORGE_KEYRING_KEY, which the entrypoint
fills from MAILFORGE_KEYRING_PASSWORD or MAILFORGE_KEYRING_PASSWORD_FILE. A
`docker compose exec` / `docker exec` process does NOT go through the entrypoint,
so when MAILFORGE_KEYRING_KEY is absent the same two variables are read here, with
the entrypoint's precedence (a readable _FILE wins, trailing newlines dropped as
`$(cat file)` does).

With no password and no terminal the store stays LOCKED and says so, rather
than hanging or dying on EOFError; an interactive `docker compose run -it`
session still gets cryptfile's normal prompt. A WRONG password is reported as
KeyringLocked with a plain sentence, not as cryptfile's bare "MAC check failed".
"""
from __future__ import annotations

import os
import sys

from keyring.errors import KeyringLocked
from keyrings.cryptfile.cryptfile import CryptFileKeyring


def password_from_env() -> str:
    """The store password, exactly as docker-entrypoint.sh step 5 derives it."""
    key = os.environ.get("MAILFORGE_KEYRING_KEY", "")
    if key:
        return key
    password = os.environ.get("MAILFORGE_KEYRING_PASSWORD", "")
    path = os.environ.get("MAILFORGE_KEYRING_PASSWORD_FILE", "")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                password = fh.read().rstrip("\n")
        except OSError:
            pass  # the entrypoint logs this; fall back to the plain variable
    return password


class EnvCryptFileKeyring(CryptFileKeyring):
    def __init__(self) -> None:
        super().__init__()
        self._env_key = password_from_env()

    def _init_keyring(self) -> None:
        # Lazy on purpose (Argon2 costs real time) and routed through the base
        # class's _init_keyring so the store is initialised or UNLOCKED AND
        # VERIFIED against its password reference: setting _keyring_key directly
        # would skip that, and a wrong password would silently start a second,
        # unreadable set of entries.
        from_env = False
        if self._keyring_key is None:
            if self._env_key.strip():
                self._keyring_key = self._env_key
                from_env = True
            elif not sys.stdin.isatty():
                raise KeyringLocked(
                    "the credential store is locked: set MAILFORGE_KEYRING_PASSWORD "
                    "(or MAILFORGE_KEYRING_PASSWORD_FILE) for the container"
                )
        try:
            super()._init_keyring()
        except ValueError as e:  # "MAC check failed" / "Incorrect Password"
            self._keyring_key = None
            source = "MAILFORGE_KEYRING_PASSWORD" if from_env else "the password entered"
            raise KeyringLocked(
                f"the credential store at {self.file_path} did not unlock with {source} "
                f"({e}): wrong password? It must be the one the store was created with."
            ) from None
