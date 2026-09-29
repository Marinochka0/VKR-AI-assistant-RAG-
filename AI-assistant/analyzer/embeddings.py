import json
import hashlib
import re
from pathlib import Path

import numpy as np

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / ".vector_cache"


def get_model():
    if not hasattr(get_model, "_model"):
        try:
            from sentence_transformers import SentenceTransformer
            print("[Embeddings] Загружаем модель sentence-transformers...")
            get_model._model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
            print("[Embeddings] Модель загружена.")
        except ImportError:
            raise ImportError("Установите: pip install sentence-transformers")
    return get_model._model


def _safe_text(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _chunk_uid(chunk: dict) -> str:
    source = _safe_text(chunk.get("source"))
    ctype = _safe_text(chunk.get("type"))
    text = _safe_text(chunk.get("text"))
    raw = f"{source}||{ctype}||{text}"
    return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()


def _extract_direction_terms() -> dict:
    return {
        "сп": "системное программирование",
        "системное программирование": "системное программирование",
        "сцт": "сквозные цифровые технологии",
        "сквозные цифровые технологии": "сквозные цифровые технологии",
        "иииад": "искусственный интеллект и анализ данных",
        "ииад": "искусственный интеллект и анализ данных",
        "иияд": "искусственный интеллект и анализ данных",
        "искусственный интеллект и анализ данных": "искусственный интеллект и анализ данных",
        "микт": "математика и компьютерные технологии",
        "мкт": "математика и компьютерные технологии",
        "математика и компьютерные технологии": "математика и компьютерные технологии",
        "пивкд": "прикладная информатика в компьютерном дизайне",
        "пкд": "прикладная информатика в компьютерном дизайне",
        "прикладная информатика в компьютерном дизайне": "прикладная информатика в компьютерном дизайне",
    }


def _extract_direction_from_query(query: str) -> str:
    q = _safe_text(query).lower()
    for key, value in _extract_direction_terms().items():
        if key in q:
            return value
    return ""


def embed_texts(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)

    model = get_model()
    prepared = [_safe_text(t)[:1200] for t in texts]
    embeddings = model.encode(
        prepared,
        batch_size=32,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return np.array(embeddings, dtype=np.float32)


def embed_query(query: str) -> np.ndarray:
    arr = embed_texts([query])
    return arr[0] if len(arr) else np.zeros((384,), dtype=np.float32)


class VectorStore:
    """
    Простая локальная векторная БД на NumPy.
    """

    def __init__(self):
        self.vectors: np.ndarray | None = None
        self.chunks: list[dict] = []
        self.chunk_ids: list[str] = []
        self._cache_key: str = ""

    def build(self, chunks: list[dict]) -> None:
        if not chunks:
            self.vectors = None
            self.chunks = []
            self.chunk_ids = []
            self._cache_key = ""
            return

        normalized_chunks = []
        seen = set()

        for chunk in chunks:
            uid = _chunk_uid(chunk)
            if uid in seen:
                continue
            seen.add(uid)
            normalized_chunks.append({
                "source": chunk.get("source", ""),
                "type": chunk.get("type", ""),
                "text": _safe_text(chunk.get("text", "")),
            })

        cache_key = self._make_cache_key(normalized_chunks)
        cached = self._load_cache(cache_key)
        if cached is not None:
            self.vectors, self.chunks, self.chunk_ids = cached
            self._cache_key = cache_key
            print(f"[VectorStore] Загружен кэш: {len(self.chunks)} чанков")
            return

        print(f"[VectorStore] Векторизуем {len(normalized_chunks)} чанков...")
        texts = [c["text"][:1200] for c in normalized_chunks]
        vectors = embed_texts(texts)

        self.vectors = vectors
        self.chunks = normalized_chunks
        self.chunk_ids = [_chunk_uid(c) for c in normalized_chunks]
        self._cache_key = cache_key

        self._save_cache(cache_key, self.vectors, self.chunks, self.chunk_ids)
        print(f"[VectorStore] Готово. Размерность: {self.vectors.shape}")

    def search(self, query: str, top_k: int = 8, min_score: float = 0.15) -> list[dict]:
        if self.vectors is None or len(self.chunks) == 0:
            return []

        q_vec = embed_query(query)
        scores = self.vectors @ q_vec
        order = np.argsort(scores)[::-1]

        direction = _extract_direction_from_query(query)
        query_lower = _safe_text(query).lower()
        results = []

        for i in order:
            score = float(scores[i])
            if len(results) >= top_k * 3:
                break
            if score < min_score:
                continue

            chunk = dict(self.chunks[i])
            text_low = chunk["text"].lower()
            src_low = _safe_text(chunk.get("source")).lower()
            ctype = chunk.get("type", "")

            bonus = 0.0

            if direction:
                if direction in text_low or direction in src_low:
                    bonus += 0.08

            if ctype == "curriculum_plan" and any(k in query_lower for k in ["учебный план", "дисциплин", "семестр", "курс"]):
                bonus += 0.06

            if ctype == "competencies" and "компетен" in query_lower:
                bonus += 0.06

            if ctype == "document" and any(k in query_lower for k in ["документ", "pdf", "файл", "приказ", "методич"]):
                bonus += 0.05

            final_score = score + bonus
            chunk["_vector_score"] = round(score, 6)
            chunk["_vector_final_score"] = round(final_score, 6)
            chunk["_chunk_uid"] = self.chunk_ids[i]
            results.append(chunk)

        results.sort(key=lambda x: x.get("_vector_final_score", x.get("_vector_score", 0.0)), reverse=True)
        return results[:top_k]

    @staticmethod
    def _make_cache_key(chunks: list[dict]) -> str:
        content = "||".join(
            f"{c.get('source','')}|{c.get('type','')}|{c.get('text','')[:300]}"
            for c in chunks
        )
        return hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()

    def _load_cache(self, key: str):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        vec_path = CACHE_DIR / f"{key}.npy"
        meta_path = CACHE_DIR / f"{key}.json"

        if vec_path.exists() and meta_path.exists():
            try:
                vectors = np.load(str(vec_path))
                with open(meta_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                chunks = payload.get("chunks", [])
                chunk_ids = payload.get("chunk_ids", [_chunk_uid(c) for c in chunks])
                if len(chunks) == len(vectors):
                    return vectors, chunks, chunk_ids
            except Exception:
                pass
        return None

    def _save_cache(self, key: str, vectors: np.ndarray, chunks: list[dict], chunk_ids: list[str]) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.save(str(CACHE_DIR / f"{key}.npy"), vectors)
        with open(CACHE_DIR / f"{key}.json", "w", encoding="utf-8") as f:
            json.dump({
                "chunks": chunks,
                "chunk_ids": chunk_ids,
            }, f, ensure_ascii=False)


def _deduplicate_chunks(chunks: list[dict]) -> list[dict]:
    result = []
    seen = set()

    for chunk in chunks:
        uid = chunk.get("_chunk_uid") or _chunk_uid(chunk)
        if uid in seen:
            continue
        seen.add(uid)
        result.append(chunk)

    return result


def hybrid_search(
    query: str,
    chunks: list[dict],
    vector_store: VectorStore,
    top_k: int = 10,
    bm25_weight: float = 0.45,
    vector_weight: float = 0.55,
) -> list[dict]:
    """
    Гибридный поиск: BM25 + vector similarity + простое rerank-объединение.
    """
    from .rag import retrieve as bm25_retrieve

    if not chunks:
        return []

    fetch_k = min(max(top_k * 4, 12), len(chunks))
    bm25_results = bm25_retrieve(query, chunks, top_k=fetch_k)
    vec_results = vector_store.search(query, top_k=fetch_k)

    combined = {}

    for rank, chunk in enumerate(bm25_results):
        uid = _chunk_uid(chunk)
        item = dict(chunk)
        item["_chunk_uid"] = uid
        item["_bm25_rank"] = rank
        item["_bm25_rrf"] = bm25_weight / (60 + rank)
        combined.setdefault(uid, {}).update(item)

    for rank, chunk in enumerate(vec_results):
        uid = chunk.get("_chunk_uid") or _chunk_uid(chunk)
        item = dict(chunk)
        item["_chunk_uid"] = uid
        item["_vector_rank"] = rank
        item["_vector_rrf"] = vector_weight / (60 + rank)

        if uid not in combined:
            combined[uid] = item
        else:
            combined[uid].update(item)

    direction = _extract_direction_from_query(query)
    query_lower = _safe_text(query).lower()

    rescored = []
    for uid, item in combined.items():
        text_low = _safe_text(item.get("text")).lower()
        src_low = _safe_text(item.get("source")).lower()
        ctype = item.get("type", "")

        score = float(item.get("_bm25_rrf", 0.0)) + float(item.get("_vector_rrf", 0.0))

        if direction and (direction in text_low or direction in src_low):
            score += 0.025

        if ctype == "curriculum_plan" and any(k in query_lower for k in ["учебный план", "дисциплин", "курс", "семестр"]):
            score += 0.02

        if ctype == "competencies" and "компетен" in query_lower:
            score += 0.02

        if ctype == "document" and any(k in query_lower for k in ["документ", "pdf", "приказ", "методич"]):
            score += 0.015

        item["_hybrid_score"] = round(score, 6)
        rescored.append(item)

    rescored.sort(key=lambda x: x.get("_hybrid_score", 0.0), reverse=True)
    rescored = _deduplicate_chunks(rescored)

    return rescored[:top_k]


_vector_store = VectorStore()


def get_vector_store() -> VectorStore:
    return _vector_store