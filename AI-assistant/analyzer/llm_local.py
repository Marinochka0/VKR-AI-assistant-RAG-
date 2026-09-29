import os
import re
import requests
from pathlib import Path
from django.conf import settings

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

def _clean_output(text: str) -> str:
    cleaned = re.sub(
        r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff01-\uffee]+',
        '',
        text or ''
    )
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    cleaned = re.sub(r' +', ' ', cleaned)
    return cleaned.strip()

class LocalLLM:
    def __init__(self):
        self._pipeline = None
        self._llama = None
        self._mode = None
        self._initialized = False

    def _init(self):
        if self._initialized:
            return
        self._initialized = True

        if self._try_llama_cpp():
            return
        if self._try_transformers():
            return
        self._mode = "api"

    def _try_llama_cpp(self) -> bool:
        try:
            from llama_cpp import Llama
        except ImportError:
            return False

        gguf_files = list(MODELS_DIR.glob("*.gguf")) if MODELS_DIR.exists() else []
        if not gguf_files:
            return False

        try:
            self._llama = Llama(
                model_path=str(gguf_files[0]),
                n_ctx=8192,
                n_threads=os.cpu_count() or 4,
                n_gpu_layers=-1,
                verbose=False,
            )
            self._mode = "llama_cpp"
            return True
        except Exception:
            return False

    def _try_transformers(self) -> bool:
        try:
            import torch
            from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline
        except ImportError:
            return False

        local_model = MODELS_DIR / "transformers"
        model_id = str(local_model) if local_model.exists() else "Qwen/Qwen2.5-1.5B-Instruct"

        try:
            device = "cuda" if _cuda_available() else "cpu"
            dtype = _get_dtype(device)

            tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
            model = AutoModelForCausalLM.from_pretrained(
                model_id,
                torch_dtype=dtype,
                device_map="auto" if device == "cuda" else None,
                trust_remote_code=True,
            )

            if device == "cpu":
                model = model.to("cpu")

            self._pipeline = pipeline(
                "text-generation",
                model=model,
                tokenizer=tokenizer,
                device_map="auto" if device == "cuda" else -1,
            )

            self._mode = "transformers"
            return True
        except Exception:
            return False

    def _sanitize_history(self, history: list) -> list:
        cleaned = []
        for m in (history or []):
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role not in ("user", "assistant") or not content:
                continue
            cleaned.append({"role": role, "content": content[:1200]})
        return cleaned[-2:]

    def _build_messages(self, system_prompt, user_message, history):
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self._sanitize_history(history))
        messages.append({"role": "user", "content": user_message})
        return messages

    def generate(self, system_prompt: str, user_message: str, history: list) -> str:
        self._init()
        if self._mode == "llama_cpp":
            return self._generate_llama(system_prompt, user_message, history)
        if self._mode == "transformers":
            return self._generate_transformers(system_prompt, user_message, history)
        return self._generate_api(system_prompt, user_message, history)

    def _generate_llama(self, system_prompt, user_message, history) -> str:
        messages = self._build_messages(system_prompt, user_message, history)
        prompt = ""
        for m in messages:
            prompt += f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n"
        prompt += "<|im_start|>assistant\n"

        output = self._llama(
            prompt,
            max_tokens=2200,
            temperature=0.1,
            top_p=0.9,
            repeat_penalty=1.08,
            stop=["<|im_end|>", "<|im_start|>"],
        )
        raw = output["choices"][0]["text"].strip()
        return _clean_output(raw)

    def _generate_transformers(self, system_prompt, user_message, history) -> str:
        messages = self._build_messages(system_prompt, user_message, history)
        outputs = self._pipeline(
            messages,
            max_new_tokens=2000,
            return_full_text=False,
            do_sample=False,
            repetition_penalty=1.08,
        )
        return _clean_output(outputs[0]["generated_text"].strip())

    def _generate_api(self, system_prompt, user_message, history) -> str:
        api_key = settings.OPENROUTER_API_KEY
        messages = self._build_messages(system_prompt, user_message, history)

        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "openai/gpt-4o-mini",
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 2200
            },
            timeout=120,
        )
        resp.raise_for_status()
        return _clean_output(resp.json()["choices"][0]["message"]["content"])

    @property
    def mode(self) -> str:
        self._init()
        return self._mode or "api"

def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False

def _get_dtype(device: str):
    try:
        import torch
        return torch.float16 if device == "cuda" else torch.float32
    except ImportError:
        return None

_llm = LocalLLM()

def get_llm() -> LocalLLM:
    return _llm