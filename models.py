from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime

# ───── AUTH ─────

class UserRegister(BaseModel):
    email: EmailStr
    username: str
    password: str
    full_name: Optional[str] = None
    group_name: Optional[str] = None

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    username: str
    email: str

# ───── PROFILE ─────

class UserProfile(BaseModel):
    id: int
    email: str
    username: str
    full_name: Optional[str]
    group_name: Optional[str]
    avatar_url: Optional[str]
    is_active: bool
    created_at: datetime

class UserProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    group_name: Optional[str] = None
    avatar_url: Optional[str] = None

# ───── CHAT ─────

class MessageCreate(BaseModel):
    session_id: Optional[int] = None
    content: str
    subject: Optional[str] = None   # предмет для промпт-инжиниринга

class MessageResponse(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    created_at: datetime

class ChatSessionResponse(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime
