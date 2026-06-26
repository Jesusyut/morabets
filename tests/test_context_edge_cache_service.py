import unittest

from context_edge_cache_service import (
    build_context_edge_cache_parts,
    lookup_context_edge_cache,
)


class ContextEdgeCacheServiceTest(unittest.TestCase):
    def setUp(self):
        self.board = [
            {
                "sport": "MLB",
                "player": "Example Player",
                "stat": "hits",
                "line": 1.5,
                "no_vig_prob": 61.2,
                "best_over_price": 120,
            }
        ]

    def test_cache_key_is_deterministic_for_equivalent_prompt_whitespace(self):
        first = build_context_edge_cache_parts(
            self.board,
            "  Best   value today? ",
        )
        second = build_context_edge_cache_parts(
            self.board,
            "best value TODAY?",
        )

        self.assertEqual(first["cache_key"], second["cache_key"])
        self.assertEqual(first["prompt_hash"], second["prompt_hash"])
        self.assertEqual(first["board_hash"], second["board_hash"])

    def test_lookup_returns_cached_response_when_supabase_has_row(self):
        expected_response = "Cached Context Edge answer"
        calls = []

        def fake_reader(cache_key):
            calls.append(cache_key)
            return {
                "cache_key": cache_key,
                "response": expected_response,
            }

        result = lookup_context_edge_cache(
            self.board,
            "Best value today?",
            cache_reader=fake_reader,
        )

        self.assertTrue(result["hit"])
        self.assertEqual(result["response"], expected_response)
        self.assertEqual(result["cache_row"]["response"], expected_response)
        self.assertEqual(calls, [result["cache_key"]])

    def test_lookup_returns_cache_miss_when_supabase_has_no_row(self):
        result = lookup_context_edge_cache(
            self.board,
            "Best value today?",
            cache_reader=lambda cache_key: None,
        )

        self.assertFalse(result["hit"])
        self.assertIsNone(result["response"])
        self.assertIsNone(result["cache_row"])
        self.assertTrue(result["cache_key"])


if __name__ == "__main__":
    unittest.main()
