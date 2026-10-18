"""Headless unlock for MailForge's encrypted credential store (containers).

MailForge falls back to ``keyrings.cryptfile`` when there is no desktop keyring
(src/mailforge/secrets.py). cryptfile asks for its password with getpass, which
cannot work in a container: stdin is not a terminal. Its documented
KEYRING_PROPERTY_* environment hook does not reach it either (its __init__ does
not run the base class's), so this subclass reads the password from the
environment instead.

Selected by PYTHON_KEYRING_BACKEND=container_keyring.EnvCryptFileKeyring (set in
the image). The password comes from MAILFORGE_KEYRING_KEY, which the entrypoint
fills from MAILFORGE_KEYRING_PASSWORD or MAILFORGE_KEYRING_PASSWORD_FILE.

With no password and no terminal the store stays LOCKED and says so, rather
than hanging or dying on EOFError; an interactive `docker compose run -it`
session still gets cryptfile's normal prompt.
"""
from __future__ import annotations

import os
import sys

from keyring.errors import KeyringLocked
from keyrings.cryptfile.cryptfile import CryptFileKeyring


class EnvCryptFileKeyring(CryptFileKeyring):
    def __init__(self) -> None:
        super().__init__()
        self._env_key = os.environ.get("MAILFORGE_KEYRING_KEY", "")

    def _init_keyring(self) -> None:
        # Lazy on purpose (Argon2 costs real time) and routed through the base
        # class's _init_keyring so the store is initialised or UNLOCKED AND
        # VERIFIED against its password reference: setting _keyring_key directly
        # would skip that, and a wrong password would silently start a second,
        # unreadable set of entries.
        if self._keyring_key is None:
            if self._env_key.strip():
                self._keyring_key = self._env_key
            elif not sys.stdin.isatty():
                raise KeyringLocked(
                    "the credential store is locked: set MAILFORGE_KEYRING_PASSWORD "
                    "(or MAILFORGE_KEYRING_PASSWORD_FILE) for the container"
                )
        super()._init_keyring()
