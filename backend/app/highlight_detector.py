from __future__ import annotations

import json
import os
import re
from typing import Any

HOOK_WORDS = {
    "amazing", "because", "best", "crazy", "discover", "finally", "imagine",
    "important", "impossible", "mistake", "never", "secret", "surprising",
    "truth", "why", "win", "wrong", "lesson", "tip", "result",
}


def _heuristic_score(text: str) -> tuple[int, str]:
    words = re.findall(r"\b[\w']+\b", text.lower())
    if not words:
        return 0, "Not enough spoken content"
    score = 35
    hits = sorted(set(words) & HOOK_WORDS)
    if hits:
        score += min(25, 8 * len(hits))
    if "?" in text:
        score += 8
    if "!" in text:
        score += 5
    if re.search(r"\b(but|however|instead|actually|so)\b", text, re.I):
        score += 7
    if re.search(r"\b(first|then|finally|step|tip)\b", text, re.I):
        score += 5
    if 25 <= len(words) <= 110:
        score += 10
    elif len(words) < 12:
        score -= 10
    score = max(0, min(100, score))
    reason = "Strong hook or useful takeaway" if score >= 65 else "Promising transcript section"
    if hits:
        reason += f"; hook terms: {', '.join(hits[:4])}"
    return score, reason


def _llm_rank(candidates: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        compact = [
            {"index": i, "text": c["transcript_excerpt"][:1400]}
            for i, c in enumerate(candidates)
        ]
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Select short-form video highlight moments from transcript excerpts. "
                        "Prioritize a clear hook, useful insight, emotional or surprising "
                        "moment, strong story beat, and standalone context. Do not reward "
                        "sensational claims without substance. Return JSON with a 'rankings' "
                        "array; each item has index, score (0-100), and reason (max 18 words). "
                        "Include each input index exactly once."
                    ),
                },
                {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
            ],
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        rankings = payload.get("rankings", [])
        by_index = {
            int(item["index"]): {
                "score": max(0, min(100, int(item["score"]))),
                "reason": str(item["reason"])[:180],
            }
            for item in rankings
            if str(item.get("index", "")).isdigit()
            and 0 <= int(item["index"]) < len(candidates)
        }
        if len(by_index) != len(candidates):
            return None
        return [by_index[i] for i in range(len(candidates))]
    except Exception:
        # A temporary API/model error should not block the local fallback.
        return None


def detect_highlights(
    segments: list[dict[str, Any]], min_seconds: float, max_seconds: float
) -> tuple[list[dict[str, Any]], str]:
    """Build transcript windows, then rank them with an optional LLM or local fallback."""
    windows: list[dict[str, Any]] = []
    i = 0
    while i < len(segments):
        start = float(segments[i]["start"])
        end = start
        text_parts: list[str] = []
        j = i
        while j < len(segments) and float(segments[j]["end"]) - start <= max_seconds:
            end = float(segments[j]["end"])
            text_parts.append(str(segments[j]["text"]))
            j += 1
            if end - start >= min_seconds:
                break
        if end - start >= min_seconds:
            excerpt = " ".join(text_parts).strip()
            score, reason = _heuristic_score(excerpt)
            windows.append({
                "start": start,
                "end": end,
                "transcript_excerpt": excerpt,
                "score": score,
                "reason": reason,
            })
            i = j
        else:
            i += 1

    llm_scores = _llm_rank(windows) if windows else None
    method = "openai_llm" if llm_scores is not None else "local_heuristic"
    if llm_scores is not None:
        for candidate, rating in zip(windows, llm_scores):
            candidate["score"] = rating["score"]
            candidate["reason"] = rating["reason"]
    windows.sort(key=lambda item: item["score"], reverse=True)
    for rank, candidate in enumerate(windows, start=1):
        candidate["rank"] = rank
        candidate["detection_method"] = method
    return windows, method
