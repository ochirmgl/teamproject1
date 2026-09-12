"""Document-only retrieval and Gemini answer generation for the DMS chat.

Added by: Ochir

Why this file exists:
    The teammate's original app.py already contained the DMS interface and a
    placeholder chat page, but it did not read documents or call an AI model.
    This separate module keeps the new AI/RAG logic away from the teammate's
    original interface code and makes ownership and maintenance clear.

Main responsibilities:
    - Read text from selected PDF, DOCX and TXT documents.
    - Split and locally search document text (no web search).
    - Understand Mongolian, English and common Latin-written Mongolian queries.
    - Ask Gemini to answer only from the retrieved document passages.
    - Return source labels/excerpts for the Streamlit interface.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
from typing import Iterable, Sequence


NO_ANSWER_MESSAGE = "Оруулсан баримт бичгүүдээс энэ талаар мэдээлэл олдсонгүй."
NO_ANSWER_MESSAGE_EN = "I could not find information about this in the selected documents."
WORD_PATTERN = re.compile(r"[0-9A-Za-zА-Яа-яЁёӨөҮү]+", re.UNICODE)
CYRILLIC_PATTERN = re.compile(r"[А-Яа-яЁёӨөҮү]")
LATIN_PATTERN = re.compile(r"[A-Za-z]")
ROMANISED_MONGOLIAN_WORDS = {
    "baigaa", "baina", "barimt", "bichig", "end", "geree", "heden",
    "medeelel", "mongol", "tuhai", "uu", "ve", "yu", "yuu",
}
ENGLISH_FOLLOW_UP_STOPWORDS = {
    "a", "about", "again", "an", "and", "any", "are", "based", "can",
    "could", "did", "do", "does", "explain", "for", "how", "i", "info",
    "information", "is", "it", "me", "more", "of", "okay", "on", "so",
    "tell", "that", "the", "then", "there", "these", "this", "those", "to",
    "us", "was", "were", "what", "why", "would", "you",
}
ENGLISH_FOLLOW_UP_REFERENCE = re.compile(
    r"\b(that|this|it|its|they|their|those|them|the above|previous answer|earlier answer)\b",
    re.IGNORECASE,
)
FOLLOW_UP_PHRASES = (
    "based on that", "according to that", "what about", "how about",
    "okay then", "so then", "and then", "tell me more", "explain more",
    "тэр талаар", "тэр мэдээлэл", "тэрний", "түүнийг", "түүний", "тэдгээр",
    "энэ талаар", "энэ нь", "энэ гэрээ", "энэ журам", "үүн дээр", "үүнээс",
    "дээрх", "өмнөх хариулт", "тэгвэл", "тэгээд", "цааш нь", "дэлгэрүүл",
)
QUERY_STOPWORDS = {
    "авч", "асуулт", "байгаа", "байна", "баримт", "баримтад", "баримтын",
    "бичсэн", "бол", "болон", "гэсэн", "гэж", "дээр", "дотор", "зүйл",
    "ийн", "ийг", "нь", "талаар", "тухай", "хэд", "хэрхэн", "энэ", "юу",
    "ямар", "яагаад", "вэ", "бэ", "уу", "үү", "юм",
}


def _question_language(question: str) -> str:
    """Choose the answer language while recognising common Mongolian transliteration."""
    words = {word.lower() for word in WORD_PATTERN.findall(question)}
    if len(words.intersection(ROMANISED_MONGOLIAN_WORDS)) >= 2:
        return "Mongolian"

    latin_count = len(LATIN_PATTERN.findall(question))
    cyrillic_count = len(CYRILLIC_PATTERN.findall(question))
    return "English" if latin_count > cyrillic_count and latin_count >= 4 else "Mongolian"


def _needs_mongolian_search_rewrite(question: str) -> bool:
    """Return True when a Latin-script query needs cross-language retrieval."""
    latin_count = len(LATIN_PATTERN.findall(question))
    cyrillic_count = len(CYRILLIC_PATTERN.findall(question))
    return latin_count > cyrillic_count and latin_count >= 4


def _needs_conversation_context(question: str) -> bool:
    """Added by Ochir: detect follow-ups that depend on an earlier turn."""
    normalised = " ".join(question.lower().split())
    if ENGLISH_FOLLOW_UP_REFERENCE.search(normalised):
        return True
    if any(phrase in normalised for phrase in FOLLOW_UP_PHRASES):
        return True

    meaningful_words = [
        word.lower()
        for word in WORD_PATTERN.findall(question)
        if len(word) >= 3
        and word.lower() not in QUERY_STOPWORDS
        and word.lower() not in ENGLISH_FOLLOW_UP_STOPWORDS
    ]
    return len(meaningful_words) < 3


class RAGError(RuntimeError):
    """Raised when the document chat cannot complete safely."""


# Added by Ochir: Gemini 3.x can use part of the output allowance for internal
# reasoning. Minimal thinking leaves enough room for the visible document answer.
# The fallback also supports older google-genai package versions.
def _minimal_thinking_config(types):
    try:
        thinking_level_type = getattr(types, "ThinkingLevel", None)
        minimal_level = (
            getattr(thinking_level_type, "MINIMAL", "minimal")
            if thinking_level_type
            else "minimal"
        )
        return types.ThinkingConfig(thinking_level=minimal_level)
    except (TypeError, ValueError):
        return types.ThinkingConfig(thinking_budget=0)


# Added by Ochir: detect provider-side truncation instead of displaying a
# response that ends in the middle of a word or sentence.
def _finish_reason_name(response) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    finish_reason = getattr(candidates[0], "finish_reason", None)
    if finish_reason is None:
        return ""
    return str(getattr(finish_reason, "name", finish_reason)).upper()


# Added by Ochir: some provider responses have no usable finish reason. In that
# case a longer answer without final punctuation is treated as probably cut off.
def _looks_incomplete_answer(answer_text: str) -> bool:
    text = answer_text.rstrip()
    if len(text) < 240:
        return False
    return re.search(r"[.!?…\]\)\}\"'”’]$", text) is None


@dataclass(frozen=True)
class DocumentChunk:
    document_id: int
    title: str
    file_name: str
    page_label: str
    text: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class RetrievedSource:
    number: int
    document_id: int
    title: str
    file_name: str
    page_label: str
    score: float
    context_text: str
    excerpt: str
    file_path: str = ''


def resolve_document_path(base_dir: str | Path, stored_path: str) -> Path:
    """Resolve both Windows and POSIX relative paths from the SQLite database."""
    base = Path(base_dir).resolve()
    normalized = stored_path.replace("\\", os.sep).replace("/", os.sep)
    candidate = Path(normalized)

    if not candidate.is_absolute():
        candidate = base / candidate
    if candidate.exists():
        return candidate.resolve()

    fallback = base / "uploaded_files" / Path(normalized).name
    return fallback.resolve()


def _normalise_text(text: str) -> str:
    text = text.replace("\x00", " ")
    return re.sub(r"\s+", " ", text).strip()


def _split_text(text: str, max_chars: int = 1500, overlap: int = 220) -> list[str]:
    """Create small overlapping chunks without requiring a tokenizer package."""
    text = _normalise_text(text)
    if not text:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if boundary > start + max_chars // 2:
                end = boundary + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)

    return chunks


def _tokenise(text: str) -> tuple[str, ...]:
    """Word and character tokens work reasonably well with Mongolian suffixes."""
    words = [word.lower() for word in WORD_PATTERN.findall(text)]
    tokens = list(words)
    for word in words:
        if len(word) >= 4:
            tokens.extend(f"#{word[index:index + 3]}" for index in range(len(word) - 2))
    return tuple(tokens)


def _extract_pdf(path: Path) -> list[tuple[str, str]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RAGError("pypdf суулгагдаагүй байна. requirements.txt-ийг суулгана уу.") from exc

    try:
        reader = PdfReader(str(path))
        pages: list[tuple[str, str]] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = _normalise_text(page.extract_text() or "")
            if text:
                pages.append((f"{page_number}-р хуудас", text))
        return pages
    except Exception as exc:
        raise RAGError(f"PDF файлыг уншиж чадсангүй: {path.name}") from exc


def _extract_docx(path: Path) -> list[tuple[str, str]]:
    try:
        from docx import Document
    except ImportError as exc:
        raise RAGError("python-docx суулгагдаагүй байна. requirements.txt-ийг суулгана уу.") from exc

    try:
        document = Document(str(path))
        blocks: list[str] = []
        blocks.extend(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip())
        for table in document.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    blocks.append(row_text)
        text = _normalise_text("\n".join(blocks))
        return [("Word баримт", text)] if text else []
    except Exception as exc:
        raise RAGError(f"Word файлыг уншиж чадсангүй: {path.name}") from exc


def _extract_text_file(path: Path) -> list[tuple[str, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        text = _normalise_text(text)
        return [("Текст баримт", text)] if text else []
    except Exception as exc:
        raise RAGError(f"Текст файлыг уншиж чадсангүй: {path.name}") from exc


class DocumentRAG:
    """Added by Ochir: local document index plus grounded Gemini answering."""

    def __init__(self, records: Sequence[dict], base_dir: str | Path):
        self.base_dir = Path(base_dir).resolve()
        self.chunks: list[DocumentChunk] = []
        self.errors: list[str] = []
        self.document_ids: set[int] = set()
        self.document_sections: dict[int, list[tuple[str, str]]] = {}
        self.document_metadata: dict[int, dict[str, str]] = {}
        self.semantic_vectors = None

        for record in records:
            document_id = int(record["id"])
            title = str(record["title"])
            stored_path = str(record["file_path"])
            path = resolve_document_path(self.base_dir, stored_path)

            if not path.exists():
                self.errors.append(f"Файл олдсонгүй: {path.name}")
                continue

            try:
                from processing import extract, process_document
                database_path = self.base_dir / 'dms_system.db'
                if database_path.exists():
                    status, warning, sections = process_document(document_id, self.base_dir, path=database_path)
                else:
                    status, warning, sections = extract(path)
                if warning:
                    self.errors.append(f'{title}: {warning}')
                if status not in ('ready', 'partial'):
                    continue
            except Exception as exc:
                self.errors.append(str(exc))
                continue

            if not sections:
                self.errors.append(
                    f"Уншигдах текст олдсонгүй: {path.name}. Энэ файл зураг хэлбэрийн PDF бол OCR шаардлагатай."
                )
                continue

            self.document_ids.add(document_id)
            self.document_sections[document_id] = sections
            self.document_metadata[document_id] = {
                "title": title,
                "file_name": path.name,
                "file_type": str(record.get("file_type") or path.suffix.lower()),
                "file_path": stored_path,
            }
            for page_label, section_text in sections:
                for chunk_text in _split_text(section_text):
                    searchable_text = f"{title}\n{path.name}\n{chunk_text}"
                    self.chunks.append(
                        DocumentChunk(
                            document_id=document_id,
                            title=title,
                            file_name=path.name,
                            page_label=page_label,
                            text=chunk_text,
                            tokens=_tokenise(searchable_text),
                        )
                    )

        self._document_frequency = Counter()
        try:
            from semantic_search import encode
            self.semantic_vectors = encode([chunk.text for chunk in self.chunks]) if self.chunks else None
        except Exception:
            self.errors.append('Утгаар хайх загвар ачаалагдсангүй. Түлхүүр үгийн хайлт ашиглаж байна.')
        for chunk in self.chunks:
            self._document_frequency.update(set(chunk.tokens))
        self._average_length = (
            sum(len(chunk.tokens) for chunk in self.chunks) / len(self.chunks)
            if self.chunks
            else 0.0
        )

    @property
    def document_count(self) -> int:
        return len(self.document_ids)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def retrieve(
        self,
        question: str,
        selected_document_ids: Iterable[int] | None = None,
        limit: int = 3,
    ) -> list[RetrievedSource]:
        if not self.chunks:
            return []

        selected = set(int(value) for value in selected_document_ids) if selected_document_ids is not None else set(self.document_ids)
        if not selected:
            return []
        aliases = {'amralt':'амралт','leave':'амралт','vacation':'амралт',
                   'salary':'цалин','tsalin':'цалин','travel':'томилолт',
                   'tomilolt':'томилолт','land':'газар','gazar':'газар',
                   'employee':'ажилтан','contract':'гэрээ','geree':'гэрээ'}
        expansion = ' '.join(aliases[word.lower()] for word in WORD_PATTERN.findall(question) if word.lower() in aliases)
        query_tokens = Counter(_tokenise(question + ' ' + expansion))
        if not query_tokens:
            return []
        query_words = {
            aliases.get(token, token)
            for token in query_tokens
            if not token.startswith("#") and len(token) >= 4 and token not in QUERY_STOPWORDS and token not in ENGLISH_FOLLOW_UP_STOPWORDS
        }

        chunk_count = len(self.chunks)
        average_length = max(self._average_length, 1.0)
        k1 = 1.5
        b = 0.75
        scored: list[tuple[float, DocumentChunk]] = []

        for chunk in self.chunks:
            if selected and chunk.document_id not in selected:
                continue
            frequencies = Counter(chunk.tokens)
            chunk_words = {token for token in frequencies if not token.startswith("#")}
            # Mongolian inflections often share stems but not exact words.
            matched_words = sum(any(a == word or (min(len(a),len(word)) >= 5 and a[:5] == word[:5])
                                    for word in chunk_words) for a in query_words)
            # Conservative lexical relevance: one generic shared word is not
            # enough evidence for a question with several distinct concepts.
            if query_words and matched_words < math.ceil(len(query_words) / 2):
                continue
            score = 0.0
            length_normaliser = k1 * (1 - b + b * len(chunk.tokens) / average_length)

            for token, query_frequency in query_tokens.items():
                frequency = frequencies.get(token, 0)
                if not frequency:
                    continue
                document_frequency = self._document_frequency.get(token, 0)
                inverse_frequency = math.log(
                    1 + (chunk_count - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                score += (
                    inverse_frequency
                    * (frequency * (k1 + 1) / (frequency + length_normaliser))
                    * min(query_frequency, 2)
                )

            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        if self.semantic_vectors is not None:
            from semantic_search import encode
            query_vector = encode([question])[0]
            semantic = sorted([(float(vector @ query_vector),chunk) for vector,chunk in zip(self.semantic_vectors,self.chunks)
                               if chunk.document_id in selected],key=lambda pair:pair[0],reverse=True)
            semantic = [(score,chunk) for score,chunk in semantic if score >= 0.45][:20]
            fused = {}
            for ranking in (scored[:20],semantic):
                for rank,(_,chunk) in enumerate(ranking,1):
                    fused[chunk] = fused.get(chunk,0) + 1/(60+rank)
            scored = sorted([(score,chunk) for chunk,score in fused.items()],key=lambda pair:pair[0],reverse=True)
        # Rerank toward passages that cover more query terms, then deduplicate.
        scored = [(score * (1 + sum(w in chunk.text.lower() for w in query_words)/max(len(query_words),1)),chunk) for score,chunk in scored]
        scored.sort(key=lambda item:item[0],reverse=True)
        seen = set()
        scored = [(score,chunk) for score,chunk in scored if not (chunk.text in seen or seen.add(chunk.text))]
        if scored:
            relative_cutoff = scored[0][0] * 0.35
            top_items = [item for item in scored if item[0] >= relative_cutoff][:limit]
        else:
            top_items = []

        # A selected single document can still be summarised with a very general question.
        if not top_items and len(selected) == 1 and self._is_summary_request(question):
            fallback_chunks = [chunk for chunk in self.chunks if chunk.document_id in selected][:limit]
            top_items = [(0.01, chunk) for chunk in fallback_chunks]

        grouped: dict[tuple[int, str], dict] = {}
        for score, chunk in top_items:
            key = (chunk.document_id, chunk.page_label)
            if key not in grouped:
                grouped[key] = {"score": score, "chunk": chunk, "texts": [chunk.text]}
            elif chunk.text not in grouped[key]["texts"]:
                grouped[key]["texts"].append(chunk.text)

        sources = []
        for index, item in enumerate(grouped.values(), start=1):
            chunk = item["chunk"]
            context_text = " ".join(item["texts"]).strip()
            sources.append(
                RetrievedSource(
                    number=index,
                    document_id=chunk.document_id,
                    title=chunk.title,
                    file_name=chunk.file_name,
                    page_label=chunk.page_label,
                    score=item["score"],
                    context_text=context_text,
                    excerpt=context_text[:600].strip(),
                    file_path=self.document_metadata[chunk.document_id]['file_path'],
                )
            )
        return sources

    def _selected_ids(self, selected_document_ids: Iterable[int] | None) -> set[int]:
        return {int(value) for value in selected_document_ids} if selected_document_ids is not None else set(self.document_ids)

    def _is_summary_request(self, question: str) -> bool:
        normalised = " ".join(question.lower().split())
        patterns = (
            "summarize",
            "summary",
            "what is in",
            "what is it about",
            "what is this document",
            "юуны тухай",
            "товчло",
            "товч",
            "агуулга",
        )
        return any(pattern in normalised for pattern in patterns)

    def _mentioned_document_ids(
        self,
        question: str,
        selected_document_ids: Iterable[int] | None,
    ) -> list[int]:
        selected = self._selected_ids(selected_document_ids)
        question_words = {
            word.lower() for word in WORD_PATTERN.findall(question) if len(word) >= 3
        }
        candidates: list[tuple[float, int]] = []

        for document_id in selected:
            metadata = self.document_metadata.get(document_id)
            if not metadata:
                continue
            title_words = {
                word.lower()
                for word in WORD_PATTERN.findall(metadata["title"])
                if len(word) >= 3 and word.lower() not in QUERY_STOPWORDS
            }
            if not title_words:
                continue
            overlap = len(question_words.intersection(title_words)) / len(title_words)
            if overlap >= 0.4:
                candidates.append((overlap, document_id))

        if not candidates and len(selected) == 1:
            return list(selected)
        if not candidates:
            return []

        candidates.sort(reverse=True)
        best_score = candidates[0][0]
        return [document_id for score, document_id in candidates if score == best_score]

    def _summary_sources(self, document_ids: Sequence[int]) -> list[RetrievedSource]:
        sources: list[RetrievedSource] = []
        total_context_chars = 0
        max_total_chars = 10000

        for document_id in document_ids:
            metadata = self.document_metadata.get(document_id)
            if not metadata:
                continue
            for page_label, section_text in self.document_sections.get(document_id, []):
                if total_context_chars >= max_total_chars or len(sources) >= 12:
                    break
                remaining = max_total_chars - total_context_chars
                context_text = section_text[:remaining].strip()
                if not context_text:
                    continue
                sources.append(
                    RetrievedSource(
                        number=len(sources) + 1,
                        document_id=document_id,
                        title=metadata["title"],
                        file_name=metadata["file_name"],
                        page_label=page_label,
                        score=1.0,
                        context_text=context_text,
                        excerpt=context_text[:600],
                        file_path=metadata['file_path'],
                    )
                )
                total_context_chars += len(context_text)
        return sources

    def _document_inventory(self, selected_document_ids: Iterable[int] | None) -> str:
        selected = self._selected_ids(selected_document_ids)
        lines = []
        for index, document_id in enumerate(sorted(selected), start=1):
            metadata = self.document_metadata.get(document_id)
            if metadata:
                lines.append(
                    f"{index}. {metadata['title']} | файл: {metadata['file_name']} | төрөл: {metadata['file_type']}"
                )
        return "\n".join(lines) if lines else "Сонгосон баримт байхгүй."

    def _conversation_retrieval_question(
        self,
        question: str,
        conversation_history: Sequence[dict] | None,
    ) -> str:
        """Added by Ochir: attach earlier user topics to ambiguous follow-up queries."""
        if not _needs_conversation_context(question):
            return question

        previous_questions: list[str] = []
        for item in reversed(conversation_history or []):
            if item.get("role") == "user" and item.get("content"):
                previous_questions.append(str(item["content"]))
                if len(previous_questions) == 2:
                    break

        if not previous_questions:
            return question

        previous_questions.reverse()
        previous_topic = "\n".join(previous_questions)
        return (
            f"Previous user questions that establish the topic:\n{previous_topic}\n"
            f"Current follow-up question:\n{question}"
        )

    def answer(self, question, api_key="", model="gemini-2.5-flash",
               selected_document_ids=None, conversation_history=None, config=None,
               user_id=None, db_path=None):
        from ai_provider import AIConfig, AIError
        from grounded_answer import answer
        config = config or AIConfig(api_key=api_key, model=model)
        try:
            return answer(self, question, selected_document_ids, conversation_history,
                          config, user_id, db_path or self.base_dir / "dms_system.db")
        except AIError as exc:
            raise RAGError(str(exc)) from None
