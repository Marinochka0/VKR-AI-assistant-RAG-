import json
import re
import traceback

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import UserProfile
from .rag import (
    _index_cache,
    answer,
    build_sources_payload,
    classify_query,
    filter_chunks_for_role,
    get_chunks,
    get_data_status,
    smart_retrieve,
)

FOLLOWUP_PATTERNS = [
    r"^(а|и|или|то есть|тогда|ещ[её]|понятно|хорошо|ладно)\b",
    r"\b(это|этот|эта|эти|он|она|они|там|тут|здесь|такой|такая|такие)\b",
    r"\b(по нему|по ней|по ним|об этом|про это|про него|про нее|про них)\b",
    r"\b(какие именно|а какие|а что|а как|а где|а когда|а сколько|а почему)\b",
    r"^(подробнее|уточни|продолжи|покажи подробнее|объясни подробнее)\b",
]

NEW_TOPIC_HINTS = [
    "сп", "сцт", "иииад", "микт", "пивкд", "мкт", "ияд", "ииад",
    "системное программирование",
    "сквозные цифровые технологии",
    "искусственный интеллект и анализ данных",
    "математика и компьютерные технологии",
    "прикладная информатика в компьютерном дизайне",
    "компетенции", "дисциплины", "график", "учебный план", "семестр",
]


def _get_profile(user):
    profile, _ = UserProfile.objects.get_or_create(
        user=user,
        defaults={"role": "teacher" if user.is_staff else "student"}
    )
    return profile


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _sanitize_history(history: list) -> list:
    if not isinstance(history, list):
        return []

    cleaned = []
    for item in history[-8:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = (item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        cleaned.append({"role": role, "content": content[:1800]})
    return cleaned


def _has_explicit_new_topic(question: str) -> bool:
    q = _normalize_text(question)
    return any(hint in q for hint in NEW_TOPIC_HINTS)


def _is_followup_by_pattern(question: str) -> bool:
    q = _normalize_text(question)
    return any(re.search(pattern, q) for pattern in FOLLOWUP_PATTERNS)


def _select_history(question: str, history: list) -> list:
    if not history:
        return []

    q = _normalize_text(question)
    words = q.split()

    if _has_explicit_new_topic(q):
        return []

    if len(words) >= 8:
        return []

    if _is_followup_by_pattern(q):
        return history[-2:]

    if len(words) <= 3 and any(tok in q for tok in ["это", "он", "она", "они", "там", "тут", "такой", "такая"]):
        return history[-2:]

    return []


def _select_top_k(query_type: str, question: str) -> int:
    q = _normalize_text(question)
    broad_markers = [
        "расскажи", "опиши", "общая", "подробно", "в целом",
        "учебный план", "программа", "все дисциплины", "все компетенции",
        "все 115", "полный список", "полный учебный план", "напиши все"
    ]

    if query_type in ("analytics", "student"):
        return 6
    if query_type == "curriculum":
        return 12 if any(marker in q for marker in broad_markers) else 6
    if query_type in ("competencies", "documents"):
        return 6
    return 5 if any(marker in q for marker in broad_markers) else 4


def login_view(request):
    if request.user.is_authenticated:
        return redirect("/")

    error = ""
    if request.method == "POST":
        user = authenticate(
            request,
            username=request.POST.get("username", ""),
            password=request.POST.get("password", "")
        )
        if user:
            login(request, user)
            return redirect(request.GET.get("next", "/"))
        error = "Неверный логин или пароль"

    return render(request, "login.html", {"error": error})


def logout_view(request):
    logout(request)
    return redirect("/login/")


def register_view(request):
    error = ""
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip()
        full_name = request.POST.get("full_name", "").strip()

        if not username or not password:
            error = "Заполните все поля"
        elif User.objects.filter(username=username).exists():
            error = "Пользователь уже существует"
        else:
            user = User.objects.create_user(username=username, password=password)
            UserProfile.objects.create(user=user, role="student", full_name=full_name)
            login(request, user)
            return redirect("/")

    return render(request, "register.html", {"error": error})


@login_required
def index(request):
    profile = _get_profile(request.user)
    is_teacher = profile.is_teacher() or request.user.is_staff
    status = get_data_status()

    if not is_teacher:
        status["files"] = [f for f in status["files"] if "result" not in f["name"].lower()]

    return render(request, "analyzer/index.html", {
        "status": status,
        "is_teacher": is_teacher,
        "profile": profile,
    })


@login_required
@csrf_exempt
@require_POST
def chat(request):
    try:
        body = json.loads(request.body)
        question = (body.get("question") or "").strip()
        raw_history = _sanitize_history(body.get("history", []))

        if not question:
            return JsonResponse({"error": "Пустой вопрос"}, status=400)

        profile = _get_profile(request.user)
        is_teacher = profile.is_teacher() or request.user.is_staff

        chunks = get_chunks()
        chunks = filter_chunks_for_role(chunks, is_teacher=is_teacher)

        effective_history = _select_history(question, raw_history)
        query_meta = classify_query(question, is_teacher=is_teacher)
        query_meta["use_history"] = bool(effective_history)

        top_k = _select_top_k(query_meta["type"], question)
        context = smart_retrieve(
            query=question,
            chunks=chunks,
            top_k=top_k,
            query_meta=query_meta,
        )

        reply = answer(
            query=question,
            context_chunks=context,
            history=effective_history,
            is_teacher=is_teacher,
            query_meta=query_meta,
        )

        return JsonResponse({
            "answer": reply,
            "used_history": len(effective_history),
            "used_chunks": len(context),
            "query_type": query_meta["type"],
            "mode": query_meta["mode"],
            "sources": build_sources_payload(context),
        })

    except Exception:
        return JsonResponse({"error": traceback.format_exc()}, status=500)


@login_required
@csrf_exempt
def reload_kb(request):
    profile = _get_profile(request.user)
    if not (request.user.is_staff or profile.is_teacher()):
        return JsonResponse({"error": "Доступ запрещён"}, status=403)

    _index_cache["mtime"] = None
    _index_cache["chunks"] = []
    _index_cache["vector_store_built"] = False
    get_chunks()

    return JsonResponse(get_data_status())


@login_required
def status_view(request):
    return JsonResponse(get_data_status())