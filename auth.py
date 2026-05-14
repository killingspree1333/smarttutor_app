from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import hashlib, os

SECRET_KEY = "smarttutor-super-secret-key-2026"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 10080

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверить пароль"""
    return get_password_hash(plain_password) == hashed_password

def get_password_hash(password: str) -> str:
    """Хешировать пароль с SHA256 + соль"""
    salt = "smarttutor_salt_2026"
    return hashlib.sha256((password + salt).encode()).hexdigest()

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Создать JWT токен"""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    """Декодировать JWT токен"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None