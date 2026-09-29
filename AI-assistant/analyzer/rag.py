import math
import re
from pathlib import Path

import pandas as pd
import requests
from django.conf import settings

BASE_DIR = Path(settings.BASE_DIR)
DATA_DIR = BASE_DIR / "data"
EXCEL_PATH = DATA_DIR / "results.xlsx"

DIRECTION_NAMES = {
    "СП": "Системное программирование",
    "СЦТ": "Сквозные цифровые технологии",
    "ИИиАД": "Искусственный интеллект и анализ данных",
    "МиКТ": "Математика и компьютерные технологии",
    "МКТ": "Математика и компьютерные технологии",
    "МИКТ": "Математика и компьютерные технологии",
    "ИИАД": "Искусственный интеллект и анализ данных",
    "ИИЯД": "Искусственный интеллект и анализ данных",
    "ПИвКД": "Прикладная информатика в компьютерном дизайне",
    "ПИВКД": "Прикладная информатика в компьютерном дизайне",
    "ПКД": "Прикладная информатика в компьютерном дизайне",
}

_index_cache: dict = {
    "mtime": None,
    "chunks": [],
    "vector_store_built": False,
}

QUERY_TYPE_MAP = {
    "curriculum": {"curriculum_title", "curriculum_plan", "course_plan", "schedule"},
    "competencies": {"competencies"},
    "analytics": {"summary", "topics", "questions", "answers"},
    "student": {"student", "summary", "topics"},
    "documents": {"document", "table", "curriculum_title", "curriculum_plan", "competencies"},
    "general": {
        "curriculum_title", "curriculum_plan", "course_plan", "schedule",
        "competencies", "document", "table", "summary", "topics"
    },
}

SYSTEM_PROMPT = """Ты — AI-ассистент кафедры университета.
КРИТИЧЕСКИ ВАЖНО: Отвечай только на русском языке.
КРИТИЧЕСКИ ВАЖНО: Основа ответа — только контекст по текущему вопросу.

Ты помогаешь преподавателям и студентам. В базе знаний есть: учебные планы, компетенции, графики, документы, а для преподавателей — результаты тестирования и аналитика.

Правила ответа:
- Если вопрос новый и не связан с предыдущим, не используй прошлую тему.
- Не подставляй направление из прошлого вопроса, если в текущем оно не указано.
- Не смешивай разные направления подготовки.
- Если данных достаточно, отвечай конкретно и структурированно.
- Если данных мало, честно напиши: «В базе знаний недостаточно данных по этому вопросу».
- Не выдумывай факты, которых нет в контексте.
- Пиши короткими абзацами и списками, без воды.
"""


def _safe_text(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _extract_direction(filename: str) -> str:
    name = filename.replace(".plx", "").replace(".xlsx", "").replace(".xls", "").replace(".pdf", "")
    parts = name.split("_")
    for p in parts:
        if p in DIRECTION_NAMES:
            return DIRECTION_NAMES[p]
    return ""


def _extract_direction_from_query(query: str) -> str:
    q = _safe_text(query).lower()
    for abbr, full in DIRECTION_NAMES.items():
        if abbr.lower() in q or full.lower() in q:
            return full
    return ""


def _data_mtime():
    total = 0.0
    if DATA_DIR.exists():
        for p in DATA_DIR.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                total += p.stat().st_mtime
    return total


def get_chunks() -> list:
    mtime = _data_mtime()
    if _index_cache["mtime"] == mtime and _index_cache["chunks"]:
        return _index_cache["chunks"]

    chunks = load_knowledge_base(str(DATA_DIR))
    _index_cache["mtime"] = mtime
    _index_cache["chunks"] = chunks
    _index_cache["vector_store_built"] = False

    try:
        from .embeddings import get_vector_store
        vs = get_vector_store()
        vs.build(chunks)
        _index_cache["vector_store_built"] = True
    except Exception:
        pass

    return chunks


def get_data_status() -> dict:
    files, students = [], 0

    if DATA_DIR.exists():
        for p in DATA_DIR.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".xlsx", ".xls", ".pdf", ".txt", ".csv"):
                files.append({
                    "name": p.name,
                    "type": p.suffix.lower().lstrip("."),
                    "size": f"{round(p.stat().st_size / 1024, 1)} KB",
                    "path": str(p.relative_to(DATA_DIR)),
                })

    if EXCEL_PATH.exists():
        try:
            df = pd.read_excel(str(EXCEL_PATH))
            if "ID пользователя" in df.columns:
                students = df["ID пользователя"].nunique()
        except Exception:
            pass

    try:
        from .llm_local import get_llm
        llm_mode = get_llm().mode
    except Exception:
        llm_mode = "api"

    return {
        "files": files,
        "total": len(files),
        "students": students,
        "llm_mode": llm_mode,
        "vector_ready": _index_cache.get("vector_store_built", False),
        "data_dir": str(DATA_DIR),
        "chunks_count": len(_index_cache.get("chunks", [])),
    }


def load_knowledge_base(data_dir: str) -> list:
    chunks = []
    dp = Path(data_dir)
    if not dp.exists():
        return chunks

    for p in sorted(dp.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue

        ext = p.suffix.lower()
        try:
            if ext in (".xlsx", ".xls"):
                chunks += _index_excel(p)
            elif ext == ".pdf":
                chunks += _index_pdf(p)
            elif ext == ".txt":
                chunks += _index_txt(p)
            elif ext == ".csv":
                chunks += _index_csv(p)
        except Exception as e:
            chunks.append({
                "source": p.name,
                "text": f"Ошибка обработки {p.name}: {e}",
                "type": "error"
            })

    return chunks


def _index_excel(path: Path) -> list:
    chunks = []
    direction = _extract_direction(path.name)

    try:
        xl = pd.ExcelFile(str(path))
    except Exception as e:
        return [{"source": path.name, "text": f"Ошибка открытия {path.name}: {e}", "type": "error"}]

    for sheet in xl.sheet_names:
        try:
            df = xl.parse(sheet, header=None)
            df = df.dropna(how="all").dropna(axis=1, how="all")
            if df.empty:
                continue

            flat = " ".join(str(v) for v in df.values.flatten() if pd.notna(v)).lower()

            if sheet == "Титул" or ("направлени" in flat and "образовательн" in flat):
                chunks += _index_generic_sheet(df, path.name, sheet, direction, chunk_type="curriculum_title")
            elif sheet == "План" or ("индекс" in flat and "наименование" in flat and "дисципл" in flat):
                chunks += _index_curriculum_plan(df, path.name, sheet, direction)
            elif "компетенц" in sheet.lower() or ("компетенц" in flat and "содержание" in flat):
                chunks += _index_competencies(df, path.name, sheet, direction)
            elif sheet == "График" or "календарн" in flat:
                chunks += _index_generic_sheet(df, path.name, sheet, direction, chunk_type="schedule")
            elif sheet in ("Курс 1", "Курс 2", "Курс 3", "Курс 4") or ("курс" in sheet.lower() and "семестр" in flat):
                chunks += _index_generic_sheet(df, path.name, sheet, direction, chunk_type="course_plan")
            elif "id пользователя" in flat or "баллы за попытку" in flat:
                df2 = xl.parse(sheet)
                chunks += _index_student_results(df2, path.name)
            else:
                chunks += _index_generic_sheet(df, path.name, sheet, direction)
        except Exception as e:
            chunks.append({"source": path.name, "text": f"Лист {sheet}: {e}", "type": "error"})

    return chunks


def _make_header(source, direction=""):
    return f"НАПРАВЛЕНИЕ: {direction}\nФайл: {source}" if direction else f"Файл: {source}"


def _normalize_lines(lines: list[str]) -> list[str]:
    result = []
    seen = set()

    for line in lines:
        value = _safe_text(line)
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)

    return result


def _index_curriculum_plan(df, source, sheet, direction=""):
    chunks = []
    header = _make_header(source, direction)
    disciplines = []

    for _, row in df.iterrows():
        vals = [str(v).strip() if pd.notna(v) else "" for v in row]
        joined = " ".join(v for v in vals if v)

        if re.search(r'[БбФф]\d+\.[А-ЯЁA-Zа-яё]+[\.\d]*', joined) or re.search(r'[A-ZА-ЯЁ]\d+\.\w+\.\d+', joined):
            meaningful = [v for v in vals if v and v != "nan" and len(v) > 2]
            if len(meaningful) >= 2 and not meaningful[1].replace(".", "").replace("-", "").isdigit():
                disciplines.append(f"{meaningful[0]} — {meaningful[1]}")

    disciplines = _normalize_lines(disciplines)

    base = [f"УЧЕБНЫЙ ПЛАН — ПЕРЕЧЕНЬ ДИСЦИПЛИН\n{header}\nВсего дисциплин: {len(disciplines)}", ""]

    for i in range(0, max(len(disciplines), 1), 20):
        batch = list(base) if i == 0 else [f"ДИСЦИПЛИНЫ ({source}) часть {i // 20 + 1}", ""]
        batch += disciplines[i:i + 20]
        chunks.append({
            "source": source,
            "text": "\n".join(batch),
            "type": "curriculum_plan"
        })

    return chunks or [{
        "source": source,
        "text": f"УЧЕБНЫЙ ПЛАН ({source}): дисциплины не найдены",
        "type": "curriculum_plan"
    }]


def _index_competencies(df, source, sheet, direction=""):
    header = _make_header(source, direction)
    lines = [f"КОМПЕТЕНЦИИ\n{header}"]

    for _, row in df.iterrows():
        vals = [str(v).strip() for v in row if pd.notna(v) and str(v).strip() and str(v) != "nan"]
        if vals and len(" ".join(vals)) > 5:
            lines.append(" — ".join(vals[:4]))

    chunks = []
    for i in range(0, len(lines), 28):
        batch = lines[i:i + 28]
        if i > 0:
            batch = [f"КОМПЕТЕНЦИИ ({source}) продолжение"] + batch
        chunks.append({"source": source, "text": "\n".join(batch), "type": "competencies"})

    return chunks or [{"source": source, "text": "\n".join(lines), "type": "competencies"}]


def _index_generic_sheet(df, source, sheet, direction="", chunk_type="table"):
    header = _make_header(source, direction)
    lines = [f"ТАБЛИЦА: {source} / лист: {sheet}\n{header}"]

    for i, (_, row) in enumerate(df.iterrows()):
        if i > 80:
            break
        vals = [str(v).strip() for v in row if pd.notna(v) and str(v).strip() and str(v) != "nan"]
        if vals:
            lines.append(" | ".join(vals[:10]))

    chunks = []
    for i in range(0, len(lines), 28):
        chunks.append({"source": source, "text": "\n".join(lines[i:i + 28]), "type": chunk_type})

    return chunks or [{"source": source, "text": "\n".join(lines), "type": chunk_type}]


def _index_student_results(df, source):
    if "ID пользователя" not in df.columns:
        return []

    total = df["ID пользователя"].nunique()
    lines = [
        f"РЕЗУЛЬТАТЫ ТЕСТИРОВАНИЯ СТУДЕНТОВ\nФайл: {source}",
        f"Всего студентов: {total}",
    ]
    return [{"source": source, "text": "\n".join(lines), "type": "summary"}]


def _extract_pdf(path: Path) -> str:
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(str(path)) as pdf:
            for p in pdf.pages:
                t = p.extract_text()
                if t:
                    parts.append(t)
        if parts:
            return "\n\n".join(parts)
    except Exception:
        pass
    return ""


def _index_pdf(path: Path) -> list:
    text = _extract_pdf(path)
    if not text.strip():
        return [{"source": path.name, "text": f"PDF {path.name}: текст не извлечён", "type": "error"}]

    direction = _extract_direction(path.name)
    header = _make_header(path.name, direction)

    chunks, i, part = [], 0, 1
    while i < len(text):
        chunks.append({
            "source": path.name,
            "text": f"{header} / стр.~{part}\n{text[i:i + 1200]}",
            "type": "document"
        })
        i += 950
        part += 1
    return chunks


def _index_txt(path: Path) -> list:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return []

    if not text:
        return []

    direction = _extract_direction(path.name)
    header = _make_header(path.name, direction)

    chunks, i, part = [], 0, 1
    while i < len(text):
        chunks.append({
            "source": path.name,
            "text": f"{header} / часть {part}\n{text[i:i + 1200]}",
            "type": "document"
        })
        i += 950
        part += 1
    return chunks


def _index_csv(path: Path) -> list:
    try:
        df = pd.read_csv(path, encoding="utf-8", encoding_errors="replace")
    except Exception:
        try:
            df = pd.read_csv(path, encoding="cp1251", encoding_errors="replace")
        except Exception as e:
            return [{"source": path.name, "text": f"Ошибка CSV: {e}", "type": "error"}]

    direction = _extract_direction(path.name)
    return _index_generic_sheet(df, path.name, "CSV", direction)


def classify_query(query: str, is_teacher: bool = False) -> dict:
    q = _safe_text(query).lower()

    analytics_kw = ["сложн", "статист", "успева", "не успева", "средний балл", "слабые темы", "сложные темы", "анализ", "результат тест", "вопросы"]
    student_kw = ["студент", "id", "пользователь", "кто набрал", "кто не сдал", "кто прошел"]
    competencies_kw = ["компетен", "компетенции", "умения", "навыки", "формируются"]
    curriculum_kw = ["дисциплин", "учебный план", "семестр", "курс", "аттестаци", "экзамен", "зачет", "график", "нагрузка"]
    document_kw = ["документ", "pdf", "файл", "приказ", "методич", "регламент"]

    if any(k in q for k in student_kw) and is_teacher:
        return {"type": "student", "mode": "student", "use_history": False}
    if any(k in q for k in analytics_kw):
        return {"type": "analytics", "mode": "analytics", "use_history": False}
    if any(k in q for k in competencies_kw):
        return {"type": "competencies", "mode": "curriculum", "use_history": False}
    if any(k in q for k in curriculum_kw):
        return {"type": "curriculum", "mode": "curriculum", "use_history": False}
    if any(k in q for k in document_kw):
        return {"type": "documents", "mode": "documents", "use_history": False}
    return {"type": "general", "mode": "general", "use_history": False}


def filter_chunks_for_role(chunks: list, is_teacher: bool) -> list:
    if is_teacher:
        return chunks
    hidden_types = {"summary", "student", "topics", "questions", "answers"}
    return [c for c in chunks if c.get("type") not in hidden_types]


def _query_allowed_types(query_meta: dict) -> set:
    qtype = query_meta.get("type", "general")
    return QUERY_TYPE_MAP.get(qtype, QUERY_TYPE_MAP["general"])


def build_sources_payload(chunks: list) -> list:
    result = []
    for c in chunks[:6]:
        text = _safe_text(c.get("text")).replace("\n", " ")
        result.append({
            "source": c.get("source", "Неизвестный файл"),
            "type": c.get("type", "unknown"),
            "preview": text[:220] + ("..." if len(text) > 220 else "")
        })
    return result


def _tok(text):
    return re.findall(r"[а-яёa-z0-9]+", _safe_text(text).lower())


def _bm25(qt, dt, df_map, n_docs, k1=1.5, b=0.75, avgdl=50):
    dl = len(dt)
    tf = {}
    for t in dt:
        tf[t] = tf.get(t, 0) + 1

    score = 0.0
    for t in set(qt):
        if t not in tf:
            continue
        idf = math.log((n_docs - df_map.get(t, 1) + 0.5) / (df_map.get(t, 1) + 0.5) + 1)
        score += idf * (tf[t] * (k1 + 1)) / (tf[t] + k1 * (1 - b + b * dl / avgdl))
    return score


def _is_full_curriculum_request(query: str, query_meta: dict | None = None) -> bool:
    q = _safe_text(query).lower()
    if (query_meta or {}).get("type") != "curriculum":
        return False

    markers = [
        "все дисциплины",
        "весь учебный план",
        "полный учебный план",
        "полный список",
        "все 115",
        "напиши все",
        "перечисли все",
        "полный перечень",
    ]
    return any(marker in q for marker in markers)


def retrieve(query: str, chunks: list, top_k: int = 10, query_meta: dict | None = None) -> list:
    if not chunks:
        return []

    query_meta = query_meta or {"type": "general"}
    allowed_types = _query_allowed_types(query_meta)
    candidate_chunks = [c for c in chunks if c.get("type") in allowed_types] or chunks

    qt = _tok(query)
    if not qt:
        return candidate_chunks[:top_k]

    tokenized = [_tok(c["text"]) for c in candidate_chunks]
    n_docs = len(candidate_chunks)
    df_map = {}

    for toks in tokenized:
        for t in set(toks):
            df_map[t] = df_map.get(t, 0) + 1

    avgdl = sum(len(t) for t in tokenized) / max(n_docs, 1)
    scored = []
    q_low = _safe_text(query).lower()
    direction = _extract_direction_from_query(query)

    for i, (chunk, toks) in enumerate(zip(candidate_chunks, tokenized)):
        score = _bm25(qt, toks, df_map, n_docs, avgdl=avgdl)

        txt = _safe_text(chunk.get("text")).lower()
        src = _safe_text(chunk.get("source")).lower()
        ctype = chunk.get("type", "")

        if direction and (direction.lower() in txt or direction.lower() in src):
            score += 10.0

        if ctype in allowed_types:
            score += 4.0

        if query_meta.get("type") == "curriculum" and ctype == "curriculum_plan":
            score += 5.0
        if query_meta.get("type") == "competencies" and ctype == "competencies":
            score += 5.0
        if query_meta.get("type") == "documents" and ctype == "document":
            score += 4.0

        scored.append((score, i))

    scored.sort(key=lambda x: -x[0])
    return [candidate_chunks[i] for _, i in scored[:top_k]]


def smart_retrieve(query: str, chunks: list, top_k: int = 10, query_meta: dict | None = None) -> list:
    query_meta = query_meta or {"type": "general"}

    if _is_full_curriculum_request(query, query_meta):
        direction = _extract_direction_from_query(query)
        curriculum_chunks = [c for c in chunks if c.get("type") == "curriculum_plan"]
        if direction:
            filtered = [
                c for c in curriculum_chunks
                if direction.lower() in _safe_text(c.get("text")).lower()
                or direction.lower() in _safe_text(c.get("source")).lower()
            ]
            return filtered or curriculum_chunks
        return curriculum_chunks

    if _index_cache.get("vector_store_built"):
        try:
            from .embeddings import hybrid_search, get_vector_store
            results = hybrid_search(query, chunks, get_vector_store(), top_k=top_k)
            allowed = _query_allowed_types(query_meta)
            filtered = [c for c in results if c.get("type") in allowed]
            return (filtered or results)[:top_k]
        except Exception:
            pass

    return retrieve(query, chunks, top_k=top_k, query_meta=query_meta)


def _collect_curriculum_disciplines(context_chunks: list, query: str) -> list[str]:
    direction = _extract_direction_from_query(query)
    items = []

    for chunk in context_chunks:
        if chunk.get("type") != "curriculum_plan":
            continue

        text = chunk.get("text") or ""
        source = chunk.get("source") or ""
        if direction:
            hay = f"{text}\n{source}".lower()
            if direction.lower() not in hay:
                continue

        for line in text.splitlines():
            line = line.strip()
            if " — " not in line:
                continue
            low = line.lower()
            if low.startswith("учебный план"):
                continue
            if low.startswith("дисциплины ("):
                continue
            if low.startswith("направление:"):
                continue
            if low.startswith("файл:"):
                continue
            if low.startswith("всего дисциплин"):
                continue
            items.append(line)

    return _normalize_lines(items)


def _build_full_curriculum_answer(query: str, context_chunks: list) -> str:
    disciplines = _collect_curriculum_disciplines(context_chunks, query)
    direction = _extract_direction_from_query(query)

    if not disciplines:
        return "В базе знаний недостаточно данных по этому учебному плану."

    header = (
        f"В учебном плане {direction} найдено {len(disciplines)} дисциплин:"
        if direction else
        f"Найдено {len(disciplines)} дисциплин:"
    )

    lines = [header, ""]
    for i, item in enumerate(disciplines, start=1):
        lines.append(f"{i}. {item}")

    return "\n".join(lines)


def _prepare_context(context_chunks: list, query: str, query_meta: dict | None = None) -> str:
    if not context_chunks:
        return ""

    query_meta = query_meta or {"type": "general"}
    qtype = query_meta.get("type", "general")
    q = _safe_text(query).lower()

    if qtype in ("analytics", "student"):
        max_chunks, max_chars_per_chunk = 6, 1100
    elif qtype in ("curriculum", "competencies", "documents"):
        max_chunks, max_chars_per_chunk = 6, 1200
    else:
        broad_markers = ["расскажи", "подробно", "общая", "в целом", "учебный план", "программа"]
        max_chunks = 5 if any(marker in q for marker in broad_markers) else 4
        max_chars_per_chunk = 900

    prepared = []
    seen = set()

    for chunk in context_chunks:
        source = chunk.get("source", "неизвестный файл")
        chunk_type = chunk.get("type", "fragment")
        text = _safe_text(chunk.get("text"))
        key = (source, chunk_type, text[:180])

        if key in seen:
            continue
        seen.add(key)

        prepared.append(
            f"[Фрагмент {len(prepared) + 1} | тип: {chunk_type} | источник: {source}]\n{text[:max_chars_per_chunk]}"
        )
        if len(prepared) >= max_chunks:
            break

    return "\n\n---\n\n".join(prepared)


def answer(query: str, context_chunks: list, history: list, is_teacher: bool = False, query_meta: dict | None = None) -> str:
    query_meta = query_meta or {"type": "general", "mode": "general", "use_history": False}

    if _is_full_curriculum_request(query, query_meta):
        return _build_full_curriculum_answer(query, context_chunks)

    role_note = (
        "\nРоль пользователя: ПРЕПОДАВАТЕЛЬ. Можно давать полные данные о студентах, оценках и статистике."
        if is_teacher else
        "\nРоль пользователя: СТУДЕНТ. Нельзя раскрывать персональные данные других студентов."
    )

    query_note = (
        f"\nТип вопроса: {query_meta.get('type', 'general')}."
        f"\nРежим поиска: {query_meta.get('mode', 'general')}."
        f"\nИспользовать историю: {'да' if query_meta.get('use_history') else 'нет'}."
    )

    ctx = _prepare_context(context_chunks, query, query_meta)
    system = SYSTEM_PROMPT + role_note + query_note + (
        f"\n\n# АКТУАЛЬНЫЙ КОНТЕКСТ ПО ТЕКУЩЕМУ ВОПРОСУ:\n{ctx}"
        if ctx else
        "\n\n# АКТУАЛЬНЫЙ КОНТЕКСТ ПО ТЕКУЩЕМУ ВОПРОСУ:\nКонтекст не найден."
    )

    effective_history = history[-2:] if query_meta.get("use_history") else []

    try:
        from .llm_local import get_llm
        return get_llm().generate(system, query, effective_history)
    except Exception:
        api_key = settings.OPENROUTER_API_KEY
        messages = [{"role": "system", "content": system}]
        for msg in effective_history:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": query})

        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "openai/gpt-4o-mini",
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 2200
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]