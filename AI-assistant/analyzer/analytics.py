import os
import json
import requests
import pandas as pd
from django.conf import settings


def load_excel_from_path(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Excel файл не найден: {path}")
    return pd.read_excel(path)


def load_txt_tests_from_folder(folder: str) -> str:
    combined = ""
    for file in os.listdir(folder):
        if file.lower().endswith(".txt"):
            full_path = os.path.join(folder, file)
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            combined += f"\n\n===== ТЕСТ: {file} =====\n"
            combined += content[:3000]
    return combined


def build_structured_data(df, tests_text):
    required = {"ID пользователя", "Баллы за попытку"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"В Excel отсутствуют столбцы: {', '.join(missing)}")

    if "Максимальный балл за вопрос" in df.columns:
        max_col = "Максимальный балл за вопрос"
    elif "Максимальный балл" in df.columns:
        max_col = "Максимальный балл"
    else:
        max_col = None

    students = []
    for user_id, group in df.groupby("ID пользователя"):
        total_score = group["Баллы за попытку"].sum()

        if max_col:
            total_max = group[max_col].sum()
            avg_percent = round((total_score / total_max) * 100, 2) if total_max > 0 else 0
        elif "Баллы за попытку (%)" in group.columns:
            avg_percent = round(group["Баллы за попытку (%)"].mean(), 2)
        else:
            avg_percent = None

        if avg_percent and avg_percent > 100:
            avg_percent = 100.0

        students.append({
            "user_id": str(user_id),
            "attempts_count": group["Номер попытки"].nunique() if "Номер попытки" in group.columns else len(group),
            "average_score_percent": avg_percent,
            "tests": group.to_dict(orient="records"),
        })

    return {
        "students_total": len(students),
        "students": students,
        "tests_text": tests_text[:6000],
    }


def generate_report(structured_data, query: str = "") -> str:
    api_key = settings.OPENROUTER_API_KEY
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY не задан")

    system_prompt = """Ты — аналитическая система образовательной платформы.
Правила:
1. Один ID пользователя = один студент.
2. Агрегируй данные по студенту.
3. Выявляй неуспевающих (<60%).
4. Определи проблемные темы.
5. Сделай: общую сводку, тематическую аналитику, анализ неуспевающих, поведенческий анализ, педагогические рекомендации.
Стиль — профессиональный, структурированный. Не перечисляй сырые данные."""

    focus = f"\n\nДополнительный вопрос: {query}" if query else ""
    user_prompt = f"Сформируй полный аналитический отчет.{focus}\n\nДАННЫЕ:\n{json.dumps(structured_data, ensure_ascii=False)[:15000]}"

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "openai/gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 2000,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
