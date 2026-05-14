# SmartTutor Backend

FastAPI бэкенд для кроссплатформенной системы поддержки студентов.

## Установка

```bash
pip install -r requirements.txt
```

## Настройка

Скопируй `.env.example` в `.env` и заполни:

```bash
cp .env.example .env
```

Заполни в .env:
- `DATABASE_URL` — строка подключения к Supabase (Session Pooler)
- `SECRET_KEY` — любая длинная случайная строка
- `OPENROUTER_API_KEY` — ключ от openrouter.ai

## Запуск

```bash
python main.py
```

API будет доступен на http://localhost:8000
Документация: http://localhost:8000/docs

## Эндпоинты

| Метод | URL | Описание |
|-------|-----|----------|
| POST | /auth/register | Регистрация |
| POST | /auth/login | Вход |
| GET | /profile/me | Мой профиль |
| PUT | /profile/me | Обновить профиль |
| GET | /chat/sessions | Список чатов |
| POST | /chat/sessions | Создать чат |
| GET | /chat/sessions/{id}/messages | Сообщения чата |
| POST | /chat/send | Отправить сообщение ИИ |
| GET | /profile/progress | Учебный прогресс |
| GET | /subscription | Подписка |
