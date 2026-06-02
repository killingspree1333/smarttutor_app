-- =====================================================
-- SmartTutor — Migration SQL для Supabase
-- Запусти это в Supabase: SQL Editor → New query → Run
-- =====================================================

-- ─── 1. ОБНОВЛЕНИЕ ТАБЛИЦЫ USERS ─────────────────────

-- Убираем group_name (больше не используется)
ALTER TABLE users DROP COLUMN IF EXISTS group_name;

-- Добавляем auth_provider (email / google / github)
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider VARCHAR(50) DEFAULT 'email';

-- is_active теперь FALSE по умолчанию (верификация email при регистрации)
ALTER TABLE users ALTER COLUMN is_active SET DEFAULT FALSE;

-- ─── 2. ОБНОВЛЕНИЕ ТАБЛИЦЫ CHAT_SESSIONS ─────────────

-- Добавляем is_deleted (мягкое удаление чатов)
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;

-- ─── 3. ТАБЛИЦА ТЕМ ОБУЧЕНИЯ ─────────────────────────

CREATE TABLE IF NOT EXISTS topics (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES chat_sessions(id) ON DELETE SET NULL,
    title VARCHAR(255) NOT NULL,
    subject VARCHAR(255),
    status VARCHAR(50) DEFAULT 'active',    -- active / completed / paused
    progress INTEGER DEFAULT 0,             -- 0–100
    current_step INTEGER DEFAULT 1,
    total_steps INTEGER DEFAULT 5,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ─── 4. ТАБЛИЦА ШАГОВ ТЕМЫ ───────────────────────────

CREATE TABLE IF NOT EXISTS topic_steps (
    id SERIAL PRIMARY KEY,
    topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL,
    title VARCHAR(255),
    status VARCHAR(50) DEFAULT 'pending',   -- pending / active / completed
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ─── 5. ТАБЛИЦА АКТИВНОСТИ ───────────────────────────

CREATE TABLE IF NOT EXISTS user_activity (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    messages_count INTEGER DEFAULT 0,
    UNIQUE(user_id, date)
);

-- ─── 6. ИНДЕКСЫ ──────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_topics_user ON topics(user_id);
CREATE INDEX IF NOT EXISTS idx_topics_session ON topics(session_id);
CREATE INDEX IF NOT EXISTS idx_topic_steps_topic ON topic_steps(topic_id);
CREATE INDEX IF NOT EXISTS idx_topic_steps_user ON topic_steps(user_id);
CREATE INDEX IF NOT EXISTS idx_user_activity_user ON user_activity(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_deleted ON chat_sessions(is_deleted);

-- ─── 7. АКТИВИРУЙ СУЩЕСТВУЮЩИХ ПОЛЬЗОВАТЕЛЕЙ ─────────
-- Старые аккаунты делаем активными (они были созданы до верификации)
UPDATE users SET is_active = TRUE WHERE is_active IS NULL OR is_active = FALSE;

-- =====================================================
-- Готово! Проверь что все таблицы созданы:
-- SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';
-- =====================================================
