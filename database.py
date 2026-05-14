import requests
import os

SUPABASE_URL = "https://iyingdhlkmlfobfqjjuf.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Iml5aW5nZGhsa21sZm9iZnFqanVmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzgzNjMyNzIsImV4cCI6MjA5MzkzOTI3Mn0.GxJrjIr6GGsI7QP7OMjWVCeXqodn9HjJkTu7bsU86Mo"
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Iml5aW5nZGhsa21sZm9iZnFqanVmIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3ODM2MzI3MiwiZXhwIjoyMDkzOTM5MjcyfQ.14rNkDDY-QAw22vG_1PKRCUgjAJeFZE_TEJo0O8xde4"

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

def sb_get(table: str, filters: dict = None):
    """Получить записи из таблицы"""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = {"limit": "1000"}
    if filters:
        for key, value in filters.items():
            params[key] = f"eq.{value}"
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    return resp.json()

def sb_get_one(table: str, filters: dict = None):
    """Получить одну запись"""
    results = sb_get(table, filters)
    return results[0] if results else None

def sb_insert(table: str, data: dict):
    """Вставить запись и вернуть результат"""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    resp = requests.post(url, headers=HEADERS, json=data)
    resp.raise_for_status()
    result = resp.json()
    return result[0] if result else None

def sb_update(table: str, data: dict, filters: dict):
    """Обновить записи"""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params = {}
    for key, value in filters.items():
        params[key] = f"eq.{value}"
    resp = requests.patch(url, headers=HEADERS, json=data, params=params)
    resp.raise_for_status()
    return resp.json()

# Совместимые заглушки (не используются напрямую)
def execute_one(query: str, params=None):
    pass

def execute_query(query: str, params=None, fetch=False):
    pass
