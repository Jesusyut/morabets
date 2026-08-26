import unittest

from best_opportunities_service import build_best_opportunities_report


class BestOpportunitiesServiceTest(unittest.TestCase):
    def test_filters_to_draftkings_fanduel_and_actionable_odds(self):
        payload = {
            "edge_picks": [
                {
                    "player": "Team A",
                    "stat_label": "Moneyline Win",
                    "no_vig_prob": 61.5,
                    "ev_pct": 8.2,
                    "best_book": "DraftKings",
                    "best_over_price": -210,
                    "all_books": [{"book": "DraftKings", "over_price": -210}],
                },
                {
                    "player": "Team B",
                    "stat_label": "Moneyline Win",
                    "no_vig_prob": 68.0,
                    "ev_pct": 12.0,
                    "best_book": "BetMGM",
                    "best_over_price": -110,
                    "all_books": [{"book": "BetMGM", "over_price": -110}],
                },
                {
                    "player": "Team C",
                    "stat_label": "Moneyline Win",
                    "no_vig_prob": 70.0,
                    "ev_pct": 14.0,
                    "best_book": "FanDuel",
                    "best_over_price": -245,
                    "all_books": [{"book": "FanDuel", "over_price": -245}],
                },
            ]
        }

        report = build_best_opportunities_report(line_payloads={"MLB": payload})

        self.assertEqual(report["count"], 1)
        self.assertEqual(report["opportunities"][0]["title"], "Team A")
        self.assertEqual(report["opportunities"][0]["book"], "DraftKings")

    def test_ranks_by_blended_score_with_context_and_payout(self):
        props = [
            {
                "player": "Player A",
                "stat_label": "Over 1.5 Total Bases",
                "no_vig_prob": 58.0,
                "ev_pct": 4.0,
                "best_book": "FanDuel",
                "best_over_price": +105,
                "matchup": "High Scoring Game",
                "all_books": [{"book": "FanDuel", "over_price": +105}],
            },
            {
                "player": "Player B",
                "stat_label": "Over 0.5 Hits",
                "no_vig_prob": 64.0,
                "ev_pct": 1.0,
                "best_book": "DraftKings",
                "best_over_price": -180,
                "all_books": [{"book": "DraftKings", "over_price": -180}],
            },
        ]

        report = build_best_opportunities_report(prop_payloads={"MLB": props})

        self.assertEqual(report["count"], 2)
        self.assertEqual(report["opportunities"][0]["title"], "Player A")
        self.assertEqual(report["opportunities"][1]["title"], "Player B")


if __name__ == "__main__":
    unittest.main()
