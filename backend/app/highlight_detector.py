from __future__ import annotations

import json
import os
import re
from typing import Any

# These are transcript signals, not claims about what happened visually or in the audio.
HOOK_WORDS = {
    "amazing", "because", "best", "crazy", "discover", "finally", "imagine",
    "important", "impossible", "mistake", "never", "secret", "surprising",
    "truth", "why", "win", "wrong", "lesson", "tip", "result",
}
SURPRISE_PHRASES = (
    "no way", "what just happened", "i can't believe", "you won't believe",
    "never expected", "did not expect", "didn't expect", "that's insane",
    "that is insane", "are you serious", "oh my god", "wait a minute",
    "plot twist", "out of nowhere", "how is that possible",
)
REACTION_PHRASES = (
    "let's go", "lets go", "there we go", "come on", "shut up",
    "i'm done", "im done", "this is crazy", "that was close",
    "we got it", "we did it", "what a", "holy",
)
STORY_PHRASES = (
    "the problem is", "here's the thing", "the reason is", "turns out",
    "long story short", "at the end", "the moment", "what happened was",
)
FILLER_PHRASES = (
    "you know", "like i said", "as i said", "at the moment",
    "kind of", "sort of", "i mean",
)


def _count_phrases(text: str, phrases: tuple[str, ...]) -> list[str]:
    normalized = re.sub(r"\s+", " ", text.lower())
    return [phrase for phrase in phrases if phrase in normalized]


def _heuristic_score(text: str) -> tuple[int, str]:
    """Score transcript-only evidence for an engaging, self-contained clip."""
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    words = re.findall(r"\b[\w']+\b", normalized)
    if not words:
        return 0, "Not enough spoken content"

    score = 34
    reasons: list[str] = []

    hits = sorted(set(words) & HOOK_WORDS)
    if hits:
        score += min(16, 4 * len(hits))
        reasons.append("strong hook wording")

    surprises = _count_phrases(normalized, SURPRISE_PHRASES)
    reactions = _count_phrases(normalized, REACTION_PHRASES)
    story_beats = _count_phrases(normalized, STORY_PHRASES)
    fillers = _count_phrases(normalized, FILLER_PHRASES)

    if surprises:
        score += min(20, 10 * len(surprises))
        reasons.append("surprise or disbelief")
    if reactions:
        score += min(16, 8 * len(reactions))
        reasons.append("strong reaction wording")
    if story_beats:
        score += min(12, 6 * len(story_beats))
        reasons.append("clear story beat")

    if "?" in text:
        score += 5
        reasons.append("question or setup")
    if "!" in text:
        score += 5
        reasons.append("emphatic delivery")
    if re.search(r"\b(but|however|instead|actually|until|suddenly|then)\b", normalized):
        score += 7
        reasons.append("contrast or change")
    if re.search(r"\b(first|finally|step|tip|result|because)\b", normalized):
        score += 4
        reasons.append("payoff or takeaway")

    # Prefer enough context to understand the moment, without rewarding rambling.
    if 35 <= len(words) <= 125:
        score += 8
    elif len(words) < 15:
        score -= 12
    elif len(words) > 180:
        score -= 8

    if fillers:
        score -= min(8, 2 * len(fillers))
    if len(set(words)) <= max(3, len(words) // 8):
        score -= 8

    score = max(0, min(100, score))
    reason = ", ".join(reasons[:3]) if reasons else "Promising spoken-content section"
    if hits:
        reason += f"; keywords: {', '.join(hits[:3])}"
    return score, reason[:180]


def _llm_rank(candidates: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    # Optional only: local scoring works without an API key and has no per-request AI cost.
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or os.getenv("CLIPFORGE_USE_OPENAI_RANKER", "").lower() not in {"1", "true", "yes"}:
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
                        "Rank transcript excerpts for short-form video. Prioritize funny, "
                        "surprising, emotional, high-energy, or memorable story moments, "
                        "then useful insights. Prefer moments understandable without long "
                        "setup. Do not invent visual or audio events. Return JSON with a "
                        "'rankings' array; each item has index, score (0-100), and reason "
                        "(max 18 words). Include each input index exactly once."
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
        # A temporary API/model error should never block free local detection.
        return None


def detect_highlights(
    segments: list[dict[str, Any]], min_seconds: float, max_seconds: float
) -> tuple[list[dict[str, Any]], str]:
    """Build chronological transcript windows and rank them using local signals."""
    if not segments or min_seconds <= 0 or max_seconds < min_seconds:
        return [], "local_heuristic"

    windows: list[dict[str, Any]] = []
    i = 0
    while i < len(segments):
        try:
            start = float(segments[i]["start"])
            end = start
            text_parts: list[str] = []
            j = i
            while j < len(segments):
                segment_end = float(segments[j]["end"])
                if segment_end < start or segment_end - start > max_seconds:
                    break
                end = segment_end
                text_parts.append(str(segments[j].get("text", "")))
                j += 1
                if end - start >= min_seconds:
                    break

            if end - start >= min_seconds and j > i:
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
        except (KeyError, TypeError, ValueError):
            # Ignore malformed transcript segments instead of failing the whole job.
            i += 1

    llm_scores = _llm_rank(windows) if windows else None
    method = "openai_llm" if llm_scores is not None else "local_heuristic"
    if llm_scores is not None:
        for candidate, rating in zip(windows, llm_scores):
            candidate["score"] = rating["score"]
            candidate["reason"] = rating["reason"]

    # Highest scoring moments are shown first; timestamps remain unchanged.
    windows.sort(key=lambda item: item["score"], reverse=True)
    for rank, candidate in enumerate(windows, start=1):
        candidate["rank"] = rank
        candidate["detection_method"] = method
    return windows, method
