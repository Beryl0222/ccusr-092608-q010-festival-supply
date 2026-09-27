import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from festival_supply.cli import main


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = str(ROOT / "data/scenario.jsonl")

    def test_replay_explain_market(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["replay", self.scenario, "--explain-market", "M-SOUTH"])
        self.assertEqual(0, code)
        result = json.loads(buf.getvalue())
        veg = result["categories"]["蔬菜/上海青/一级"]
        self.assertEqual("shortage", veg["status"])
        self.assertEqual(2500.0, veg["diverted_away"])
        self.assertTrue(any("改道" in r for r in veg["reasons"]))

    def test_replay_price_basis_and_transfer(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["replay", self.scenario, "--price-basis", "gp-veg-1001"])
        self.assertEqual(0, code)
        self.assertEqual(2420.0, json.loads(buf.getvalue())["arrived_qty"])

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["replay", self.scenario, "--explain-transfer", "to-emg-veg"])
        info = json.loads(buf.getvalue())
        self.assertEqual(0, code)
        self.assertEqual("M-SOUTH", info["diverted_from"])
        self.assertEqual(80.0, info["loss_qty"])

    def test_replay_as_of_filters_future(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main([
                "replay", self.scenario,
                "--as-of", "2026-09-30T07:30:00+08:00",
                "--explain-market", "M-NORTH",
            ])
        self.assertEqual(0, code)
        result = json.loads(buf.getvalue())
        # 青菜 07:00 已放行：全部可售；带鱼 06:30 已申报但 08:00 才暂扣，
        # 此时为待检（pending），同样不可售。
        self.assertEqual(5000.0, result["categories"]["蔬菜/上海青/一级"]["free"])
        self.assertEqual(800.0, result["categories"]["海鲜/带鱼/冰鲜"]["detained"])

        # 10:00 才发起的应急调拨在 07:30 尚不存在
        err = io.StringIO()
        import contextlib
        with redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main([
                "replay", self.scenario,
                "--as-of", "2026-09-30T07:30:00+08:00",
                "--explain-transfer", "to-emg-veg",
            ])
        self.assertEqual(1, code)
        self.assertIn("order_unknown", err.getvalue())

    def test_unknown_aggregate_is_nonzero(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["replay", self.scenario, "--explain-transfer", "NOPE"])
        self.assertEqual(1, code)


if __name__ == "__main__":
    unittest.main()
