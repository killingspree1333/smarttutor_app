from fastapi import FastAPI, HTTPException, Depends, Header, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from typing import Optional
import os, httpx, re, asyncio
from dotenv import load_dotenv

from database import sb_get, sb_get_one, sb_insert, sb_update, SUPABASE_URL, HEADERS
from auth import verify_password, get_password_hash, create_access_token, decode_token
from models import UserRegister, UserLogin, TokenResponse, UserProfileUpdate, MessageCreate

load_dotenv()

# ─── Системные настройки (хранятся в памяти) ───────────────────────────────
_system_settings = {
    "registration_enabled": True,
    "maintenance_mode": False,
    "qa_api_key": os.getenv("QA_API_KEY", "smarttutor_qa_2025"),
    "gigachat_temperature": 0.7,
    "max_tokens": 2000,
}
_ip_bans = {}       # {ip: {"until": timestamp, "reason": ""}}
_prompt_overrides = {}  # {mode: custom_text}

app = FastAPI(title="SmartTutor API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

_gigachat_token = None
_gigachat_token_expires = 0

# ─── РЕЖИМЫ ───
MODE_FREE_CHAT = "free_chat"
MODE_PLAN_GENERATOR = "plan_generator"
MODE_STUDY_STEP = "study_step"
MODE_FINAL_TEST = "final_test"

# Слова которые запускают генерацию плана
PLAN_TRIGGERS = ["создать тему", "хочу план", "давай план", "по плану", "структурированно", "начать обучение", "создай план"]

def detect_mode(content: str, is_topic_chat: bool, current_step: int, total_steps: int, has_topic: bool) -> str:
    """Backend определяет режим — модель не угадывает"""
    content_lower = content.lower()

    # Если есть тема и идёт обучение
    if is_topic_chat and has_topic:
        if current_step >= total_steps:
            return MODE_FINAL_TEST
        return MODE_STUDY_STEP

    # Если пользователь хочет план
    if any(trigger in content_lower for trigger in PLAN_TRIGGERS):
        return MODE_PLAN_GENERATOR

    return MODE_FREE_CHAT

def get_system_prompt(mode: str, **kwargs) -> str:
    """Возвращает нужный промпт для режима"""
    if mode in _prompt_overrides and _prompt_overrides[mode]:
        p = _prompt_overrides[mode]
        p = p.replace("{username}", kwargs.get("username", "студент"))
        p = p.replace("{topic_title}", kwargs.get("topic_title", ""))
        return p
    username = kwargs.get("username", "студент")
    topic_title = kwargs.get("topic_title", "")

    if mode == MODE_FREE_CHAT:
        from prompts.free_chat_prompt import get_prompt
        return get_prompt(
            username=username,
            user_msg_count=kwargs.get("user_msg_count", 0),
            completed_topics=kwargs.get("completed_topics", [])
        )
    elif mode == MODE_PLAN_GENERATOR:
        from prompts.plan_generator_prompt import get_prompt
        return get_prompt(topic=kwargs.get("topic", topic_title), username=username)
    elif mode == MODE_STUDY_STEP:
        from prompts.study_step_prompt import get_prompt
        return get_prompt(
            topic_title=topic_title,
            username=username,
            current_step=kwargs.get("current_step", 1),
            step_title=kwargs.get("step_title", ""),
            total_steps=kwargs.get("total_steps", 5),
            completed_count=kwargs.get("completed_count", 0)
        )
    elif mode == MODE_FINAL_TEST:
        from prompts.final_test_prompt import get_prompt
        return get_prompt(topic_title=topic_title, username=username)
    return ""

def parse_steps_from_response(text: str) -> list:
    steps = []
    patterns = [
        r'#{1,3}\s*Шаг\s*(\d+)[:.]\s*(.+?)(?:\n|$)',
        r'\*\*Шаг\s*(\d+)[:.]\s*(.+?)\*\*',
        r'(?:^|\n)Шаг\s*(\d+)[:.]\s*(.+?)(?:\n|$)',
    ]
    seen = set()
    for pattern in patterns:
        matches = re.findall(pattern, text, re.MULTILINE)
        if matches:
            for num, title in matches:
                clean = title.strip().replace('*','').replace('#','').strip()
                n = int(num)
                if clean and len(clean) > 2 and n not in seen:
                    seen.add(n)
                    steps.append({"number": n, "title": clean})
            if steps: break
    return sorted(steps, key=lambda x: x["number"])

def sb_delete(table: str, filters: dict):
    import requests as req
    params = {k: f"eq.{v}" for k, v in filters.items()}
    req.delete(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=params)

def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Не авторизован")
    token = authorization.split(" ")[1]
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Токен недействителен")
    results = sb_get("users", {"id": payload.get("sub")})
    if not results:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return results[0]

# ─── СТАТИКА ───
@app.get("/")
def serve_index():
    return FileResponse("index.html")

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}

@app.get("/favicon.png")
def favicon():
    if os.path.exists("SmartTutor.png"):
        return FileResponse("SmartTutor.png", media_type="image/png")
    raise HTTPException(status_code=404)

# Хранилища кодов верификации
_verify_codes = {}   # Верификация email при регистрации

# ─── АВТОРИЗАЦИЯ ───
@app.middleware("http")
async def maintenance_middleware(request, call_next):
    import time as _t
    client_ip = request.client.host if request.client else ""
    if client_ip and client_ip in _ip_bans:
        ban = _ip_bans[client_ip]
        if ban.get("until", 0) > _t.time():
            if not request.url.path.startswith("/admin") and request.url.path not in ["/auth/login"]:
                from fastapi.responses import JSONResponse
                from datetime import datetime as _dt
                until_str = _dt.utcfromtimestamp(ban["until"]).strftime("%d.%m.%Y")
                return JSONResponse({"detail": f"Ваш IP заблокирован до {until_str}. Причина: {ban.get('reason','—')}"}, status_code=403)
        else:
            del _ip_bans[client_ip]
    if _system_settings.get("maintenance_mode") and not request.url.path.startswith("/admin") and request.url.path not in ["/", "/health", "/favicon.png"]:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Сервис на техническом обслуживании. Попробуйте позже."}, status_code=503)
    return await call_next(request)

@app.post("/auth/register")
async def register(data: UserRegister, request: Request, background_tasks: BackgroundTasks):
    import random, time
    if not _system_settings.get("registration_enabled", True):
        raise HTTPException(status_code=403, detail="Регистрация временно отключена администратором")
    existing = sb_get("users", {"email": data.email})
    if existing:
        if existing[0].get("is_active"):
            raise HTTPException(status_code=400, detail="Email уже зарегистрирован")
        # Аккаунт есть но не подтверждён — обновляем пароль и шлём новый код
        user = existing[0]
        sb_update("users", {"hashed_password": get_password_hash(data.password)}, {"id": user["id"]})
    else:
        if sb_get("users", {"username": data.username}):
            raise HTTPException(status_code=400, detail="Имя пользователя уже занято")
        # Создаём пользователя неактивным до подтверждения email
        user = sb_insert("users", {
            "email": data.email,
            "username": data.username,
            "hashed_password": get_password_hash(data.password),
            "is_active": False
        })
    sb_insert("subscriptions", {"user_id": user["id"], "plan": "free", "is_active": True})
    # Генерируем и отправляем код верификации
    code = str(random.randint(100000, 999999))
    _verify_codes[data.email] = {"code": code, "user_id": user["id"], "expires": time.time() + 600}
    print(f"[VERIFY] Код для {data.email}: {code}")
    # Пробуем отправить email, если не получилось — вернём код в ответе
    sent = False
    try:
        import asyncio as _aio
        sent = _aio.get_event_loop().run_until_complete(
            send_email(data.email,
                "SmartTutor — подтвердите аккаунт",
                f"Добро пожаловать в SmartTutor!\n\nВаш код подтверждения: {code}\n\nКод действителен 10 минут.")
        )
    except Exception:
        pass
    resp = {"status": "verification_required", "email": data.email}
    if not sent:
        resp["dev_code"] = code  # показываем код если email не дошёл
    return resp

@app.post("/auth/verify-email", response_model=TokenResponse)
def verify_email(data: dict, request: Request):
    import time
    email = data.get("email", "").strip().lower()
    code = data.get("code", "").strip()
    stored = _verify_codes.get(email)
    if not stored:
        raise HTTPException(status_code=400, detail="Код не найден. Зарегистрируйтесь снова.")
    if time.time() > stored["expires"]:
        del _verify_codes[email]
        raise HTTPException(status_code=400, detail="Код истёк. Зарегистрируйтесь снова.")
    if stored["code"] != code:
        raise HTTPException(status_code=400, detail="Неверный код")
    user_id = stored["user_id"]
    sb_update("users", {"is_active": True}, {"id": user_id})
    del _verify_codes[email]
    users = sb_get("users", {"id": user_id})
    user = users[0]
    sb_insert("login_logs", {"user_id": user_id, "ip_address": request.client.host, "success": True})
    token = create_access_token({"sub": str(user_id)})
    return TokenResponse(access_token=token, user_id=user_id, username=user["username"], email=user["email"])

@app.post("/auth/resend-verification")
async def resend_verification(data: dict, background_tasks: BackgroundTasks):
    import random, time
    email = data.get("email", "").strip().lower()
    users = sb_get("users", {"email": email})
    if not users or users[0].get("is_active"):
        return {"message": "ok"}
    code = str(random.randint(100000, 999999))
    _verify_codes[email] = {"code": code, "user_id": users[0]["id"], "expires": time.time() + 600}
    print(f"[VERIFY RESEND] Код для {email}: {code}")
    background_tasks.add_task(send_email_smtp, email,
        "SmartTutor — код подтверждения",
        f"Ваш новый код: {code}\n\nКод действителен 10 минут."
    )
    return {"message": "Код отправлен повторно"}

@app.post("/auth/google", response_model=TokenResponse)
async def google_auth(data: dict, request: Request):
    import secrets
    credential = data.get("credential", "")
    if not credential:
        raise HTTPException(status_code=400, detail="Нет Google токена")
    # Верифицируем токен через Google API (без доп. библиотек)
    try:
        r = await httpx.AsyncClient().get(
            f"https://oauth2.googleapis.com/tokeninfo?id_token={credential}",
            timeout=10.0
        )
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail="Неверный Google токен")
        info = r.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Ошибка проверки Google токена")
    email = info.get("email", "")
    name = info.get("name", "") or info.get("given_name", "")
    google_id = info.get("sub", "")
    if not email:
        raise HTTPException(status_code=400, detail="Не удалось получить email от Google")
    # Ищем пользователя по email
    users = sb_get("users", {"email": email})
    if users:
        user = users[0]
        # Активируем если не активен
        if not user.get("is_active"):
            sb_update("users", {"is_active": True}, {"id": user["id"]})
    else:
        # Создаём нового пользователя
        base_username = email.split("@")[0].replace(".", "_").replace("-", "_")[:20]
        username = base_username
        counter = 1
        while sb_get("users", {"username": username}):
            username = f"{base_username}{counter}"
            counter += 1
        user = sb_insert("users", {
            "email": email,
            "username": username,
            "hashed_password": get_password_hash(secrets.token_hex(16)),
            "full_name": name,
            "is_active": True
        })
        sb_insert("subscriptions", {"user_id": user["id"], "plan": "free", "is_active": True})
    sb_insert("login_logs", {"user_id": user["id"], "ip_address": request.client.host, "success": True})
    token = create_access_token({"sub": str(user["id"])})
    return TokenResponse(access_token=token, user_id=user["id"], username=user["username"], email=user["email"])

@app.post("/auth/login", response_model=TokenResponse)
def login(data: UserLogin, request: Request):
    results = sb_get("users", {"email": data.email})
    user = results[0] if results else None
    if not user or not verify_password(data.password, user["hashed_password"]):
        if user: sb_insert("login_logs", {"user_id": user["id"], "ip_address": request.client.host, "success": False})
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    if not user.get("is_active"):
        # Генерируем новый код и отправляем/возвращаем его
        import random, time as _time
        code = str(random.randint(100000, 999999))
        _verify_codes[data.email] = {"code": code, "user_id": user["id"], "expires": _time.time() + 600}
        print(f"[VERIFY LOGIN] Код для {data.email}: {code}")
        sent = False
        try:
            import asyncio as _aio
            sent = _aio.get_event_loop().run_until_complete(
                send_email(data.email, "SmartTutor — подтвердите аккаунт",
                    f"Ваш код подтверждения SmartTutor: {code}\n\nКод действителен 10 минут.")
            )
        except Exception:
            pass
        detail = f"UNVERIFIED:{data.email}"
        if not sent:
            detail = f"UNVERIFIED_CODE:{data.email}:{code}"
        raise HTTPException(status_code=403, detail=detail)
    sb_insert("login_logs", {"user_id": user["id"], "ip_address": request.client.host, "success": True})
    token = create_access_token({"sub": str(user["id"])})
    return TokenResponse(access_token=token, user_id=user["id"], username=user["username"], email=user["email"])

# ─── ПРОФИЛЬ ───
@app.get("/profile/me")
def get_profile(current_user=Depends(get_current_user)):
    return {"id": current_user["id"], "email": current_user["email"], "username": current_user["username"], "full_name": current_user.get("full_name"), "avatar_url": current_user.get("avatar_url"), "is_active": current_user["is_active"], "is_admin": current_user.get("is_admin", False), "created_at": str(current_user.get("created_at", "")), "auth_provider": current_user.get("auth_provider", "email")}

@app.put("/profile/me")
def update_profile(data: UserProfileUpdate, current_user=Depends(get_current_user)):
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    if update_data: sb_update("users", update_data, {"id": current_user["id"]})
    return {"message": "ok"}

@app.put("/profile/password")
def change_password(data: dict, current_user=Depends(get_current_user)):
    password = data.get("password", "")
    if len(password) < 6: raise HTTPException(status_code=400, detail="Пароль минимум 6 символов")
    sb_update("users", {"hashed_password": get_password_hash(password)}, {"id": current_user["id"]})
    return {"message": "ok"}

# Временное хранилище кодов (в продакшене — Redis или БД)
_email_codes = {}
_reset_codes = {}  # Коды сброса пароля

async def send_email(to_email: str, subject: str, body: str) -> bool:
    """Отправка через Resend API. Возвращает True если успешно."""
    api_key = os.getenv("RESEND_API_KEY", "")
    print(f"[EMAIL] Отправка на {to_email}: {subject}")
    if not api_key:
        print(f"[DEV] Нет RESEND_API_KEY.")
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"from": "SmartTutor <onboarding@resend.dev>", "to": [to_email], "subject": subject, "text": body}
            )
            if r.status_code in (200, 201):
                print(f"[EMAIL] Отправлено на {to_email} ✓")
                return True
            else:
                print(f"[EMAIL ERROR] Resend: {r.status_code} {r.text}")
                return False
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False

# Обратная совместимость для BackgroundTasks (синхронный wrapper)
def send_email_smtp(to_email: str, subject: str, body: str):
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(send_email(to_email, subject, body))
        loop.close()
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")

@app.post("/auth/forgot-password")
async def forgot_password(data: dict, background_tasks: BackgroundTasks):
    import random, time
    email = data.get("email", "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Введите корректный email")
    users = sb_get("users", {"email": email})
    # Всегда возвращаем успех чтобы не раскрывать существование email
    if users:
        code = str(random.randint(100000, 999999))
        _reset_codes[email] = {"code": code, "user_id": users[0]["id"], "expires": time.time() + 600}
        print(f"[RESET CODE] {email} → {code}")
        sent = False
        try:
            import asyncio as _aio
            sent = _aio.get_event_loop().run_until_complete(
                send_email(email,
                    "SmartTutor — восстановление пароля",
                    f"Ваш код для сброса пароля SmartTutor: {code}\n\nКод действителен 10 минут.")
            )
        except Exception:
            pass
        resp = {"message": "Если email зарегистрирован, код отправлен"}
        if not sent:
            resp["dev_code"] = code
        return resp
    return {"message": "Если email зарегистрирован, код отправлен"}

@app.post("/auth/reset-password/check")
def check_reset_code(data: dict):
    """Проверяет код без смены пароля — для шага 2 формы"""
    import time
    email = data.get("email", "").strip().lower()
    code = data.get("code", "").strip()
    stored = _reset_codes.get(email)
    if not stored:
        raise HTTPException(status_code=400, detail="Код не найден. Запросите новый.")
    if time.time() > stored["expires"]:
        del _reset_codes[email]
        raise HTTPException(status_code=400, detail="Код истёк. Запросите новый.")
    if stored["code"] != code:
        raise HTTPException(status_code=400, detail="Неверный код")
    return {"message": "ok"}

@app.post("/auth/reset-password")
def reset_password(data: dict):
    import time
    email = data.get("email", "").strip().lower()
    code = data.get("code", "").strip()
    new_password = data.get("password", "")
    if not email or not code or not new_password:
        raise HTTPException(status_code=400, detail="Заполните все поля")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Пароль минимум 6 символов")
    stored = _reset_codes.get(email)
    if not stored:
        raise HTTPException(status_code=400, detail="Код не найден. Запросите новый.")
    if time.time() > stored["expires"]:
        del _reset_codes[email]
        raise HTTPException(status_code=400, detail="Код истёк. Запросите новый.")
    if stored["code"] != code:
        raise HTTPException(status_code=400, detail="Неверный код")
    sb_update("users", {"hashed_password": get_password_hash(new_password)}, {"id": stored["user_id"]})
    del _reset_codes[email]
    return {"message": "Пароль успешно изменён"}

@app.post("/profile/email/send-code")
async def send_email_code(data: dict, background_tasks: BackgroundTasks, current_user=Depends(get_current_user)):
    import random, smtplib, time
    from email.mime.text import MIMEText
    new_email = data.get("email", "").strip().lower()
    if not new_email or "@" not in new_email:
        raise HTTPException(status_code=400, detail="Некорректный email")
    # Проверяем что такой email не занят
    existing = sb_get("users", {"email": new_email})
    if existing and existing[0]["id"] != current_user["id"]:
        raise HTTPException(status_code=400, detail="Email уже используется")
    # Генерируем 6-значный код
    code = str(random.randint(100000, 999999))
    _email_codes[current_user["id"]] = {"code": code, "email": new_email, "expires": time.time() + 600}
    # Отправляем письмо через Gmail SMTP
    smtp_email = os.getenv("SMTP_EMAIL", "")
    smtp_pass = os.getenv("SMTP_PASSWORD", "")
    print(f"[EMAIL CHANGE] Код для {new_email}: {code}")
    background_tasks.add_task(send_email_smtp, new_email,
        "SmartTutor — код подтверждения",
        f"Ваш код для смены email в SmartTutor: {code}\n\nКод действует 10 минут."
    )
    return {"message": "Код отправлен"}

@app.post("/profile/email/verify-code")
def verify_email_code(data: dict, current_user=Depends(get_current_user)):
    import time
    code = data.get("code", "").strip()
    stored = _email_codes.get(current_user["id"])
    if not stored:
        raise HTTPException(status_code=400, detail="Код не найден. Запросите новый.")
    if time.time() > stored["expires"]:
        del _email_codes[current_user["id"]]
        raise HTTPException(status_code=400, detail="Код истёк. Запросите новый.")
    if stored["code"] != code:
        raise HTTPException(status_code=400, detail="Неверный код")
    # Меняем email
    new_email = stored["email"]
    sb_update("users", {"email": new_email}, {"id": current_user["id"]})
    del _email_codes[current_user["id"]]
    return {"message": "Email успешно изменён", "email": new_email}

@app.get("/profile/progress")
def get_progress(current_user=Depends(get_current_user)):
    progress = sb_get("learning_progress", {"user_id": current_user["id"]}) or []
    messages = sb_get("messages", {"user_id": current_user["id"]}) or []
    return {"subjects": progress, "total_questions": len([m for m in messages if m.get("role") == "user"])}

@app.get("/subscription")
def get_subscription(current_user=Depends(get_current_user)):
    subs = sb_get("subscriptions", {"user_id": current_user["id"]}) or []
    return subs[0] if subs else {"plan": "free", "is_active": True}

# ─── ЧАТЫ ───
@app.get("/chat/sessions")
def get_sessions(current_user=Depends(get_current_user)):
    return sb_get("chat_sessions", {"user_id": current_user["id"]}) or []

@app.post("/chat/sessions")
def create_session(current_user=Depends(get_current_user)):
    return sb_insert("chat_sessions", {"user_id": current_user["id"], "title": "Новый чат", "is_deleted": False})

@app.patch("/chat/sessions/{session_id}")
def update_session(session_id: int, data: dict, current_user=Depends(get_current_user)):
    sb_update("chat_sessions", data, {"id": session_id})
    return {"message": "ok"}

@app.get("/chat/sessions/{session_id}/messages")
def get_messages(session_id: int, current_user=Depends(get_current_user)):
    msgs = sb_get("messages", {"session_id": session_id}) or []
    return sorted(msgs, key=lambda x: x.get("created_at", ""))

# ─── ГЛАВНЫЙ ЭНДПОИНТ ───
@app.post("/chat/send")
async def send_message(data: MessageCreate, current_user=Depends(get_current_user)):
    global _gigachat_token, _gigachat_token_expires

    # Создаём сессию если нет
    if not data.session_id:
        session = sb_insert("chat_sessions", {"user_id": current_user["id"], "title": data.content[:50], "is_deleted": False})
        session_id = session["id"]
    else:
        session_id = data.session_id

    # Сохраняем сообщение
    sb_insert("messages", {"session_id": session_id, "user_id": current_user["id"], "role": "user", "content": data.content})

    # Получаем данные сессии
    session_info = sb_get_one("chat_sessions", {"id": session_id})
    topic_title = session_info.get("title", "") if session_info else ""
    is_named_chat = topic_title and topic_title not in ["Новый чат", ""] and len(topic_title) < 80

    # Получаем тему если есть
    topic_data = None
    current_step = 1
    step_title = ""
    total_steps = 5
    completed_count = 0

    if is_named_chat:
        topics = sb_get("topics", {"session_id": session_id})
        if topics:
            topic_data = topics[0]
            total_steps = topic_data.get("total_steps") or 5
            current_step = topic_data.get("current_step") or 1
            steps = sb_get("topic_steps", {"topic_id": topic_data["id"]}) or []
            active = [s for s in steps if s.get("status") == "active"]
            done = [s for s in steps if s.get("status") == "completed"]
            completed_count = len(done)
            if active:
                current_step = active[0].get("step_number", current_step)
                step_title = active[0].get("title", "")

    # Определяем режим
    all_msgs = sb_get("messages", {"session_id": session_id}) or []
    user_msg_count = len([m for m in all_msgs if m.get("role") == "user"])
    username = current_user.get("full_name") or current_user.get("username") or "студент"

    mode = detect_mode(
        content=data.content,
        is_topic_chat=is_named_chat and topic_data is not None,
        current_step=current_step,
        total_steps=total_steps,
        has_topic=topic_data is not None
    )

    # Получаем промпт для режима
    completed_topics = [t["title"] for t in (sb_get("topics", {"user_id": current_user["id"]}) or []) if t.get("status") == "completed"]

    # Если режим PLAN_GENERATOR — определяем тему из сообщения
    topic_for_plan = data.content
    if mode == MODE_PLAN_GENERATOR:
        # Убираем триггерные слова чтобы получить чистую тему
        for trigger in PLAN_TRIGGERS:
            topic_for_plan = topic_for_plan.lower().replace(trigger, "").strip()
        if not topic_for_plan or len(topic_for_plan) < 3:
            topic_for_plan = topic_title if is_named_chat else data.content[:50]

    system_prompt = get_system_prompt(
        mode,
        username=username,
        topic_title=topic_title,
        topic=topic_for_plan,
        user_msg_count=user_msg_count,
        completed_topics=completed_topics,
        current_step=current_step,
        step_title=step_title,
        total_steps=total_steps,
        completed_count=completed_count
    )

    # История (последние 16 сообщений)
    history = sorted(all_msgs, key=lambda x: x.get("created_at", ""))[-16:]
    messages_for_api = [{"role": "system", "content": system_prompt}]
    for msg in history[:-1]:
        messages_for_api.append({"role": msg["role"], "content": msg["content"]})
    messages_for_api.append({"role": "user", "content": data.content})

    # Запрос к GigaChat
    auth_key = os.getenv("GIGACHAT_AUTH_KEY", "")
    ai_response = "ИИ временно недоступен."

    if auth_key:
        try:
            import uuid, time
            now = time.time()
            if not _gigachat_token or now >= _gigachat_token_expires:
                tr = await httpx.AsyncClient(verify=False).post(
                    "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                    headers={"Authorization": f"Basic {auth_key}", "Content-Type": "application/x-www-form-urlencoded", "RqUID": str(uuid.uuid4())},
                    data={"scope": "GIGACHAT_API_PERS"}, timeout=15.0
                )
                td = tr.json()
                _gigachat_token = td["access_token"]
                _gigachat_token_expires = td.get("expires_at", 0) / 1000 - 60

            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.post(
                    "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {_gigachat_token}", "Content-Type": "application/json"},
                    json={"model": "GigaChat-Pro", "messages": messages_for_api, "temperature": 0.7, "max_tokens": 2000},
                    timeout=30.0
                )
                ai_response = resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            _gigachat_token = None
            ai_response = f"Ошибка ИИ: {str(e)}"

    # Сохраняем ответ
    sb_insert("messages", {"session_id": session_id, "user_id": current_user["id"], "role": "assistant", "content": ai_response})

    # ─── СИНХРОНИЗАЦИЯ ───

    # Если был режим PLAN_GENERATOR и ответ содержит план — создаём тему
    if mode == MODE_PLAN_GENERATOR and "ТЕМА:" in ai_response:
        topic_match = re.search(r'ТЕМА:\s*(.+?)(?:\n|$)', ai_response)
        if topic_match:
            topic_name = topic_match.group(1).strip().replace("*", "").replace("#", "")
            existing = sb_get("topics", {"session_id": session_id}) or []
            if not existing:
                steps = parse_steps_from_response(ai_response)
                if not steps:
                    steps = [{"number":1,"title":"Введение и теория"},{"number":2,"title":"Примеры и практика"},{"number":3,"title":"Самостоятельное задание"},{"number":4,"title":"Проверка знаний"},{"number":5,"title":"Итоговый тест"}]
                topic = sb_insert("topics", {"user_id": current_user["id"], "session_id": session_id, "title": topic_name, "subject": topic_name, "status": "active", "progress": 0, "current_step": 1, "total_steps": len(steps)})
                for i, step in enumerate(steps):
                    sb_insert("topic_steps", {"topic_id": topic["id"], "user_id": current_user["id"], "step_number": step["number"], "title": step["title"], "status": "active" if i == 0 else "pending"})
                # Переименовываем чат
                sb_update("chat_sessions", {"title": topic_name}, {"id": session_id})

    # Если засчитан шаг
    if "ШАГ ЗАСЧИТАН" in ai_response and topic_data:
        tid = topic_data["id"]
        steps = sb_get("topic_steps", {"topic_id": tid}) or []
        active_s = [s for s in steps if s.get("status") == "active"]
        if active_s:
            cur = sorted(active_s, key=lambda x: x["step_number"])[0]
            sb_update("topic_steps", {"status": "completed"}, {"id": cur["id"]})
            nxt = [s for s in steps if s.get("step_number") == cur["step_number"] + 1]
            if nxt: sb_update("topic_steps", {"status": "active"}, {"id": nxt[0]["id"]})
        fresh = sb_get("topic_steps", {"topic_id": tid}) or []
        done_c = len([s for s in fresh if s.get("status") == "completed"])
        total = topic_data.get("total_steps") or 5
        sb_update("topics", {"progress": min(95, round(done_c/total*100)), "current_step": done_c+1}, {"id": tid})

    # Если тема завершена
    if "ТЕМА ЗАВЕРШЕНА" in ai_response and topic_data:
        sb_update("topics", {"status": "completed", "progress": 100}, {"id": topic_data["id"]})

    return {"session_id": session_id, "answer": ai_response, "mode": mode}

# ─── ТЕМЫ ───
@app.get("/topics")
def get_topics(current_user=Depends(get_current_user)):
    topics = sb_get("topics", {"user_id": current_user["id"]}) or []
    result = []
    for t in topics:
        steps = sb_get("topic_steps", {"topic_id": t["id"]}) or []
        result.append({**t, "steps": sorted(steps, key=lambda x: x.get("step_number", 0))})
    return result

@app.post("/topics")
def create_topic(data: dict, current_user=Depends(get_current_user)):
    sid = data.get("session_id")
    if sid:
        ex = sb_get("topics", {"session_id": sid})
        if ex: return ex[0]
    msgs = sb_get("messages", {"session_id": sid}) or [] if sid else []
    steps = []
    for msg in reversed(msgs):
        if msg.get("role") == "assistant":
            steps = parse_steps_from_response(msg.get("content", ""))
            if steps: break
    if not steps:
        steps = [{"number":1,"title":"Введение"},{"number":2,"title":"Практика"},{"number":3,"title":"Задание"},{"number":4,"title":"Проверка"},{"number":5,"title":"Тест"}]
    topic = sb_insert("topics", {"user_id": current_user["id"], "session_id": sid, "title": data.get("title"), "subject": data.get("subject"), "status": "active", "progress": 0, "current_step": 1, "total_steps": len(steps)})
    for i, s in enumerate(steps):
        sb_insert("topic_steps", {"topic_id": topic["id"], "user_id": current_user["id"], "step_number": s["number"], "title": s["title"], "status": "active" if i==0 else "pending"})
    return topic

@app.patch("/topics/{topic_id}")
def update_topic(topic_id: int, data: dict, current_user=Depends(get_current_user)):
    sb_update("topics", data, {"id": topic_id})
    return {"message": "ok"}

@app.delete("/topics/{topic_id}")
def delete_topic(topic_id: int, current_user=Depends(get_current_user)):
    sb_delete("topic_steps", {"topic_id": topic_id})
    sb_delete("topics", {"id": topic_id})
    return {"message": "ok"}

# ─── АКТИВНОСТЬ ───
@app.post("/activity/track")
def track_activity(current_user=Depends(get_current_user)):
    from datetime import date
    today = str(date.today())
    ex = sb_get("user_activity", {"user_id": current_user["id"], "date": today})
    if not ex: sb_insert("user_activity", {"user_id": current_user["id"], "date": today, "messages_count": 1})
    else: sb_update("user_activity", {"messages_count": ex[0].get("messages_count", 0)+1}, {"id": ex[0]["id"]})
    return {"ok": True}

@app.get("/activity")
def get_activity(current_user=Depends(get_current_user)):
    return sb_get("user_activity", {"user_id": current_user["id"]}) or []

# ─── ADMIN ───────────────────────────────────────────────────────────────────

def get_admin_user(current_user=Depends(get_current_user)):
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещён")
    return current_user

@app.get("/admin/stats")
def admin_stats(admin=Depends(get_admin_user)):
    import requests as req
    users = sb_get_all("users") or []
    sessions = sb_get_all("chat_sessions") or []
    messages = sb_get_all("messages") or []
    logs = sb_get_all("login_logs") or []
    topics = sb_get_all("topics") or []
    test_cases = sb_get_all("test_cases") or []
    return {
        "total_users": len(users),
        "active_users": len([u for u in users if u.get("is_active")]),
        "admin_users": len([u for u in users if u.get("is_admin")]),
        "total_sessions": len(sessions),
        "total_messages": len(messages),
        "total_topics": len(topics),
        "total_logins": len(logs),
        "successful_logins": len([l for l in logs if l.get("success")]),
        "total_test_cases": len(test_cases),
    }

@app.get("/admin/users")
def admin_get_users(admin=Depends(get_admin_user)):
    users = sb_get_all("users") or []
    result = []
    for u in users:
        msgs = sb_get("messages", {"user_id": u["id"]}) or []
        result.append({
            "id": u["id"],
            "email": u.get("email"),
            "username": u.get("username"),
            "full_name": u.get("full_name"),
            "is_active": u.get("is_active"),
            "is_admin": u.get("is_admin", False),
            "auth_provider": u.get("auth_provider", "email"),
            "created_at": str(u.get("created_at", "")),
            "messages_count": len([m for m in msgs if m.get("role") == "user"]),
        })
    return sorted(result, key=lambda x: x.get("created_at", ""), reverse=True)

@app.patch("/admin/users/{user_id}")
def admin_update_user(user_id: int, data: dict, admin=Depends(get_admin_user)):
    allowed = {"is_active", "is_admin", "full_name"}
    update = {k: v for k, v in data.items() if k in allowed}
    if "password" in data and data["password"]:
        update["hashed_password"] = get_password_hash(data["password"])
    if update:
        sb_update("users", update, {"id": user_id})
    return {"message": "ok"}

@app.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin=Depends(get_admin_user)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Нельзя удалить себя")
    sb_delete("users", {"id": user_id})
    return {"message": "ok"}

@app.get("/admin/logs")
def admin_get_logs(admin=Depends(get_admin_user)):
    logs = sb_get_all("login_logs") or []
    result = []
    for log in logs:
        user = (sb_get("users", {"id": log.get("user_id")}) or [{}])[0]
        result.append({
            **log,
            "email": user.get("email", "—"),
            "username": user.get("username", "—"),
        })
    return sorted(result, key=lambda x: x.get("attempted_at", ""), reverse=True)[:200]

@app.get("/admin/test-cases")
def admin_get_test_cases(admin=Depends(get_admin_user)):
    return sb_get_all("test_cases") or []

@app.post("/admin/test-cases")
def admin_create_test_case(data: dict, admin=Depends(get_admin_user)):
    case = sb_insert("test_cases", {
        "project_name": data.get("project_name", "SmartTutor"),
        "case_title": data.get("case_title", ""),
        "description": data.get("description", ""),
        "steps": data.get("steps", ""),
        "expected_result": data.get("expected_result", ""),
        "priority": data.get("priority", "medium"),
        "status": data.get("status", "not_run"),
        "source": data.get("source", "admin"),
    })
    return case

@app.patch("/admin/test-cases/{case_id}")
def admin_update_test_case(case_id: int, data: dict, admin=Depends(get_admin_user)):
    allowed = {"status", "case_title", "description", "steps", "expected_result", "priority"}
    update = {k: v for k, v in data.items() if k in allowed}
    if update:
        sb_update("test_cases", update, {"id": case_id})
    return {"message": "ok"}

@app.delete("/admin/test-cases/{case_id}")
def admin_delete_test_case(case_id: int, admin=Depends(get_admin_user)):
    sb_delete("test_cases", {"id": case_id})
    return {"message": "ok"}

# ─── QA TESTER INTEGRATION ───────────────────────────────────────────────────

QA_API_KEY = os.getenv("QA_API_KEY", "smarttutor_qa_key_2025")

@app.post("/qa/import")
def qa_import(data: dict):
    """Эндпоинт для импорта тест-кейсов из приложения QA Tester одногруппника"""
    if data.get("api_key") != QA_API_KEY:
        raise HTTPException(status_code=401, detail="Неверный API ключ")
    cases = data.get("cases", [])
    project = data.get("project_name", "QA Tester Import")
    imported = 0
    for case in cases:
        sb_insert("test_cases", {
            "project_name": project,
            "case_title": case.get("title", case.get("name", "Без названия")),
            "description": case.get("description", ""),
            "steps": case.get("steps", ""),
            "expected_result": case.get("expected_result", ""),
            "priority": case.get("priority", "medium"),
            "status": case.get("status", "not_run"),
            "source": "qa_tester",
        })
        imported += 1
    return {"message": f"Импортировано {imported} тест-кейсов", "imported": imported}

@app.get("/qa/export")
def qa_export(api_key: str = ""):
    """Экспорт тест-кейсов для QA Tester"""
    if api_key != QA_API_KEY:
        raise HTTPException(status_code=401, detail="Неверный API ключ")
    return sb_get_all("test_cases") or []

# ─── HELPERS ─────────────────────────────────────────────────────────────────

# ─── ADMIN: АНАЛИТИКА ────────────────────────────────────────────────────────

@app.get("/admin/analytics")
def admin_analytics(admin=Depends(get_admin_user)):
    from datetime import datetime, timedelta
    users = sb_get_all("users") or []
    messages = sb_get_all("messages") or []
    topics = sb_get_all("topics") or []
    sessions = sb_get_all("chat_sessions") or []

    # Регистрации по дням (14 дней)
    reg_by_day = {}
    msg_by_day = {}
    today = datetime.utcnow().date()
    for i in range(13, -1, -1):
        d = str(today - timedelta(days=i))
        reg_by_day[d] = 0
        msg_by_day[d] = 0

    for u in users:
        d = str(u.get("created_at", ""))[:10]
        if d in reg_by_day:
            reg_by_day[d] += 1

    for m in messages:
        if m.get("role") == "user":
            d = str(m.get("created_at", ""))[:10]
            if d in msg_by_day:
                msg_by_day[d] += 1

    # Топ тем
    topic_counts = {}
    for t in topics:
        name = t.get("subject") or t.get("title", "—")
        topic_counts[name] = topic_counts.get(name, 0) + 1
    top_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "registrations_by_day": reg_by_day,
        "messages_by_day": msg_by_day,
        "top_topics": [{"name": k, "count": v} for k, v in top_topics],
        "total_users": len(users),
        "total_messages": len([m for m in messages if m.get("role") == "user"]),
        "total_topics": len(topics),
        "total_sessions": len([s for s in sessions if not s.get("is_deleted")]),
    }

# ─── ADMIN: ПОДПИСКИ ─────────────────────────────────────────────────────────

@app.get("/admin/subscriptions")
def admin_get_subscriptions(admin=Depends(get_admin_user)):
    subs = sb_get_all("subscriptions") or []
    result = []
    for s in subs:
        user = (sb_get("users", {"id": s.get("user_id")}) or [{}])[0]
        result.append({**s, "email": user.get("email","—"), "username": user.get("username","—")})
    return sorted(result, key=lambda x: x.get("started_at",""), reverse=True)

@app.patch("/admin/subscriptions/{user_id}")
def admin_set_subscription(user_id: int, data: dict, admin=Depends(get_admin_user)):
    plan = data.get("plan", "free")
    existing = sb_get("subscriptions", {"user_id": user_id})
    if existing:
        sb_update("subscriptions", {"plan": plan, "is_active": True}, {"user_id": user_id})
    else:
        sb_insert("subscriptions", {"user_id": user_id, "plan": plan, "is_active": True})
    return {"message": f"Подписка пользователя изменена на {plan}"}

# ─── ADMIN: РАССЫЛКА ─────────────────────────────────────────────────────────

@app.post("/admin/broadcast")
async def admin_broadcast(data: dict, background_tasks: BackgroundTasks, admin=Depends(get_admin_user)):
    subject = data.get("subject", "Сообщение от SmartTutor")
    body = data.get("body", "")
    target = data.get("target", "all")  # all / active / pro
    if not body:
        raise HTTPException(400, "Текст письма не может быть пустым")
    users = sb_get_all("users") or []
    if target == "active":
        users = [u for u in users if u.get("is_active")]
    emails = [u["email"] for u in users if u.get("email")]
    for email in emails:
        background_tasks.add_task(send_email_smtp, email, subject, body)
    return {"message": f"Рассылка запущена для {len(emails)} пользователей", "count": len(emails)}

@app.post("/admin/send-email")
async def admin_send_email(data: dict, background_tasks: BackgroundTasks, admin=Depends(get_admin_user)):
    to = data.get("to", "")
    subject = data.get("subject", "Сообщение от SmartTutor")
    body = data.get("body", "")
    if not to or not body:
        raise HTTPException(400, "Укажите получателя и текст")
    background_tasks.add_task(send_email_smtp, to, subject, body)
    return {"message": f"Письмо отправлено на {to}"}

# ─── ADMIN: ЭКСПОРТ ──────────────────────────────────────────────────────────

@app.get("/admin/export/users")
def admin_export_users(admin=Depends(get_admin_user)):
    from fastapi.responses import Response
    users = sb_get_all("users") or []
    lines = ["ID,Email,Username,FullName,IsActive,IsAdmin,AuthProvider,CreatedAt"]
    for u in users:
        lines.append(f'"{u.get("id","")}","{u.get("email","")}","{u.get("username","")}","{u.get("full_name","")}",{u.get("is_active","")},{u.get("is_admin","")},{u.get("auth_provider","email")},"{str(u.get("created_at",""))[:10]}"')
    csv = "\n".join(lines)
    return Response(content="﻿"+csv, media_type="text/csv; charset=utf-8", headers={"Content-Disposition":"attachment; filename=smarttutor_users.csv"})

@app.get("/admin/export/logs")
def admin_export_logs(admin=Depends(get_admin_user)):
    from fastapi.responses import Response
    logs = sb_get_all("login_logs") or []
    lines = ["ID,UserID,IP,Success,AttemptedAt"]
    for l in logs:
        lines.append(f'"{l.get("id","")}","{l.get("user_id","")}","{l.get("ip_address","")}",{l.get("success","")},"{str(l.get("attempted_at",""))[:19]}"')
    csv = "\n".join(lines)
    return Response(content="﻿"+csv, media_type="text/csv; charset=utf-8", headers={"Content-Disposition":"attachment; filename=smarttutor_logs.csv"})

@app.get("/admin/export/stats")
def admin_export_stats(admin=Depends(get_admin_user)):
    from fastapi.responses import Response
    users = sb_get_all("users") or []
    messages = sb_get_all("messages") or []
    topics = sb_get_all("topics") or []
    lines = [
        "Метрика,Значение",
        f"Всего пользователей,{len(users)}",
        f"Активных пользователей,{len([u for u in users if u.get('is_active')])}",
        f"Всего сообщений,{len([m for m in messages if m.get('role')=='user'])}",
        f"Всего тем,{len(topics)}",
        f"Завершённых тем,{len([t for t in topics if t.get('status')=='completed'])}",
    ]
    csv = "\n".join(lines)
    return Response(content="﻿"+csv, media_type="text/csv; charset=utf-8", headers={"Content-Disposition":"attachment; filename=smarttutor_stats.csv"})

# ─── ADMIN: НАСТРОЙКИ ────────────────────────────────────────────────────────

@app.get("/admin/settings")
def admin_get_settings(admin=Depends(get_admin_user)):
    return _system_settings

@app.patch("/admin/settings")
def admin_update_settings(data: dict, admin=Depends(get_admin_user)):
    allowed = {"registration_enabled","maintenance_mode","qa_api_key","gigachat_temperature","max_tokens"}
    for key in allowed:
        if key in data:
            _system_settings[key] = data[key]
    return _system_settings

# ─── ADMIN: IP БАНЫ ──────────────────────────────────────────────────────────

@app.get("/admin/ip-bans")
def admin_get_ip_bans(admin=Depends(get_admin_user)):
    import time as _t
    now = _t.time()
    expired = [ip for ip, v in list(_ip_bans.items()) if v.get("until", 0) <= now]
    for ip in expired:
        del _ip_bans[ip]
    return [{"ip": ip, "until": int(v["until"]), "reason": v.get("reason", ""), "days_left": max(0, round((v["until"] - now) / 86400, 1))} for ip, v in _ip_bans.items()]

@app.post("/admin/ip-bans")
def admin_ban_ip(data: dict, admin=Depends(get_admin_user)):
    import time as _t
    ip = data.get("ip", "").strip()
    days = max(1, min(365, int(data.get("days", 1))))
    reason = data.get("reason", "")
    if not ip:
        raise HTTPException(400, "Укажите IP адрес")
    until = int(_t.time() + days * 86400)
    _ip_bans[ip] = {"until": until, "reason": reason}
    try:
        existing = sb_get("ip_bans", {"ip": ip})
        if existing:
            sb_update("ip_bans", {"until": until, "reason": reason}, {"ip": ip})
        else:
            sb_insert("ip_bans", {"ip": ip, "until": until, "reason": reason})
    except Exception:
        pass
    return {"message": f"IP {ip} заблокирован на {days} дн."}

@app.delete("/admin/ip-bans/{ip_encoded}")
def admin_unban_ip(ip_encoded: str, admin=Depends(get_admin_user)):
    from urllib.parse import unquote
    ip = unquote(ip_encoded)
    if ip in _ip_bans:
        del _ip_bans[ip]
    try:
        sb_delete("ip_bans", {"ip": ip})
    except Exception:
        pass
    return {"message": f"IP {ip} разблокирован"}

# ─── ADMIN: ПРОМПТЫ ──────────────────────────────────────────────────────────

PROMPT_LABELS = {
    MODE_FREE_CHAT: "Свободный чат",
    MODE_PLAN_GENERATOR: "Генератор плана",
    MODE_STUDY_STEP: "Обучение по шагам",
    MODE_FINAL_TEST: "Итоговый тест",
}

@app.get("/admin/prompts")
def admin_get_prompts(admin=Depends(get_admin_user)):
    sample = {"username": "студент", "topic_title": "Python", "current_step": 1, "step_title": "Введение", "total_steps": 5, "completed_count": 0, "user_msg_count": 0, "completed_topics": [], "topic": "Python"}
    result = {}
    for mode, label in PROMPT_LABELS.items():
        is_override = mode in _prompt_overrides and bool(_prompt_overrides[mode])
        try:
            content = _prompt_overrides[mode] if is_override else get_system_prompt(mode, **sample)
        except Exception:
            content = ""
        result[mode] = {"label": label, "content": content, "is_override": is_override}
    return result

@app.patch("/admin/prompts/{mode}")
def admin_update_prompt(mode: str, data: dict, admin=Depends(get_admin_user)):
    if mode not in PROMPT_LABELS:
        raise HTTPException(400, "Неверный режим")
    content = data.get("content", "").strip()
    if content:
        _prompt_overrides[mode] = content
        try:
            existing = sb_get("prompt_overrides", {"mode": mode})
            if existing:
                sb_update("prompt_overrides", {"content": content}, {"mode": mode})
            else:
                sb_insert("prompt_overrides", {"mode": mode, "content": content})
        except Exception:
            pass
    else:
        _prompt_overrides.pop(mode, None)
        try:
            sb_delete("prompt_overrides", {"mode": mode})
        except Exception:
            pass
    return {"message": "Промпт сохранён" if content else "Промпт сброшен"}

@app.post("/admin/prompts/{mode}/reset")
def admin_reset_prompt(mode: str, admin=Depends(get_admin_user)):
    _prompt_overrides.pop(mode, None)
    try:
        sb_delete("prompt_overrides", {"mode": mode})
    except Exception:
        pass
    return {"message": "Промпт сброшен к исходному"}

def sb_get_all(table: str):
    """Получить все записи из таблицы (без фильтра)"""
    import requests as req
    r = req.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params={"select": "*", "limit": "1000"})
    if r.status_code == 200:
        return r.json()
    return []

@app.on_event("startup")
def load_persistent_data():
    import time as _t
    now = _t.time()
    try:
        bans = sb_get_all("ip_bans")
        for b in bans:
            if b.get("until", 0) > now:
                _ip_bans[b["ip"]] = {"until": b["until"], "reason": b.get("reason", "")}
        print(f"[STARTUP] IP банов загружено: {len(_ip_bans)}")
    except Exception as e:
        print(f"[STARTUP] Ошибка загрузки IP банов: {e}")
    try:
        overrides = sb_get_all("prompt_overrides")
        for o in overrides:
            if o.get("mode") and o.get("content"):
                _prompt_overrides[o["mode"]] = o["content"]
        print(f"[STARTUP] Промптов загружено: {len(_prompt_overrides)}")
    except Exception as e:
        print(f"[STARTUP] Ошибка загрузки промптов: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
