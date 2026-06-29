import unittest
from datetime import datetime, timezone

from context_edge_button_output_service import (
    build_board_hash,
    build_button_output_payload,
    get_output_config,
    get_output_configs,
    get_phoenix_run_window,
)


class ContextEdgeButtonOutputServiceTest(unittest.TestCase):
    def test_output_configs_include_exact_existing_button_keys(self):
        self.assertEqual(
            list(get_output_configs().keys()),
            [
                "mlb_value",
                "soccer_value",
                "plus_money",
                "nfl_value",
            ],
        )

    def test_configs_preserve_current_dashboard_prompt_intent(self):
        self.assertEqual(
            get_output_config("mlb_value")["prompt"],
            "best value bets today MLB — include moneylines, spreads, and player props",
        )
        self.assertEqual(
            get_output_config("nfl_value")["label"],
            "🏈 NFL value",
        )

    def test_board_hash_is_deterministic_for_key_order(self):
        first = [{"player": "A", "line": 1.5, "sport": "MLB"}]
        second = [{"sport": "MLB", "line": 1.5, "player": "A"}]

        self.assertEqual(build_board_hash(first), build_board_hash(second))

    def test_payload_contains_config_and_board_hash(self):
        board = [{"player": "A", "line": 1.5}]
        payload = build_button_output_payload("plus_money", board)

        self.assertEqual(payload["output_key"], "plus_money")
        self.assertEqual(payload["label"], "💰 Plus money")
        self.assertEqual(
            payload["prompt"],
            "best plus money plays today — any sport, odds better than +100",
        )
        self.assertEqual(payload["board_hash"], build_board_hash(board))
        self.assertEqual(payload["board"], board)

    def test_phoenix_run_window_morning_before_three_pm(self):
        self.assertEqual(
            get_phoenix_run_window(datetime(2026, 6, 26, 14, 59)),
            "morning",
        )

    def test_phoenix_run_window_afternoon_at_three_pm(self):
        self.assertEqual(
            get_phoenix_run_window(datetime(2026, 6, 26, 15, 0)),
            "afternoon",
        )

    def test_phoenix_run_window_converts_aware_datetime(self):
        self.assertEqual(
            get_phoenix_run_window(
                datetime(2026, 6, 26, 22, 0, tzinfo=timezone.utc)
            ),
            "afternoon",
        )


if __name__ == "__main__":
    unittest.main()
