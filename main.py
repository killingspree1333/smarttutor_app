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
@app.post("/auth/register")
async def register(data: UserRegister, request: Request, background_tasks: BackgroundTasks):
    import random, time
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
        raise HTTPException(status_code=403, detail="UNVERIFIED:" + data.email)
    sb_insert("login_logs", {"user_id": user["id"], "ip_address": request.client.host, "success": True})
    token = create_access_token({"sub": str(user["id"])})
    return TokenResponse(access_token=token, user_id=user["id"], username=user["username"], email=user["email"])

# ─── ПРОФИЛЬ ───
@app.get("/profile/me")
def get_profile(current_user=Depends(get_current_user)):
    return {"id": current_user["id"], "email": current_user["email"], "username": current_user["username"], "full_name": current_user.get("full_name"), "avatar_url": current_user.get("avatar_url"), "is_active": current_user["is_active"], "created_at": str(current_user.get("created_at", "")), "auth_provider": current_user.get("auth_provider", "email")}

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
