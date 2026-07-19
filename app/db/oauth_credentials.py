import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials

from app.config.settings import get_settings
from app.db.google_clients import SCOPES


def missing_google_scopes(granted_scopes) -> list[str]:
    granted = set(granted_scopes or [])
    return sorted(set(SCOPES) - granted)


def _key(value: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest())


def _fernets() -> list[Fernet]:
    settings = get_settings()
    configured = [
        item.strip() for item in settings.oauth_encryption_keys.split(",")
        if item.strip()
    ]
    secrets = configured or [settings.jwt_secret_key]
    return [Fernet(_key(secret)) for secret in secrets]


def _fernet() -> MultiFernet:
    return MultiFernet(_fernets())


async def save_google_credentials(pool, email: str, credentials: Credentials):
    payload = _fernet().encrypt(credentials.to_json().encode()).decode()
    scopes = list(credentials.scopes or SCOPES)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO google_oauth_credentials
               (user_id,email,encrypted_credentials,granted_scopes,updated_at)
               VALUES($1,$1,$2,$3,now())
               ON CONFLICT(user_id) DO UPDATE SET
                 email=EXCLUDED.email,
                 encrypted_credentials=EXCLUDED.encrypted_credentials,
                 granted_scopes=EXCLUDED.granted_scopes,
                 updated_at=now()""",
            email,
            payload,
            scopes,
        )


async def load_google_credentials(pool, user_id: str) -> Credentials | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT encrypted_credentials,granted_scopes
               FROM google_oauth_credentials WHERE user_id=$1""",
            user_id,
        )
    if not row or missing_google_scopes(row["granted_scopes"]):
        return None
    encrypted = row["encrypted_credentials"].encode()
    info = json.loads(_fernet().decrypt(encrypted).decode())
    try:
        _fernets()[0].decrypt(encrypted)
    except InvalidToken:
        rotated = _fernet().rotate(encrypted).decode()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE google_oauth_credentials SET encrypted_credentials=$1,
                   updated_at=now() WHERE user_id=$2""",
                rotated, user_id,
            )
    # Scope expansion is only legal through a new interactive consent grant.
    # Reusing SCOPES here would make older refresh tokens fail with invalid_scope.
    credentials = Credentials.from_authorized_user_info(info)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleRequest())
        await save_google_credentials(pool, user_id, credentials)
    return credentials


async def google_connection_status(pool, user_id: str) -> tuple[bool, list[str]]:
    async with pool.acquire() as conn:
        granted = await conn.fetchval(
            "SELECT granted_scopes FROM google_oauth_credentials WHERE user_id=$1",
            user_id,
        )
    if granted is None:
        return False, list(SCOPES)
    missing = missing_google_scopes(granted)
    return not missing, missing


async def delete_google_credentials(pool, user_id: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM google_oauth_credentials WHERE user_id=$1", user_id
        )
