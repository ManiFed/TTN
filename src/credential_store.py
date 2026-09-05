"""OS keyring + encrypted-file fallback for node cloud credentials.

Headless / LaunchAgent macOS hosts often cannot write the System keychain
(status -61 / errSecInvalidOwnerEdit / "Unknown Error"). Prefer the OS
keyring when it works; on known write failures fall back to an encrypted
file under ``data/`` so a restart does not silently mint a new node
identity.

Secrets are never written in cleartext to ``cloud_state.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

import keyring
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("credential_store")

_SERVICE = "the-telescope-node"
_FILE_DIR = Path("data")
_KEY_PATH = _FILE_DIR / ".node_credential_key"
_STORE_PATH = _FILE_DIR / "credentials.enc"

# macOS Security.framework: errSecInvalidOwnerEdit
_KEYCHAIN_DENIED_RE = re.compile(
    r"(-61\b|errSecInvalidOwnerEdit|Unknown Error|not permitted|"
    r"User interaction is not allowed|interaction not allowed|"
    r"SecKeychain|write.?denied)",
    re.IGNORECASE,
)

_lock = threading.RLock()
_backend: str = "keyring"  # keyring | encrypted_file | memory
_last_error: Optional[str] = None


def service_name() -> str:
    return _SERVICE


def backend() -> str:
    return _backend


def last_error() -> Optional[str]:
    return _last_error


def is_keychain_denied(exc: BaseException) -> bool:
    """True when the failure looks like macOS keychain write denial (-61)."""
    text = str(exc)
    if getattr(exc, "errno", None) == -61:
        return True
    return bool(_KEYCHAIN_DENIED_RE.search(text))


def _set_error(msg: Optional[str]) -> None:
    global _last_error
    _last_error = msg


def _set_backend(name: str) -> None:
    global _backend
    _backend = name


def _ensure_fernet() -> Fernet:
    _FILE_DIR.mkdir(exist_ok=True)
    if _KEY_PATH.exists():
        key = _KEY_PATH.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        _KEY_PATH.write_bytes(key + b"\n")
        try:
            os.chmod(_KEY_PATH, 0o600)
        except OSError:
            pass
    return Fernet(key)


def _read_file_store() -> dict:
    if not _STORE_PATH.exists():
        return {}
    try:
        f = _ensure_fernet()
        raw = f.decrypt(_STORE_PATH.read_bytes())
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, InvalidToken, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Could not read encrypted credential file: %s", exc)
        return {}


def _write_file_store(data: dict) -> None:
    f = _ensure_fernet()
    payload = json.dumps(data, indent=None, sort_keys=True).encode("utf-8")
    _FILE_DIR.mkdir(exist_ok=True)
    tmp = _STORE_PATH.with_suffix(".enc.tmp")
    tmp.write_bytes(f.encrypt(payload))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(_STORE_PATH)
    try:
        os.chmod(_STORE_PATH, 0o600)
    except OSError:
        pass


def _file_get(account: str) -> Optional[str]:
    with _lock:
        data = _read_file_store()
        val = data.get(account)
        return str(val) if val else None


def _file_set(account: str, password: str) -> None:
    with _lock:
        data = _read_file_store()
        data[account] = password
        _write_file_store(data)


def _file_delete(account: str) -> None:
    with _lock:
        data = _read_file_store()
        if account in data:
            del data[account]
            if data:
                _write_file_store(data)
            elif _STORE_PATH.exists():
                try:
                    _STORE_PATH.unlink()
                except OSError:
                    pass


def get_password(account: str, *, service: str = _SERVICE) -> Optional[str]:
    """Load a secret: OS keyring first, then encrypted file fallback."""
    try:
        val = keyring.get_password(service, account)
        if val:
            _set_backend("keyring")
            return val
    except keyring.errors.KeyringError as exc:
        logger.warning("Keyring read failed for %s: %s", account, exc)
        _set_error(str(exc))
    val = _file_get(account)
    if val:
        _set_backend("encrypted_file")
    return val


def set_password(account: str, password: str, *, service: str = _SERVICE) -> str:
    """Persist a secret.

    Returns the backend used (``keyring`` or ``encrypted_file``).
    Raises ``keyring.errors.KeyringError`` only when *both* keyring and the
    encrypted-file fallback fail.
    """
    try:
        keyring.set_password(service, account, password)
        _set_backend("keyring")
        _set_error(None)
        # Mirror into encrypted file so a later keychain regression still
        # survives restart (best-effort; ignore mirror failures).
        try:
            _file_set(account, password)
        except OSError as exc:
            logger.debug("Could not mirror credential to encrypted file: %s", exc)
        return "keyring"
    except keyring.errors.KeyringError as exc:
        denied = is_keychain_denied(exc)
        detail = (
            f"Keychain write failed ({exc}). "
            + (
                "macOS status -61 / errSecInvalidOwnerEdit usually means this "
                "headless or LaunchAgent process cannot modify the System "
                "keychain — falling back to an encrypted file under data/ "
                "so the node identity survives restart."
                if denied
                else "Falling back to an encrypted file under data/ so the "
                "node identity survives restart."
            )
        )
        logger.warning("%s", detail)
        _set_error(detail)
        try:
            _file_set(account, password)
            _set_backend("encrypted_file")
            logger.info(
                "Persisted %s via encrypted file fallback (keyring unavailable)",
                account,
            )
            return "encrypted_file"
        except OSError as file_exc:
            _set_backend("memory")
            msg = (
                f"{detail} Encrypted-file fallback also failed: {file_exc}. "
                "Credentials are memory-only; a restart may mint a new node id."
            )
            _set_error(msg)
            logger.error("%s", msg)
            raise keyring.errors.KeyringError(msg) from file_exc


def delete_password(account: str, *, service: str = _SERVICE) -> None:
    """Remove a secret from keyring and encrypted file (best-effort)."""
    try:
        keyring.delete_password(service, account)
    except keyring.errors.KeyringError:
        pass
    try:
        _file_delete(account)
    except OSError as exc:
        logger.debug("Could not delete encrypted credential %s: %s", account, exc)


def status_snapshot() -> dict:
    """Fields suitable for CloudCommunicator.status / dashboard."""
    return {
        "credential_store_backend": _backend,
        "credential_store_error": _last_error,
        # True when a secret will survive restart (OS keyring or encrypted file).
        "credential_store_ok": _backend in ("keyring", "encrypted_file"),
    }
