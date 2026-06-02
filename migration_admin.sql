-- =====================================================
-- SmartTutor — Admin + QA Tester Migration
-- Запусти в Supabase SQL Editor
-- =====================================================

-- 1. Добавляем роль админа в users
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;

-- Сделай себя админом (замени на свой email):
UPDATE users SET is_admin = TRUE WHERE email = 'flareonxg@gmail.com';

-- 2. Таблица тест-кейсов (от QA Tester)
CREATE TABLE IF NOT EXISTS test_cases (
    id SERIAL PRIMARY KEY,
    project_name VARCHAR(255) NOT NULL,
    case_title VARCHAR(500) NOT NULL,
    description TEXT,
    steps TEXT,
    expected_result TEXT,
    priority VARCHAR(50) DEFAULT 'medium',
    status VARCHAR(50) DEFAULT 'not_run',   -- not_run / passed / failed / blocked / skipped
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    source VARCHAR(100) DEFAULT 'qa_tester'
);

-- 3. Таблица результатов тест-прогонов
CREATE TABLE IF NOT EXISTS test_runs (
    id SERIAL PRIMARY KEY,
    run_name VARCHAR(255),
    project_name VARCHAR(255),
    total INTEGER DEFAULT 0,
    passed INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    blocked INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_test_cases_project ON test_cases(project_name);
CREATE INDEX IF NOT EXISTS idx_test_cases_status ON test_cases(status);
CREATE INDEX IF NOT EXISTS idx_users_admin ON users(is_admin);

-- =====================================================
-- После запуска проверь:
-- SELECT email, is_admin FROM users;
-- =====================================================
