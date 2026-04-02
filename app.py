import base64
import io
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, request
from PIL import Image, ImageEnhance, ImageFilter, ImageOps


DEFAULT_MODEL = os.getenv("MODEL", "gemini-2.5-flash")
DEFAULT_MAX_WORKERS = max(1, int(os.getenv("MAX_WORKERS", "6")))
DEFAULT_RECEIPT_UPSCALE = max(1.0, float(os.getenv("RECEIPT_UPSCALE_FACTOR", "2.0")))
DEFAULT_SHOPEE_UPSCALE = max(1.0, float(os.getenv("SHOPEE_UPSCALE_FACTOR", "1.2")))
DEFAULT_REQUEST_TIMEOUT = max(10, int(os.getenv("GEMINI_TIMEOUT_SECONDS", "90")))
RETRYABLE_CODES = {429, 500, 503, 504}
ALLOWED_TASKS = {"receipt", "shopee", "receipt_date", "shopee_date"}


app = Flask(__name__)


def load_api_keys():
    keys = []
    raw = os.getenv("API_KEYS", "")
    if raw:
        keys.extend([part.strip() for part in re.split(r"[\r\n,;]+", raw) if part.strip()])

    single = os.getenv("API_KEY", "").strip()
    if single:
        keys.append(single)

    indexed_keys = []
    for name, value in os.environ.items():
        if re.match(r"^API_KEY_\d+$", name) and value.strip():
            indexed_keys.append((int(name.split("_")[2]), value.strip()))
    indexed_keys.sort(key=lambda item: item[0])
    keys.extend([value for _, value in indexed_keys])

    deduped = []
    for key in keys:
        if key not in deduped:
            deduped.append(key)

    if not deduped:
        raise RuntimeError("No API keys configured. Set API_KEYS or API_KEY.")

    return deduped


class ApiKeyRouter:
    def __init__(self, keys):
        self.keys = keys
        self._index = 0
        self._lock = threading.Lock()

    def ordered_keys(self):
        if len(self.keys) <= 1:
            return list(self.keys)

        with self._lock:
            start = self._index
            self._index = (self._index + 1) % len(self.keys)

        return self.keys[start:] + self.keys[:start]


API_KEYS = load_api_keys()
KEY_ROUTER = ApiKeyRouter(API_KEYS)
BEARER_TOKEN = os.getenv("BEARER_TOKEN", "").strip()


class GeminiApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


@app.get("/")
def root():
    return jsonify(build_health_payload())


@app.get("/healthz")
def healthz():
    return jsonify(build_health_payload())


@app.route("/ocr-batch", methods=["GET", "POST"])
def ocr_batch():
    try:
        if request.method == "GET":
            return jsonify(build_health_payload())

        require_bearer_token_if_enabled(request)

        payload = request.get_json(silent=True) or {}
        task = str(payload.get("task", "")).strip()
        prompt = str(payload.get("prompt", "")).strip()
        model = str(payload.get("model", "")).strip() or DEFAULT_MODEL
        items = payload.get("items")

        if task not in ALLOWED_TASKS:
            return jsonify({"success": False, "error": f"Unsupported task: {task}"}), 400

        if not isinstance(items, list) or not items:
            return jsonify({"success": False, "error": "items must be a non-empty array"}), 400

        if not prompt:
            prompt = get_default_prompt(task)

        max_workers = min(DEFAULT_MAX_WORKERS, len(items))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_item, item, task, prompt, model) for item in items]
            results = [future.result() for future in futures]

        return jsonify({
            "success": True,
            "task": task,
            "count": len(results),
            "results": results,
        })
    except GeminiApiError as error:
        return jsonify({"success": False, "error": str(error)}), error.status_code or 500
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


def build_health_payload():
    return {
        "ok": True,
        "service": "cloud-run-ocr",
        "active_keys": len(API_KEYS),
        "default_model": DEFAULT_MODEL,
        "max_workers": DEFAULT_MAX_WORKERS,
    }


def require_bearer_token_if_enabled(req):
    if not BEARER_TOKEN:
        return

    actual = str(req.headers.get("Authorization", "")).strip()
    expected = f"Bearer {BEARER_TOKEN}"
    if actual != expected:
        raise GeminiApiError("Unauthorized", 401)


def process_item(item, task, prompt, model):
    try:
        file_name = str(item.get("file_name", "")).strip()
        mime_type = str(item.get("mime_type", "")).strip() or "image/jpeg"
        base64_data = str(item.get("base64_data", "")).strip()
        if not base64_data:
            return {"ok": False, "error": "Missing base64_data"}

        raw_bytes = base64.b64decode(base64_data)
        processed_bytes, processed_mime = preprocess_bytes(raw_bytes, mime_type, task)
        processed_b64 = base64.b64encode(processed_bytes).decode("ascii")
        structured = call_gemini_json(prompt, processed_b64, processed_mime, model)

        return {
            "ok": True,
            "data": structured,
            "meta": {
                "file_name": file_name,
                "mime_type": processed_mime,
            },
        }
    except Exception as error:
        return {"ok": False, "error": str(error)}


def preprocess_bytes(raw_bytes, mime_type, task):
    if not mime_type.startswith("image/"):
        return raw_bytes, mime_type

    try:
        image = Image.open(io.BytesIO(raw_bytes))
        image = ImageOps.exif_transpose(image)

        upscale_factor = DEFAULT_RECEIPT_UPSCALE if task in {"receipt", "receipt_date"} else DEFAULT_SHOPEE_UPSCALE
        max_dimension = max(image.width, image.height)
        if upscale_factor > 1.0 and max_dimension < 2600:
            new_size = (
                max(1, int(image.width * upscale_factor)),
                max(1, int(image.height * upscale_factor)),
            )
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        if task in {"receipt", "receipt_date"}:
            image = ImageEnhance.Contrast(image).enhance(1.18)
            image = image.filter(ImageFilter.UnsharpMask(radius=1.4, percent=135, threshold=3))
        else:
            image = ImageEnhance.Contrast(image).enhance(1.05)

        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=95, optimize=True)
        return buffer.getvalue(), "image/jpeg"
    except Exception:
        return raw_bytes, mime_type


def call_gemini_json(prompt, base64_data, mime_type, model):
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": base64_data,
                    }
                },
            ]
        }],
        "generationConfig": {
            "temperature": 0
        },
    }

    last_error = None
    for attempt, api_key in enumerate(KEY_ROUTER.ordered_keys()):
        try:
            return call_gemini_api(payload, api_key, model)
        except GeminiApiError as error:
            last_error = error
            if error.status_code not in RETRYABLE_CODES or attempt == len(API_KEYS) - 1:
                raise
            time.sleep(min(0.25 * (attempt + 1), 1.0))

    raise last_error or GeminiApiError("Gemini request failed")


def call_gemini_api(payload, api_key, model):
    url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={api_key}"
    response = requests.post(
        url,
        json=payload,
        timeout=DEFAULT_REQUEST_TIMEOUT,
    )

    if response.status_code != 200:
        raise GeminiApiError(
            f"API ERROR ({response.status_code}): {response.text}",
            response.status_code,
        )

    data = response.json()
    output = extract_text(data)
    if not output:
        raise GeminiApiError("Gemini returned an empty response")

    json_text = extract_json_object(output)
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as error:
        raise GeminiApiError(f"JSON parse failed: {error} | {json_text}") from error


def extract_text(response_json):
    texts = []
    for candidate in response_json.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if part.get("text"):
                texts.append(part["text"])
    return "\n".join(texts).strip()


def extract_json_object(text):
    cleaned = re.sub(r"```json|```", "", str(text or ""), flags=re.IGNORECASE).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return cleaned
    return cleaned[start:end + 1]


def get_default_prompt(task):
    prompts = {
        "receipt": "Return strict JSON with date, store, ssm, receipt_no, total.",
        "receipt_date": "Return strict JSON with only date.",
        "shopee": "Return strict JSON with date, order_id, store, product, total.",
        "shopee_date": "Return strict JSON with only date.",
    }
    return prompts[task]


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
