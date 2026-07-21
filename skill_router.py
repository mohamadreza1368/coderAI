"""Three-stage local-first skill routing funnel."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from memory_manager import default_storage_root


PERSIAN_TRANSLATION = str.maketrans({
    "ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه", "ؤ": "و", "إ": "ا", "أ": "ا",
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4", "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
})


def normalize_persian(text: str) -> str:
    value = str(text or "").lower().translate(PERSIAN_TRANSLATION).replace("\u200c", " ").replace("\u200f", " ")
    return re.sub(r"\s+", " ", value).strip()


@dataclass
class RoutedSkill:
    skill: object
    triggered_by: str
    matched_keywords: list[str] = field(default_factory=list)
    semantic_score: float = 0.0
    stage: str = "fast_filter"

    @property
    def reason(self) -> str:
        if self.triggered_by == "slash_command":
            return f"explicit /{self.skill.name} command"
        if self.triggered_by == "pinned":
            return "pinned for this project"
        matched = ", ".join(f'"{item}"' for item in self.matched_keywords)
        if matched:
            return f"matched {matched} · semantic {self.semantic_score:.2f}"
        return f"semantic similarity {self.semantic_score:.2f}"


@dataclass
class RouteResult:
    selected: list[RoutedSkill]
    skipped: list[RoutedSkill]
    confirmation_used: bool = False
    semantic_available: bool = False


class SkillRouter:
    def __init__(self, storage_root: str | Path | None = None, embedding_model: str | None = None):
        self.storage_dir = Path(storage_root).resolve() if storage_root else default_storage_root()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.storage_dir / "skill_vectors.json"
        self.embedding_model = embedding_model or os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
        self.ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        self._cache = self._load_cache()

    def route(self, prompt: str, skills: list, modes: dict[str, str], explicit_names: list[str] | None = None,
              max_active: int = 3, smart_confirmation: bool = False, confirmer=None) -> RouteResult:
        explicit = set(explicit_names or [])
        disabled = {name for name, mode in modes.items() if mode == "off"}
        pinned = [skill for skill in skills if modes.get(skill.name) == "pinned" and skill.name not in disabled]
        automatic = [skill for skill in skills if modes.get(skill.name, "auto") == "auto" and skill.name not in disabled]
        selected: list[RoutedSkill] = [RoutedSkill(skill, "pinned", stage="manual") for skill in pinned]
        for skill in skills:
            if skill.name in explicit and skill.name not in disabled and all(item.skill.name != skill.name for item in selected):
                selected.append(RoutedSkill(skill, "slash_command", [f"/{skill.name}"], 1.0, "manual"))

        fast_hits = self.fast_filter(prompt, automatic)
        ranked, semantic_available = self.semantic_rank(prompt, automatic)
        semantic_by_name = {skill.name: score for skill, score in ranked}
        candidates: dict[str, RoutedSkill] = {}
        for skill, triggers in fast_hits:
            candidates[skill.name] = RoutedSkill(skill, "trigger_match", triggers, semantic_by_name.get(skill.name, 0.0), "fast_filter")
        for skill, score in ranked:
            if score >= 0.45 and skill.name not in candidates:
                candidates[skill.name] = RoutedSkill(skill, "semantic_match", [], score, "semantic_rank")

        ordered = sorted(candidates.values(), key=lambda item: (item.triggered_by != "trigger_match", -item.semantic_score, item.skill.name))
        confirmation_used = False
        if smart_confirmation and confirmer and self.needs_llm_confirmation(fast_hits, ranked):
            confirmation_used = True
            confirmed = set(confirmer(prompt, ordered[:6]))
            ordered = [item for item in ordered if item.skill.name in confirmed]
            for item in ordered:
                item.stage = "llm_confirmation"

        manual_skipped = selected[max_active:]
        selected = selected[:max_active]
        existing = {item.skill.name for item in selected}
        ordered = [item for item in ordered if item.skill.name not in existing]
        room = max(0, max_active - len(selected))
        selected.extend(ordered[:room])
        return RouteResult(selected[:max_active], manual_skipped + ordered[room:], confirmation_used, semantic_available)

    def fast_filter(self, prompt: str, skills: list) -> list[tuple[object, list[str]]]:
        normalized = normalize_persian(prompt)
        hits = []
        for skill in skills:
            triggers = list(getattr(skill, "triggers", []) or [])
            if not triggers:
                triggers = self._description_terms(f"{skill.name} {skill.description}")
            matched = [trigger for trigger in triggers if normalize_persian(trigger) in normalized]
            if matched:
                hits.append((skill, matched[:6]))
        return hits

    def semantic_rank(self, prompt: str, skills: list, top_k: int = 6) -> tuple[list[tuple[object, float]], bool]:
        if not skills:
            return [], False
        query = self._embed(prompt)
        available = bool(query)
        if not query:
            query = self._fallback_vector(prompt)
        ranked = []
        for skill in skills:
            text = f"{skill.name}. {skill.description}. {' '.join(getattr(skill, 'triggers', []) or [])}"
            vector = self._cached_skill_vector(skill.name, text, use_ollama=available)
            ranked.append((skill, self._cosine(query, vector)))
        ranked.sort(key=lambda item: (-item[1], item[0].name))
        return ranked[:top_k], available

    @staticmethod
    def needs_llm_confirmation(fast_hits, semantic_scores) -> bool:
        if len(fast_hits) == 1:
            return False
        if not semantic_scores or (len(fast_hits) == 0 and semantic_scores[0][1] < 0.5):
            return False
        return True

    def invalidate(self) -> None:
        self._cache = {}
        self._save_cache()

    def _embed(self, text: str) -> list[float] | None:
        payload = json.dumps({"model": self.embedding_model, "input": normalize_persian(text)}).encode("utf-8")
        request = urllib.request.Request(f"{self.ollama_url}/api/embed", data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                data = json.loads(response.read().decode("utf-8"))
            embeddings = data.get("embeddings") or []
            return [float(value) for value in embeddings[0]] if embeddings else None
        except Exception:
            return None

    def _cached_skill_vector(self, name: str, text: str, use_ollama: bool) -> list[float]:
        fingerprint = hashlib.sha256(f"{self.embedding_model}:{text}".encode("utf-8")).hexdigest()
        cached = self._cache.get(name)
        if cached and cached.get("fingerprint") == fingerprint and bool(cached.get("ollama")) == use_ollama:
            return cached["vector"]
        vector = self._embed(text) if use_ollama else None
        vector = vector or self._fallback_vector(text)
        self._cache[name] = {"fingerprint": fingerprint, "vector": vector, "ollama": bool(use_ollama)}
        self._save_cache()
        return vector

    @staticmethod
    def _fallback_vector(text: str, dimensions: int = 256) -> list[float]:
        vector = [0.0] * dimensions
        words = re.findall(r"[^\W_]{2,}", normalize_persian(text), flags=re.UNICODE)
        for word in words:
            digest = hashlib.blake2b(word.encode("utf-8"), digest_size=4).digest()
            vector[int.from_bytes(digest, "little") % dimensions] += 1.0
        return vector

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
        return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0

    @staticmethod
    def _description_terms(text: str) -> list[str]:
        return [word for word in re.findall(r"[^\W_]{3,}", normalize_persian(text), flags=re.UNICODE) if word not in {"skill", "using", "with", "from", "that", "this"}]

    def _load_cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cache(self) -> None:
        self.cache_path.write_text(json.dumps(self._cache), encoding="utf-8")
