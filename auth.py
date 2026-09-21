from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from database import User, get_db
import os
import secrets
import time
import uuid
from pathlib import Path


def get_password_hash(password: str) -> str:
    # bcrypt limits to 72 bytes. Encode to bytes, truncate, then hash.
    pwd_bytes = password.encode('utf-8')[:72]
    return bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    # bcrypt limits to 72 bytes. Encode to bytes, truncate, then verify.
    pwd_bytes = plain_password.encode('utf-8')[:72]
    hashed_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(pwd_bytes, hashed_bytes)


# ---------------------------------------------------------------------------
# Signing key
#
# There is deliberately NO built-in default. A key that ships in the source is
# a key everybody has, and anyone holding it can mint an admin login for any
# install that forgot to override it.
#
# Order: the SECRET_KEY environment variable (start.sh exports it from .env),
# then the .env file beside this module, then - if neither exists - a fresh
# random key written to .env so it survives restarts. That last step is what
# keeps `python main.py` and reset_password.py safe when they are run
# directly, without start.sh around them.
# ---------------------------------------------------------------------------
_PLACEHOLDER_PREFIX = "your-super-secret"
_ENV_FILE = Path(__file__).resolve().parent / ".env"


def _usable(key: Optional[str]) -> bool:
    key = (key or "").strip()
    return len(key) >= 16 and not key.startswith(_PLACEHOLDER_PREFIX)


def _read_env_file() -> Optional[str]:
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SECRET_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return None


def _load_secret_key() -> str:
    key = os.environ.get("SECRET_KEY")
    if _usable(key):
        return key.strip()
    key = _read_env_file()
    if _usable(key):
        return key
    key = secrets.token_hex(32)
    try:
        with open(_ENV_FILE, "a", encoding="utf-8") as f:
            f.write(f"SECRET_KEY={key}\n")
        try:
            os.chmod(_ENV_FILE, 0o600)
        except OSError:
            pass
    except OSError:
        # Read-only install folder: this run still gets a random key, it just
        # cannot be remembered, so every restart signs everyone out.
        print("  [!] Could not save a signing key to .env - "
              "sessions will not survive a restart.", flush=True)
    return key


SECRET_KEY = _load_secret_key()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440  # 24 hours

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")


def _now_ms() -> int:
    return int(time.time() * 1000)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    jti = str(uuid.uuid4())
    # iat_ms is what session revocation compares against (see revoke_sessions).
    to_encode.update({"exp": expire, "jti": jti, "iat_ms": _now_ms()})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return {"token": encoded_jwt, "jti": jti}


def revoke_sessions(user: User):
    """Invalidate every token this user holds right now.

    Records the moment of revocation on the user row; any token issued before
    it is refused. Tokens issued afterwards (the next sign-in) work normally,
    so this signs a person out of every device without stopping them from
    signing back in. The caller commits.

    (`session_token` is an existing column. It used to hold a hash that nothing
    ever wrote, so "force sign-out" silently did nothing. It now holds the
    revocation time in milliseconds.)
    """
    user.session_token = str(_now_ms())


def _revoked_before_ms(user: User) -> int:
    try:
        return int(user.session_token) if user.session_token else 0
    except (TypeError, ValueError):
        return 0


def authenticate_user(db: Session, username: str, password: str):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        return False
    if not verify_password(password, user.hashed_password):
        return False
    if user.is_active is False:
        return False
    return user

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    # sync def -> FastAPI runs this in a worker thread, so the blocking DB
    # query below never stalls the event loop for every other user.
    return get_user_from_token(token, db)

def get_user_from_token(token: str, db: Session, check_session: bool = True):
    """Resolve a bearer/query token to a live, active user.

    `check_session` is kept so existing callers keep working, but revocation
    is now enforced on every path - including the ?token= URLs used by the
    video player, which used to skip it and so outlived a sign-out.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        jti: str = payload.get("jti")
        if username is None or jti is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception

    if user.is_active is False:
        raise credentials_exception

    revoked_before = _revoked_before_ms(user)
    if revoked_before:
        try:
            issued = int(payload.get("iat_ms") or 0)
        except (TypeError, ValueError):
            issued = 0
        # Tokens minted before revocation - including old-format tokens that
        # carry no timestamp at all - are refused.
        if issued < revoked_before:
            raise credentials_exception
    return user

def get_admin_user(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user
