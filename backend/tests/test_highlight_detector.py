import os
import unittest
from unittest.mock import patch

from app.highlight_detector import _heuristic_score, detect_highlights


class HighlightDetectorTests(unittest.TestCase):
    def test_surprising_reaction_scores_higher_than_filler(self):
        exciting_score, _ = _heuristic_score(
            "No way! I can't believe that happened. That was absolutely crazy!"
        )
        filler_score, _ = _heuristic_score(
            "You know, I mean, kind of, sort of, just talking about the same thing."
        )
        self.assertGreater(exciting_score, filler_score)

    def test_score_is_always_between_zero_and_one_hundred(self):
        score, _ = _heuristic_score(
            "No way! What just happened? Let's go! This is amazing and impossible!"
        )
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)

    def test_empty_transcript_returns_no_candidates(self):
        candidates, method = detect_highlights([], 10, 30)
        self.assertEqual(candidates, [])
        self.assertEqual(method, "local_heuristic")

    def test_invalid_duration_returns_no_candidates(self):
        segments = [{"start": 0, "end": 20, "text": "A short test segment."}]
        candidates, method = detect_highlights(segments, 30, 20)
        self.assertEqual(candidates, [])
        self.assertEqual(method, "local_heuristic")

    def test_candidate_contains_timestamps_and_rank(self):
        segments = [
            {"start": 0, "end": 18, "text": "No way! I can't believe that happened."},
            {"start": 18, "end": 36, "text": "That was crazy. Let's go!"},
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            candidates, method = detect_highlights(segments, 30, 45)

        self.assertEqual(method, "local_heuristic")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["start"], 0)
        self.assertEqual(candidates[0]["end"], 36)
        self.assertEqual(candidates[0]["rank"], 1)
        self.assertEqual(candidates[0]["detection_method"], "local_heuristic")
        self.assertGreaterEqual(candidates[0]["score"], 0)

    def test_malformed_segment_does_not_stop_detection(self):
        segments = [
            {"start": "bad", "end": 10, "text": "Broken segment"},
            {"start": 10, "end": 28, "text": "Here is the thing. This is surprising."},
            {"start": 28, "end": 46, "text": "No way! That was close. Let's go!"},
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            candidates, method = detect_highlights(segments, 30, 45)

        self.assertEqual(method, "local_heuristic")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["start"], 10)
        self.assertEqual(candidates[0]["end"], 46)


if __name__ == "__main__":
    unittest.main()
