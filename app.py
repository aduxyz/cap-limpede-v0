from __future__ import annotations

import json
import os
import re
import hashlib
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

try:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import service_account
except (ModuleNotFoundError, ImportError):
    GoogleAuthRequest = None
    service_account = None


BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"
NEWS_PREVIEW_CSS = BASE_DIR / "news-preview.css"
TODO_FILE = BASE_DIR / "todo.txt"
BULK_CACHE_FILE = BASE_DIR / "bulk_cache.json"
REVIEW_CACHE_FILE = BASE_DIR / "review_cache.json"
HOST = os.getenv("CAP_LIMPEDE_HOST", "127.0.0.1")
PORT = int(os.getenv("CAP_LIMPEDE_PORT", "8000"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL_CANDIDATES = ["gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4"]
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
GOOGLE_LOG_SPREADSHEET_ID = os.getenv("GOOGLE_LOG_SPREADSHEET_ID", "1dFdjWPVn2lMoXwGF85gZ-02klFQq6f89P7kFZsUbsCU")
GOOGLE_LOG_RANGE = os.getenv("GOOGLE_LOG_RANGE", "comparisons!A:M")
GOOGLE_LOG_SHEET_NAME = os.getenv("GOOGLE_LOG_SHEET_NAME", "comparisons")
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
MODEL_PRICING_PER_1M = {
    "gpt-5.4": {"input": 2.50, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "output": 1.25},
}

BULK_LOCK = threading.Lock()
REVIEW_CACHE_LOCK = threading.Lock()
BULK_TASK_QUEUE: queue.Queue[dict[str, Any] | None] = queue.Queue()
BULK_WORKER_THREADS: list[threading.Thread] = []
BULK_WORKER_COUNT = max(2, min(6, (os.cpu_count() or 4)))
BULK_JOB: dict[str, Any] = {
    "job_id": None,
    "running": False,
    "total": 0,
    "completed": 0,
    "failed": 0,
    "started_at": None,
    "finished_at": None,
    "models": [],
}


PROMPT_DEFAULT = """Ești editor de știri în limba română, cu rol de „cap limpede”.
Propune doar modificări utile pentru corectitudine și claritate.

Nivel 1:
- typo-uri evidente
- spațiere greșită la punctuație
- lipsă diacritice
- cuvinte repetate
- forme gramaticale incorecte

Nivel 2:
- doar sugestii care cresc clar claritatea

Nu propune:
- diacritice vechi vs noi
- ghilimele
- fact-checking
- rescrieri stilistice

Răspunde exclusiv cu JSON valid:

{
  "suggestions": [
    {
      "id": "string",
      "level": 1,
      "original": "fragment exact din content_text",
      "proposed": "fragmentul propus",
      "reason": "motiv scurt"
    }
  ]
}

Reguli:
- fără duplicate
- fără suprapuneri
- dacă nu există sugestii: "suggestions": []
- level doar 1 sau 2
"""

PROMPT_LEVEL_1_ONLY = """Ești editor de știri în limba română, cu rol de „cap limpede”.
Propune doar corecții evidente, necesare.

Caută exclusiv:
- typo-uri evidente
- spațiere greșită la punctuație
- lipsă diacritice
- cuvinte repetate
- forme gramaticale incorecte

Nu propune:
- sugestii de claritate
- reformulări de stil
- rescrieri opționale
- diacritice vechi vs noi
- ghilimele
- fact-checking

Răspunde exclusiv cu JSON valid:

{
  "suggestions": [
    {
      "id": "string",
      "level": 1,
      "original": "fragment exact din content_text",
      "proposed": "fragmentul propus",
      "reason": "motiv scurt"
    }
  ]
}

Reguli:
- fără duplicate
- fără suprapuneri
- dacă nu există sugestii: "suggestions": []
- level este mereu 1
"""


def normalize_levels(levels: Any) -> list[int]:
    if not isinstance(levels, list):
        return [1, 2]
    normalized: list[int] = []
    for level in levels:
        if level in (1, 2) and level not in normalized:
            normalized.append(level)
    if 1 not in normalized:
        normalized.insert(0, 1)
    return normalized or [1, 2]


def resolve_review_prompt(prompt: str, levels: list[int]) -> str:
    normalized_prompt = prompt.strip()
    normalized_levels = normalize_levels(levels)
    if normalized_levels == [1]:
        if not normalized_prompt or normalized_prompt in {PROMPT_DEFAULT, PROMPT_LEVEL_1_ONLY}:
            return PROMPT_LEVEL_1_ONLY
        return (
            f"{normalized_prompt}\n\n"
            "Regulă suplimentară obligatorie:\n"
            "- întoarce exclusiv sugestii de nivel 1\n"
            "- nu propune sugestii de claritate sau de nivel 2\n"
            "- câmpul `level` este mereu 1"
        )
    return normalized_prompt or PROMPT_DEFAULT


RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "level": {"type": "integer", "enum": [1, 2]},
                    "original": {"type": "string"},
                    "proposed": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "level", "original", "proposed", "reason"],
            },
        },
    },
    "required": ["suggestions"],
}


class HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"p", "br", "div", "section", "article", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()


class NewsArticleExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.in_h1 = False
        self.title_parts: list[str] = []
        self.content_div_depth = 0
        self.content_html_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        classes = set((attrs_dict.get("class") or "").split())
        if tag.lower() == "h1" and not self.title_parts:
            self.in_h1 = True
        if tag.lower() == "div" and self.content_div_depth == 0 and "art-content" in classes:
            self.content_div_depth = 1
            return
        if self.content_div_depth > 0:
            self.content_html_parts.append(self.get_starttag_text())
            if tag.lower() == "div":
                self.content_div_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.content_div_depth > 0:
            self.content_html_parts.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "h1":
            self.in_h1 = False
        if self.content_div_depth > 0:
            if tag.lower() == "div":
                self.content_div_depth -= 1
                if self.content_div_depth > 0:
                    self.content_html_parts.append(f"</{tag}>")
            else:
                self.content_html_parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.in_h1:
            self.title_parts.append(data)
        if self.content_div_depth > 0:
            self.content_html_parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self.content_div_depth > 0:
            self.content_html_parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self.content_div_depth > 0:
            self.content_html_parts.append(f"&#{name};")

    def get_article(self) -> tuple[str, str]:
        title = re.sub(r"\s+", " ", unescape("".join(self.title_parts))).strip()
        content_html = "".join(self.content_html_parts).strip()
        return title, content_html


@dataclass
class ParsedDocument:
    source_format: str
    title: str
    content_text: str
    raw: str


@dataclass
class MaskedDocument:
    analysis_text: str
    analysis_to_original: list[int | None]
    quote_map: list[dict[str, Any]]


def html_to_text(value: str) -> str:
    parser = HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.get_text()


def trim_news_ro_content_html(content_html: str) -> str:
    stop_markers = [
        r'<div[^>]*class="[^"]*links-shortcuts[^"]*"',
        r'<div[^>]*class="[^"]*info-news[^"]*"',
        r'<div[^>]*class="[^"]*list-tags[^"]*"',
        r"Articolul de mai sus este destinat exclusiv informării dumneavoastră personale",
        r"Află mai multe despre",
    ]
    cut_positions = []
    for pattern in stop_markers:
        match = re.search(pattern, content_html, flags=re.IGNORECASE)
        if match:
            cut_positions.append(match.start())
    if cut_positions:
        content_html = content_html[: min(cut_positions)]
    return content_html.strip()


def looks_like_html(value: str) -> bool:
    return bool(re.search(r"<[a-zA-Z][^>]*>", value))


def normalize_news_ro_url(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc not in {"news.ro", "www.news.ro"}:
        return None

    if parsed.path == "/flux/" and parsed.fragment.isdigit():
        return f"https://www.news.ro/rd-{parsed.fragment}"

    match = re.fullmatch(r"/rd-(\d+)", parsed.path)
    if match:
        return f"https://www.news.ro/rd-{match.group(1)}"

    return None


def fetch_news_ro_article_html(value: str) -> tuple[str, str] | None:
    normalized_url = normalize_news_ro_url(value)
    if not normalized_url:
        return None

    req = request.Request(
        normalized_url,
        headers={"User-Agent": "Mozilla/5.0 CapLimpede/0.1"},
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            html = response.read().decode("utf-8", errors="ignore")
    except error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        raise RuntimeError(f"Nu am putut citi articolul news.ro: {exc.code}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Nu m-am putut conecta la news.ro: {exc.reason}") from exc

    parser = NewsArticleExtractor()
    parser.feed(html)
    parser.close()
    title, content_html = parser.get_article()
    content_html = trim_news_ro_content_html(content_html)
    if not title or not content_html:
        raise RuntimeError("Nu am putut extrage titlul și conținutul articolului din news.ro.")

    return title, content_html


def resolve_source_input(raw: str) -> str:
    article = fetch_news_ro_article_html(raw.strip())
    if article is None:
        return raw
    title, content_html = article
    return json.dumps({"title": title, "content": content_html}, ensure_ascii=False)


def parse_source(raw: str) -> ParsedDocument:
    resolved_raw = resolve_source_input(raw)
    text = resolved_raw.strip()
    if not text:
        return ParsedDocument(source_format="text", title="", content_text="", raw=resolved_raw)

    try:
        parsed_json = json.loads(text)
    except json.JSONDecodeError:
        parsed_json = None

    if isinstance(parsed_json, dict) and isinstance(parsed_json.get("title"), str) and isinstance(parsed_json.get("content"), str):
        title = parsed_json["title"].strip()
        content_html = parsed_json["content"]
        content_text = html_to_text(content_html) if looks_like_html(content_html) else content_html.strip()
        if title and content_text:
            content_text = f"{title}\n\n{content_text}"
        elif title:
            content_text = title
        return ParsedDocument(source_format="json_article", title=title, content_text=content_text.strip(), raw=resolved_raw)

    if looks_like_html(text):
        return ParsedDocument(source_format="html", title="", content_text=html_to_text(text), raw=resolved_raw)

    return ParsedDocument(source_format="text", title="", content_text=text, raw=resolved_raw)


def get_preview_payload(document: ParsedDocument) -> dict[str, str]:
    title = document.title
    content_html = ""
    text = (document.raw or "").strip()

    if document.source_format == "json_article":
        try:
            parsed_json = json.loads(text)
        except json.JSONDecodeError:
            parsed_json = None
        if isinstance(parsed_json, dict):
            title = str(parsed_json.get("title", "") or title)
            content_html = str(parsed_json.get("content", "") or "")
    elif document.source_format == "html":
        content_html = text

    return {
        "title": title,
        "content_html": content_html,
    }


def normalize_legacy_romanian_diacritics(value: str) -> str:
    translation_table = str.maketrans(
        {
            "ş": "ș",
            "Ş": "Ș",
            "ţ": "ț",
            "Ţ": "Ț",
        }
    )
    return value.translate(translation_table)


def build_quote_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    quote_chars = {'"', "„", "”", "“", "«", "»"}
    open_index: int | None = None

    for idx, char in enumerate(text):
        if char not in quote_chars:
            continue
        if open_index is None:
            open_index = idx
        else:
            ranges.append((open_index, idx + 1))
            open_index = None

    return ranges


def build_masked_document(document: ParsedDocument) -> MaskedDocument:
    text = document.content_text
    quote_ranges = build_quote_ranges(text)
    if not quote_ranges:
        return MaskedDocument(analysis_text=text, analysis_to_original=list(range(len(text))), quote_map=[])

    analysis_parts: list[str] = []
    analysis_to_original: list[int | None] = []
    quote_map: list[dict[str, Any]] = []
    cursor = 0

    for index, (start, end) in enumerate(quote_ranges, start=1):
        if start > cursor:
            segment = text[cursor:start]
            analysis_parts.append(segment)
            analysis_to_original.extend(range(cursor, start))

        placeholder = f"__QUOTE_{index}__"
        analysis_parts.append(placeholder)
        analysis_to_original.extend([None] * len(placeholder))
        quote_map.append(
            {
                "index": index,
                "placeholder": placeholder,
                "text": text[start:end],
                "start": start,
                "end": end,
            }
        )
        cursor = end

    if cursor < len(text):
        trailing = text[cursor:]
        analysis_parts.append(trailing)
        analysis_to_original.extend(range(cursor, len(text)))

    return MaskedDocument(
        analysis_text="".join(analysis_parts),
        analysis_to_original=analysis_to_original,
        quote_map=quote_map,
    )


def filter_suggestions(document_text: str, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    seen_originals: set[str] = set()

    for item in suggestions:
        original = str(item.get("original", ""))
        proposed = str(item.get("proposed", ""))
        if not original or not proposed:
            continue
        if original not in document_text:
            continue
        if normalize_legacy_romanian_diacritics(original) == normalize_legacy_romanian_diacritics(proposed):
            continue
        if original in seen_originals:
            continue
        seen_originals.add(original)
        filtered.append(item)

    return filtered


def remap_suggestions_from_analysis(
    document_text: str,
    masked_document: MaskedDocument,
    suggestions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not masked_document.quote_map:
        return filter_suggestions(document_text, suggestions)

    filtered: list[dict[str, Any]] = []
    seen_originals: set[str] = set()
    taken_ranges: list[tuple[int, int]] = []
    placeholder_pattern = re.compile(r"__QUOTE_\d+__")
    scan_positions: dict[str, int] = {}

    for item in suggestions:
        original = str(item.get("original", ""))
        proposed = str(item.get("proposed", ""))
        if not original or not proposed:
            continue
        if placeholder_pattern.search(original) or placeholder_pattern.search(proposed):
            continue
        if normalize_legacy_romanian_diacritics(original) == normalize_legacy_romanian_diacritics(proposed):
            continue

        search_from = scan_positions.get(original, 0)
        matched = False

        while True:
            start = masked_document.analysis_text.find(original, search_from)
            if start == -1:
                break
            end = start + len(original)
            mapped_slice = masked_document.analysis_to_original[start:end]
            if mapped_slice and all(index is not None for index in mapped_slice):
                original_start = mapped_slice[0]
                original_end = mapped_slice[-1] + 1
                original_span = document_text[original_start:original_end]
                overlaps = any(original_start < taken_end and original_end > taken_start for taken_start, taken_end in taken_ranges)
                if original_span == original and not overlaps and original not in seen_originals:
                    normalized = dict(item)
                    normalized["original"] = original_span
                    filtered.append(normalized)
                    seen_originals.add(original)
                    taken_ranges.append((original_start, original_end))
                    scan_positions[original] = end
                    matched = True
                    break
            search_from = start + 1

        if not matched:
            scan_positions[original] = search_from

    return filtered


def estimate_cost_usd(model: str, usage: dict[str, Any] | None) -> float | None:
    if not usage:
        return None
    pricing = MODEL_PRICING_PER_1M.get(model)
    if not pricing:
        return None
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    return round(
        (prompt_tokens / 1_000_000) * pricing["input"]
        + (completion_tokens / 1_000_000) * pricing["output"],
        6,
    )


def load_review_cache() -> dict[str, Any]:
    if not REVIEW_CACHE_FILE.is_file():
        return {"entries": {}}
    try:
        parsed = json.loads(REVIEW_CACHE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"entries": {}}
    entries = parsed.get("entries")
    if not isinstance(entries, dict):
        return {"entries": {}}
    return {"entries": entries}


def save_review_cache(cache: dict[str, Any]) -> None:
    temp_path = REVIEW_CACHE_FILE.with_suffix(".tmp")
    temp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(REVIEW_CACHE_FILE)


def build_review_cache_key(document: ParsedDocument, prompt: str, models: list[str], levels: list[int]) -> str:
    payload = {
        "source_format": document.source_format,
        "title": document.title,
        "content_text": document.content_text,
        "prompt": prompt,
        "models": models,
        "levels": normalize_levels(levels),
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_cached_review(document: ParsedDocument, prompt: str, models: list[str], levels: list[int]) -> dict[str, Any] | None:
    cache_key = build_review_cache_key(document, prompt, models, levels)
    with REVIEW_CACHE_LOCK:
        entry = load_review_cache()["entries"].get(cache_key)
    if not isinstance(entry, dict):
        return None
    result = entry.get("result")
    if not isinstance(result, dict):
        return None
    cached_result = json.loads(json.dumps(result))
    cached_result.setdefault("meta", {})
    cached_result["meta"]["cache"] = {
        "hit": True,
        "key": cache_key,
        "cached_at": entry.get("cached_at"),
    }
    return cached_result


def store_review_cache(document: ParsedDocument, prompt: str, models: list[str], levels: list[int], result: dict[str, Any]) -> None:
    cache_key = build_review_cache_key(document, prompt, models, levels)
    payload = json.loads(json.dumps(result))
    payload.setdefault("meta", {})
    payload["meta"]["cache"] = {
        "hit": False,
        "key": cache_key,
        "cached_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with REVIEW_CACHE_LOCK:
        cache = load_review_cache()
        cache.setdefault("entries", {})[cache_key] = {
            "cached_at": payload["meta"]["cache"]["cached_at"],
            "result": payload,
        }
        save_review_cache(cache)


def fallback_review(document: ParsedDocument, masked_document: MaskedDocument) -> dict[str, Any]:
    text = masked_document.analysis_text
    suggestions: list[dict[str, Any]] = []

    def add(level: int, original: str, proposed: str, reason: str) -> None:
        if original and original in text and original != proposed and not any(item["original"] == original for item in suggestions):
            suggestions.append(
                {
                    "id": f"s{len(suggestions) + 1}",
                    "level": level,
                    "original": original,
                    "proposed": proposed,
                    "reason": reason,
                }
            )

    for match in re.finditer(r"\b([^\s,]{1,40}),([^\s,]{1,40})\b", text):
        original = match.group(0)
        proposed = f"{match.group(1)}, {match.group(2)}"
        add(1, original, proposed, "Lipsește spațiul după virgulă.")
        if len(suggestions) >= 2:
            break

    add(1, "Titllurile", "Titlurile", "Typo evident.")
    add(1, "termiant", "terminat", "Typo evident.")
    add(1, "truth social", "Truth Social", "Nume propriu scris cu literă mică.")
    add(1, "unicredit", "UniCredit", "Nume propriu scris cu literă mică.")
    add(2, "principalele indice", "principalii indici", "Acordul corect la plural este „principalii indici”.")
    add(2, "nu mai văd", "nu mai vede", "Predicatul trebuie acordat cu subiectul „Banca germană”.")
    add(2, "în teritoriu negativ", "pe minus", "Mai clar și mai scurt.")
    add(2, "o companie nouă prin fuziune", "o nouă companie prin fuziune", "Ordine mai firească.")
    add(2, "fiind în expectativă", "așteptând", "Formulare mai directă.")

    suggestions = remap_suggestions_from_analysis(document.content_text, masked_document, suggestions)
    return {
        "model": "local-rules",
        "elapsed_ms": 0,
        "usage": None,
        "estimated_cost_usd": None,
        "suggestions": suggestions,
        "debug": {
            "used_fallback": True,
            "raw_model_content": json.dumps({"suggestions": suggestions}, ensure_ascii=False, indent=2),
            "post_filtered_suggestions": suggestions,
            "analysis_text": masked_document.analysis_text,
            "quote_count": len(masked_document.quote_map),
        },
    }


def call_openai_review_for_model(document: ParsedDocument, masked_document: MaskedDocument, prompt: str, model: str) -> dict[str, Any]:
    user_payload = {
        "document": {
            "source_format": document.source_format,
            "title": document.title,
            "content_text": masked_document.analysis_text,
        }
    }
    started_at = time.perf_counter()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "news_review",
                "strict": True,
                "schema": RESPONSE_SCHEMA,
            },
        },
    }

    req = request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=120) as response:
            raw_api_response = response.read().decode("utf-8")
            data = json.loads(raw_api_response)
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI API error: {exc.code} {details}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Nu m-am putut conecta la OpenAI API: {exc.reason}") from exc

    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("Răspuns invalid de la OpenAI API.") from exc

    suggestions = remap_suggestions_from_analysis(document.content_text, masked_document, parsed.get("suggestions", []))
    usage = data.get("usage") if isinstance(data, dict) else None
    return {
        "model": model,
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000),
        "usage": usage,
        "estimated_cost_usd": estimate_cost_usd(model, usage),
        "suggestions": suggestions,
        "debug": {
            "used_fallback": False,
            "raw_model_content": content,
            "post_filtered_suggestions": suggestions,
            "request_document": user_payload["document"],
            "prompt": prompt,
            "analysis_text": masked_document.analysis_text,
            "quote_count": len(masked_document.quote_map),
        },
    }


def call_openai_review(
    document: ParsedDocument,
    prompt: str,
    models: list[str] | None = None,
    levels: list[int] | None = None,
) -> dict[str, Any]:
    selected_models = [model for model in (models or MODEL_CANDIDATES) if model in MODEL_CANDIDATES]
    if not selected_models:
        raise RuntimeError("Selectează cel puțin un model.")

    selected_levels = normalize_levels(levels)
    resolved_prompt = resolve_review_prompt(prompt, selected_levels)
    cached_result = get_cached_review(document, resolved_prompt, selected_models, selected_levels)
    if cached_result is not None:
        return cached_result

    masked_document = build_masked_document(document)
    preview = get_preview_payload(document)
    if not OPENAI_API_KEY:
        runs = [fallback_review(document, masked_document) | {"model": model} for model in selected_models]
    else:
        with ThreadPoolExecutor(max_workers=len(selected_models)) as executor:
            futures = [
                executor.submit(call_openai_review_for_model, document, masked_document, resolved_prompt, model)
                for model in selected_models
            ]
            runs = [future.result() for future in futures]

    runs.sort(key=lambda item: selected_models.index(item["model"]))
    result = {
        "run_id": uuid.uuid4().hex[:12],
        "document": {
            "source_format": document.source_format,
            "title": document.title,
            "content_text": document.content_text,
            "preview_title": preview["title"],
            "preview_html": preview["content_html"],
        },
        "runs": runs,
        "meta": {
            "provider": "openai" if OPENAI_API_KEY else "fallback",
            "models": selected_models,
            "levels": selected_levels,
        },
    }
    store_review_cache(document, resolved_prompt, selected_models, selected_levels, result)
    return result


def build_google_credentials():
    if not GOOGLE_SERVICE_ACCOUNT_FILE:
        return None
    if GoogleAuthRequest is None or service_account is None:
        raise RuntimeError(
            "Lipsesc librăriile Google pentru Sheets logging. Instalează `google-auth` în mediul curent."
        )
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=[GOOGLE_SHEETS_SCOPE],
    )
    credentials.refresh(GoogleAuthRequest())
    return credentials


def google_api_request(method: str, url: str, credentials, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="ignore")
            return json.loads(raw) if raw else None
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Google Sheets API error: {exc.code} {details}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Nu m-am putut conecta la Google Sheets API: {exc.reason}") from exc


def append_runs_to_google_sheet(document: ParsedDocument, result: dict[str, Any]) -> None:
    if not GOOGLE_SERVICE_ACCOUNT_FILE:
        return
    credentials = build_google_credentials()

    run_id = result["run_id"]
    logged_at = time.strftime("%Y-%m-%d %H:%M:%S")
    runs = result.get("runs", [])
    total_cost = sum(
        run.get("estimated_cost_usd", 0.0)
        for run in runs
        if isinstance(run.get("estimated_cost_usd"), (int, float))
    )
    values = []
    for run in runs:
        usage = run.get("usage") or {}
        estimated_cost = run.get("estimated_cost_usd")
        cost_share_pct = None
        if total_cost > 0 and isinstance(estimated_cost, (int, float)):
            cost_share_pct = round((estimated_cost / total_cost) * 100, 2)
        values.append(
            [
                logged_at,
                run_id,
                len(document.content_text or ""),
                run.get("model", ""),
                run.get("elapsed_ms", ""),
                usage.get("total_tokens", ""),
                usage.get("completion_tokens", ""),
                len(run.get("suggestions", []) or []),
                estimated_cost if isinstance(estimated_cost, (int, float)) else "",
                cost_share_pct if cost_share_pct is not None else "",
                (run.get("debug") or {}).get("quote_count", ""),
                "",
                "",
            ]
        )

    if not values:
        return

    google_api_request(
        "POST",
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_LOG_SPREADSHEET_ID}:batchUpdate",
        credentials,
        {
            "requests": [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": 0,
                            "dimension": "ROWS",
                            "startIndex": 1,
                            "endIndex": 1 + len(values),
                        },
                        "inheritFromBefore": False,
                    }
                }
            ]
        },
    )
    google_api_request(
        "PUT",
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_LOG_SPREADSHEET_ID}/values/{GOOGLE_LOG_SHEET_NAME}!M1?valueInputOption=USER_ENTERED",
        credentials,
        {"range": f"{GOOGLE_LOG_SHEET_NAME}!M1", "majorDimension": "ROWS", "values": [["best_vote"]]},
    )
    google_api_request(
        "PUT",
        (
            f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_LOG_SPREADSHEET_ID}/values/"
            f"{GOOGLE_LOG_SHEET_NAME}!A2:M{1 + len(values)}?valueInputOption=USER_ENTERED"
        ),
        credentials,
        {"range": f"{GOOGLE_LOG_SHEET_NAME}!A2:M{1 + len(values)}", "majorDimension": "ROWS", "values": values},
    )


def update_vote_in_google_sheet(run_id: str, best_model: str) -> dict[str, Any]:
    if not GOOGLE_SERVICE_ACCOUNT_FILE:
        raise RuntimeError("Google Sheets logging nu este configurat.")
    credentials = build_google_credentials()
    data = google_api_request(
        "GET",
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_LOG_SPREADSHEET_ID}/values/{GOOGLE_LOG_SHEET_NAME}!A:M",
        credentials,
    ) or {}
    rows = data.get("values", [])
    matched_rows: list[tuple[int, list[str]]] = []
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) > 1 and row[1] == run_id:
            matched_rows.append((idx, row))

    if not matched_rows:
        raise RuntimeError(f"Nu am găsit run_id `{run_id}` în sheet.")

    payload = {
        "valueInputOption": "USER_ENTERED",
        "data": [
            {
                "range": f"{GOOGLE_LOG_SHEET_NAME}!M{row_number}",
                "majorDimension": "ROWS",
                "values": [[1 if (len(row) > 3 and row[3] == best_model) else 0]],
            }
            for row_number, row in matched_rows
        ],
    }
    google_api_request(
        "POST",
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_LOG_SPREADSHEET_ID}/values:batchUpdate",
        credentials,
        payload,
    )
    return {"run_id": run_id, "best_model": best_model}


def validate_google_logging_configuration() -> None:
    if not GOOGLE_SERVICE_ACCOUNT_FILE:
        return
    if service_account is None or GoogleAuthRequest is None:
        raise RuntimeError(
            "Google Sheets logging este configurat, dar lipsesc dependențe runtime. "
            "Instalează în mediul curent: `python3 -m pip install google-auth requests`."
        )
    service_account_path = Path(GOOGLE_SERVICE_ACCOUNT_FILE).expanduser()
    if not service_account_path.is_file():
        raise RuntimeError(
            f"Google Sheets logging este configurat, dar fișierul credențialelor nu există: {service_account_path}"
        )


def read_todo_urls() -> list[str]:
    if not TODO_FILE.is_file():
        raise RuntimeError(f"Nu există fișierul todo.txt la {TODO_FILE}")
    urls = [
        line.strip()
        for line in TODO_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not urls:
        raise RuntimeError("todo.txt nu conține URL-uri de procesat.")
    return urls


def load_bulk_cache() -> dict[str, Any]:
    if not BULK_CACHE_FILE.is_file():
        return {"articles": [], "meta": {}}
    try:
        return json.loads(BULK_CACHE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"articles": [], "meta": {}}


def save_bulk_cache(cache: dict[str, Any]) -> None:
    temp_path = BULK_CACHE_FILE.with_suffix(".tmp")
    temp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(BULK_CACHE_FILE)


def build_bulk_cache(urls: list[str], prompt: str, models: list[str], levels: list[int]) -> dict[str, Any]:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "articles": [
            {
                "index": index,
                "url": url,
                "status": "pending",
                "review": None,
                "operator_state": {"runStates": {}, "best_model": None},
                "error": "",
            }
            for index, url in enumerate(urls)
        ],
        "meta": {
            "job_id": uuid.uuid4().hex[:12],
            "prompt": prompt,
            "models": models,
            "levels": normalize_levels(levels),
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    }


def get_bulk_status_payload() -> dict[str, Any]:
    with BULK_LOCK:
        cache = load_bulk_cache()
        return {
            "job": dict(BULK_JOB),
            "cache": cache,
        }


def save_bulk_article_operator_state(index: int, run_states: dict[str, Any], best_model: str | None) -> dict[str, Any]:
    with BULK_LOCK:
        cache = load_bulk_cache()
        articles = cache.get("articles", [])
        if index < 0 or index >= len(articles):
            raise RuntimeError("Articolul din cache nu a fost găsit.")
        article = articles[index]
        operator_state = article.get("operator_state") or {}
        operator_state["runStates"] = run_states or {}
        operator_state["best_model"] = best_model or None
        article["operator_state"] = operator_state
        cache.setdefault("meta", {})["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_bulk_cache(cache)
        return {"ok": True, "index": index}


def process_bulk_article(index: int, url: str, prompt: str, models: list[str], levels: list[int]) -> tuple[int, dict[str, Any]]:
    document = parse_source(url)
    result = call_openai_review(document, prompt, models, levels)
    if not ((result.get("meta") or {}).get("cache") or {}).get("hit"):
        append_runs_to_google_sheet(document, result)
    return index, result


def finalize_bulk_job_if_done_locked(cache: dict[str, Any]) -> None:
    articles = cache.get("articles", [])
    remaining = [article for article in articles if article.get("status") == "pending"]
    if remaining:
        BULK_JOB["running"] = True
        return
    BULK_JOB["running"] = False
    BULK_JOB["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cache.setdefault("meta", {})["updated_at"] = BULK_JOB["finished_at"]
    save_bulk_cache(cache)


def update_bulk_article_result(job_id: str, index: int, review_result: dict[str, Any] | None, error_message: str | None = None) -> None:
    with BULK_LOCK:
        cache = load_bulk_cache()
        if cache.get("meta", {}).get("job_id") != job_id:
            return
        articles = cache.get("articles", [])
        if index < 0 or index >= len(articles):
            return

        article = articles[index]
        article.setdefault("operator_state", {"runStates": {}, "best_model": None})
        article["status"] = "error" if error_message else "completed"
        article["review"] = review_result if review_result is not None else None
        article["error"] = error_message or ""
        cache.setdefault("meta", {})["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_bulk_cache(cache)
        BULK_JOB["completed"] += 1
        if error_message:
            BULK_JOB["failed"] += 1
        finalize_bulk_job_if_done_locked(cache)


def bulk_worker_loop() -> None:
    while True:
        task = BULK_TASK_QUEUE.get()
        if task is None:
            BULK_TASK_QUEUE.task_done()
            return
        try:
            _, review_result = process_bulk_article(
                task["index"],
                task["url"],
                task["prompt"],
                task["models"],
                task["levels"],
            )
        except Exception as exc:
            update_bulk_article_result(task["job_id"], task["index"], None, str(exc))
        else:
            update_bulk_article_result(task["job_id"], task["index"], review_result)
        finally:
            BULK_TASK_QUEUE.task_done()


def ensure_bulk_workers_started() -> None:
    if BULK_WORKER_THREADS:
        return
    for index in range(BULK_WORKER_COUNT):
        thread = threading.Thread(target=bulk_worker_loop, name=f"cap-limpede-bulk-{index + 1}", daemon=True)
        thread.start()
        BULK_WORKER_THREADS.append(thread)


def queue_bulk_articles(cache: dict[str, Any]) -> None:
    meta = cache.get("meta", {})
    job_id = str(meta.get("job_id") or "")
    prompt = str(meta.get("prompt", "")).strip() or PROMPT_DEFAULT
    models = [model for model in meta.get("models", []) if model in MODEL_CANDIDATES]
    levels = normalize_levels(meta.get("levels"))
    if not job_id or not models:
        return

    queued_any = False
    for article in cache.get("articles", []):
        if article.get("status") != "pending":
            continue
        try:
            document = parse_source(str(article.get("url", "")))
        except Exception:
            document = None
        if document is not None:
            cached_review = get_cached_review(document, resolve_review_prompt(prompt, levels), models, levels)
            if cached_review is not None:
                update_bulk_article_result(job_id, int(article.get("index", 0)), cached_review)
                continue
        BULK_TASK_QUEUE.put(
            {
                "job_id": job_id,
                "index": int(article.get("index", 0)),
                "url": str(article.get("url", "")),
                "prompt": prompt,
                "models": models,
                "levels": levels,
            }
        )
        queued_any = True

    with BULK_LOCK:
        latest_cache = load_bulk_cache()
        if latest_cache.get("meta", {}).get("job_id") != job_id:
            return
        BULK_JOB["job_id"] = job_id
        BULK_JOB["models"] = models
        BULK_JOB["total"] = len(latest_cache.get("articles", []))
        BULK_JOB["completed"] = sum(1 for article in latest_cache.get("articles", []) if article.get("status") in {"completed", "error"})
        BULK_JOB["failed"] = sum(1 for article in latest_cache.get("articles", []) if article.get("status") == "error")
        BULK_JOB["running"] = queued_any or any(article.get("status") == "pending" for article in latest_cache.get("articles", []))
        if BULK_JOB["running"] and not BULK_JOB.get("started_at"):
            BULK_JOB["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if not BULK_JOB["running"]:
            BULK_JOB["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def resume_bulk_job_from_cache() -> None:
    with BULK_LOCK:
        cache = load_bulk_cache()
        articles = cache.get("articles", [])
        meta = cache.get("meta", {})
        if not articles or not any(article.get("status") == "pending" for article in articles):
            return
        BULK_JOB.update(
            {
                "job_id": meta.get("job_id") or uuid.uuid4().hex[:12],
                "running": True,
                "total": len(articles),
                "completed": sum(1 for article in articles if article.get("status") in {"completed", "error"}),
                "failed": sum(1 for article in articles if article.get("status") == "error"),
                "started_at": meta.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": None,
                "models": [model for model in meta.get("models", []) if model in MODEL_CANDIDATES],
            }
        )
        cache.setdefault("meta", {})["job_id"] = BULK_JOB["job_id"]
        save_bulk_cache(cache)
    queue_bulk_articles(cache)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "CapLimpede/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._send_file(INDEX_HTML, "text/html; charset=utf-8")
            return
        if path == "/news-preview.css":
            self._send_file(NEWS_PREVIEW_CSS, "text/css; charset=utf-8")
            return
        if path == "/api/bulk-cache":
            self._send_json(load_bulk_cache())
            return
        if path == "/api/bulk-status":
            self._send_json(get_bulk_status_payload())
            return
        if path == "/api/config":
            self._send_json(
                {
                    "prompt_default": PROMPT_DEFAULT,
                    "prompt_level_1_only": PROMPT_LEVEL_1_ONLY,
                    "models": MODEL_CANDIDATES,
                    "has_openai_key": bool(OPENAI_API_KEY),
                }
            )
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/review", "/api/vote", "/api/bulk-process", "/api/bulk-article-state"}:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8") if length else "{}"

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_json({"error": "Body-ul trebuie să fie JSON valid."}, status=HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/bulk-process":
            prompt = str(payload.get("prompt", "")).strip() or PROMPT_DEFAULT
            levels = normalize_levels(payload.get("levels"))
            models_payload = payload.get("models")
            selected_models = [model for model in models_payload if model in MODEL_CANDIDATES] if isinstance(models_payload, list) else MODEL_CANDIDATES
            if not selected_models:
                self._send_json({"error": "Selectează cel puțin un model."}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                urls = read_todo_urls()
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            with BULK_LOCK:
                if BULK_JOB.get("running"):
                    self._send_json({"error": "Există deja un bulk process în desfășurare."}, status=HTTPStatus.CONFLICT)
                    return
                cache = build_bulk_cache(urls, prompt, selected_models, levels)
                BULK_JOB.update(
                    {
                        "job_id": cache["meta"]["job_id"],
                        "running": True,
                        "total": len(urls),
                        "completed": 0,
                        "failed": 0,
                        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "finished_at": None,
                        "models": selected_models,
                    }
                )
                save_bulk_cache(cache)
            queue_bulk_articles(cache)
            self._send_json(get_bulk_status_payload())
            return

        if path == "/api/review":
            source = str(payload.get("source", "")).strip()
            prompt = str(payload.get("prompt", "")).strip() or PROMPT_DEFAULT
            levels = normalize_levels(payload.get("levels"))
            models_payload = payload.get("models")
            selected_models = models_payload if isinstance(models_payload, list) else MODEL_CANDIDATES
            if not source:
                self._send_json({"error": "Câmpul `source` este obligatoriu."}, status=HTTPStatus.BAD_REQUEST)
                return

            document = parse_source(source)
            try:
                result = call_openai_review(document, prompt, selected_models, levels)
                if not ((result.get("meta") or {}).get("cache") or {}).get("hit"):
                    append_runs_to_google_sheet(document, result)
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)
                return
            except Exception as exc:
                self._send_json({"error": f"Analiza a mers, dar logarea în Google Sheets a eșuat: {exc}"}, status=HTTPStatus.BAD_GATEWAY)
                return

            self._send_json(result)
            return

        if path == "/api/bulk-article-state":
            index = payload.get("index")
            if not isinstance(index, int):
                self._send_json({"error": "Câmpul `index` trebuie să fie număr întreg."}, status=HTTPStatus.BAD_REQUEST)
                return
            run_states = payload.get("runStates")
            if not isinstance(run_states, dict):
                self._send_json({"error": "Câmpul `runStates` trebuie să fie obiect."}, status=HTTPStatus.BAD_REQUEST)
                return
            best_model = payload.get("best_model")
            try:
                result = save_bulk_article_operator_state(index, run_states, str(best_model).strip() if best_model else None)
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(result)
            return

        run_id = str(payload.get("run_id", "")).strip()
        best_model = str(payload.get("best_model", "")).strip()
        if not run_id or not best_model:
            self._send_json({"error": "Câmpurile `run_id` și `best_model` sunt obligatorii."}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            result = update_vote_in_google_sheet(run_id, best_model)
        except RuntimeError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)
            return
        self._send_json(result)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_file(self, path: Path, content_type: str) -> None:
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> None:
    validate_google_logging_configuration()
    ensure_bulk_workers_started()
    resume_bulk_job_from_cache()
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"Cap Limpede rulează la http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
