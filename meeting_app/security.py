import base64
import ctypes
import hashlib
import hmac
import os
import secrets
from ctypes import wintypes

from fastapi import HTTPException, Request
from .db import connect


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise HTTPException(400, "Пароль должен содержать не менее 10 символов")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return base64.b64encode(salt + digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        raw = base64.b64decode(encoded)
        return hmac.compare_digest(hashlib.scrypt(password.encode(), salt=raw[:16], n=2**14, r=8, p=1), raw[16:])
    except (ValueError, TypeError):
        return False


def current_user(request: Request):
    token = request.cookies.get("meeting_session", "")
    if not token:
        raise HTTPException(401, "Требуется вход")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connect() as db:
        row = db.execute("SELECT u.*, s.csrf FROM sessions s JOIN users u ON u.id=s.user_id "
                         "WHERE s.token_hash=? AND s.expires_at > datetime('now')", (token_hash,)).fetchone()
    if not row:
        raise HTTPException(401, "Сессия истекла")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-csrf-token") != row["csrf"]:
            raise HTTPException(403, "Недействительный токен запроса")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Недопустимый источник запроса")
    return row


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Защита ключа поддерживается только в Windows")
    source = ctypes.create_string_buffer(data)
    src = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    dst = _Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    args = (ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)) if decrypt else (
        ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst))
    if not fn(*args):
        raise RuntimeError("Не удалось открыть локальную конфигурацию")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def protect(data: str) -> bytes:
    return _protect(data.encode())


def unprotect(data: bytes) -> str:
    return _protect(data, True).decode()
