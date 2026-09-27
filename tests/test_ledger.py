"""跨市场调拨账业务不变量测试。"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from festival_supply.ledger import Ledger, LedgerError, parse_ts  # noqa: E402

CST = timezone(timedelta(hours=8))
HOLIDAY_END = datetime(2026, 10, 8, 0, 0, tzinfo=CST)
START = datetime(2026, 9, 25, 8, 0, tzinfo=CST)

DIRECTORY = {
    "jx-dispatch": {"market_ref": "jx", "roles": ["dispatch"]},
    "jx-inspect": {"market_ref": "jx", "roles": ["inspector"]},
    "hz-dispatch": {"market_ref": "hz", "roles": ["dispatch"]},
    "sh-dispatch": {"market_ref": "sh", "roles": ["dispatch"]},
    "sh-price": {"market_ref": "sh", "roles": ["price_monitor", "approver"]},
    "office-audit": {"market_ref": "office", "roles": ["approver"]},
}


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = Ledger(directory=DIRECTORY, now=START)

    def declare_passed_lot(self, lot_id: str = "lot-1", quantity: float = 1000,
                           usable_until: datetime | None = None,
                           market: str = "jx") -> None:
        actor = {"jx": "jx-dispatch", "hz": "hz-dispatch"}[market]
        inspector = {"jx": "jx-inspect"}.get(market)
        self.ledger.declare_lot(
            lot_id=lot_id, actor_ref=actor, category="蔬菜", spec="大白菜 一等",
            origin_market_ref=market, owner_ref="coop-1", quantity=quantity,
            quality_grade="A",
            usable_until=usable_until or ts("2026-10-05T10:00:00+08:00"))
        if inspector:
            self.ledger.record_inspection(lot_ref=lot_id, actor_ref=inspector,
                                          status="passed")

    def assert_raises_code(self, code: str, fn, *args, **kwargs) -> LedgerError:
        with self.assertRaises(LedgerError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    # --------------------------------------------- “已组织货源”不等于可售

    def test_declared_but_uninspected_is_not_sellable(self) -> None:
        self.ledger.declare_lot(
            lot_id="lot-1", actor_ref="jx-dispatch", category="蔬菜", spec="大白菜 一等",
            origin_market_ref="jx", owner_ref="coop-1", quantity=1000, quality_grade="A",
            usable_until=ts("2026-10-05T10:00:00+08:00"))
        avail = self.ledger.availability("jx", "蔬菜", "大白菜 一等")
        self.assertEqual(1000, avail["declared_at_source"])
        self.assertEqual(0, avail["on_sale"])

    def test_detained_lot_is_not_sellable_nor_committable(self) -> None:
        self.declare_passed_lot()
        self.ledger.record_inspection(lot_ref="lot-1", actor_ref="jx-inspect",
                                      status="detained")
        avail = self.ledger.availability("jx", "蔬菜", "大白菜 一等")
        self.assertEqual(0, avail["on_sale"])
        self.assertEqual(1000, avail["detained"])
        self.assert_raises_code(
            "lot_detained", self.ledger.reserve_quantity,
            commitment_id="c-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            market_ref="jx", quantity=10)
        # 暂扣解除后恢复可售
        self.ledger.record_inspection(lot_ref="lot-1", actor_ref="jx-inspect",
                                      status="released")
        avail = self.ledger.availability("jx", "蔬菜", "大白菜 一等")
        self.assertEqual(1000, avail["on_sale"])

    def test_inbound_in_transit_is_not_sellable_until_receipt(self) -> None:
        self.declare_passed_lot()
        self.ledger.create_emergency_transfer(
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-1",
            quantity=200, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1")
        avail = self.ledger.availability("sh", "蔬菜", "大白菜 一等")
        self.assertEqual(0, avail["on_sale"])
        self.assertEqual(200, avail["in_transit_inbound"])
        self.ledger.settle_transfer(
            transfer_id="t-1", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1", receipt_no="rc-1", received_quantity=200,
            received_quality_grade="A", loss_reason="")
        avail = self.ledger.availability("sh", "蔬菜", "大白菜 一等")
        self.assertEqual(200, avail["on_sale"])
        self.assertEqual(0, avail["in_transit_inbound"])

    def test_same_lot_cannot_be_promised_to_two_markets(self) -> None:
        self.declare_passed_lot(quantity=100)
        self.ledger.reserve_quantity(
            commitment_id="c-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            market_ref="jx", quantity=80)
        self.assert_raises_code(
            "insufficient_quantity", self.ledger.reserve_quantity,
            commitment_id="c-2", lot_ref="lot-1", actor_ref="jx-dispatch",
            market_ref="sh", quantity=30)

    # ----------------------------------------------------- 处置权与签收权

    def test_only_source_market_may_declare_and_dispose(self) -> None:
        self.assert_raises_code(
            "actor_market_mismatch", self.ledger.declare_lot,
            lot_id="lot-x", actor_ref="sh-dispatch", category="蔬菜", spec="x",
            origin_market_ref="jx", owner_ref="coop-1", quantity=10, quality_grade="A",
            usable_until=ts("2026-10-05T10:00:00+08:00"))
        self.declare_passed_lot()
        self.assert_raises_code(
            "source_disposal_required", self.ledger.create_emergency_transfer,
            transfer_id="t-x", lot_ref="lot-1", actor_ref="hz-dispatch",
            from_market_ref="hz", to_market_ref="sh", waybill_no="wb-x",
            quantity=10, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-x")

    def test_emergency_transfer_requires_source_authorization(self) -> None:
        self.declare_passed_lot()
        self.assert_raises_code(
            "source_authorization_required", self.ledger.create_emergency_transfer,
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-1",
            quantity=10, quality_grade="A", owner_ref="coop-1",
            source_authorization=" ")

    def test_only_receiver_confirms_arrival(self) -> None:
        self.declare_passed_lot()
        self.ledger.create_emergency_transfer(
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-1",
            quantity=100, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1")
        self.assert_raises_code(
            "receiver_must_confirm", self.ledger.settle_transfer,
            transfer_id="t-1", actor_ref="jx-dispatch", receiver_ref="jx-dispatch",
            waybill_no="wb-1", receipt_no="rc-x", received_quantity=100,
            received_quality_grade="A", loss_reason="")

    # ------------------------------------------------- 应急调拨拆分与守恒

    def _split_transfer(self) -> None:
        self.declare_passed_lot(quantity=1000)
        self.ledger.reserve_quantity(
            commitment_id="c-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            market_ref="jx", quantity=600)
        self.ledger.create_emergency_transfer(
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-parent",
            quantity=700, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1",
            splits=[
                {"transfer_id": "t-1a", "quantity": 400, "to_market_ref": "sh",
                 "waybill_no": "wb-1a"},
                {"transfer_id": "t-1b", "quantity": 300, "to_market_ref": "sh",
                 "waybill_no": "wb-1b"},
            ],
            sacrifices=[{"commitment_id": "c-1", "quantity": 300}])

    def test_split_totals_must_conserve(self) -> None:
        self.declare_passed_lot()
        self.assert_raises_code(
            "split_quantity_mismatch", self.ledger.create_emergency_transfer,
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-parent",
            quantity=100, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1",
            splits=[
                {"transfer_id": "t-1a", "quantity": 60, "to_market_ref": "sh",
                 "waybill_no": "wb-1a"},
                {"transfer_id": "t-1b", "quantity": 30, "to_market_ref": "sh",
                 "waybill_no": "wb-1b"},
            ])

    def test_split_waybills_must_be_distinct(self) -> None:
        self.declare_passed_lot()
        self.assert_raises_code(
            "split_waybill_duplicate", self.ledger.create_emergency_transfer,
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-parent",
            quantity=100, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1",
            splits=[
                {"transfer_id": "t-1a", "quantity": 50, "to_market_ref": "sh",
                 "waybill_no": "wb"},
                {"transfer_id": "t-1b", "quantity": 50, "to_market_ref": "sh",
                 "waybill_no": "wb"},
            ])

    def test_transfer_with_sacrifice_conserves_and_partially_releases_commitment(self) -> None:
        self._split_transfer()
        lot = self.ledger._lots["lot-1"]
        # 申报 1000 = 在库 0 + 仍锁定 300 + 已发运 700 + 释放 0
        self.assertAlmostEqual(1000, lot["free"] + lot["reserved"]
                               + lot["shipped"] + lot["released"])
        self.assertEqual(300, self.ledger._commitments["c-1"]["quantity"])
        self.assertEqual("active", self.ledger._commitments["c-1"]["status"])
        explanation = self.ledger.explain_transfer("t-1")
        self.assertEqual(700, explanation["quantity"])
        self.assertEqual([{"commitment_id": "c-1", "quantity": 300,
                           "market_ref": "jx"}],
                         explanation["sacrificed_commitments"])
        # 子批次继承品质等级与责任归属
        for child in explanation["splits"]:
            self.assertEqual("A", child["quality_grade"])
            self.assertEqual("coop-1", child["owner_ref"])

    def test_cannot_silently_take_locked_quantity(self) -> None:
        self.declare_passed_lot(quantity=100)
        self.ledger.reserve_quantity(
            commitment_id="c-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            market_ref="jx", quantity=90)
        # 可分配仅 10；想调 50 又不点名牺牲承诺 -> 拒绝
        self.assert_raises_code(
            "insufficient_quantity", self.ledger.create_emergency_transfer,
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-1",
            quantity=50, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1")

    def test_cannot_sacrifice_more_than_locked(self) -> None:
        self.declare_passed_lot(quantity=100)
        self.ledger.reserve_quantity(
            commitment_id="c-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            market_ref="jx", quantity=40)
        self.assert_raises_code(
            "sacrifice_exceeds_commitment", self.ledger.create_emergency_transfer,
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-1",
            quantity=80, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1",
            sacrifices=[{"commitment_id": "c-1", "quantity": 50}])

    def test_settlement_conservation_and_loss_reason(self) -> None:
        self._split_transfer()
        self.assert_raises_code(
            "settle_split_required", self.ledger.settle_transfer,
            transfer_id="t-1", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-parent", receipt_no="rc-x", received_quantity=700,
            received_quality_grade="A", loss_reason="")
        self.assert_raises_code(
            "loss_reason_required", self.ledger.settle_transfer,
            transfer_id="t-1a", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1a", receipt_no="rc-1", received_quantity=380,
            received_quality_grade="A", loss_reason="")
        self.ledger.settle_transfer(
            transfer_id="t-1a", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1a", receipt_no="rc-1", received_quantity=380,
            received_quality_grade="A", loss_reason="挤压损耗20")
        self.assert_raises_code(
            "settlement_out_of_range", self.ledger.settle_transfer,
            transfer_id="t-1b", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1b", receipt_no="rc-2", received_quantity=301,
            received_quality_grade="A", loss_reason="")
        self.ledger.settle_transfer(
            transfer_id="t-1b", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1b", receipt_no="rc-2", received_quantity=300,
            received_quality_grade="A", loss_reason="")
        explanation = self.ledger.explain_transfer("t-1")
        self.assertEqual("settled", explanation["status"])
        self.assertEqual(680, explanation["received_quantity"])
        self.assertEqual(20, explanation["loss_quantity"])

    def test_grade_mismatch_at_receipt_is_rejected(self) -> None:
        self.declare_passed_lot()
        self.ledger.create_emergency_transfer(
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-1",
            quantity=100, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1")
        self.assert_raises_code(
            "grade_mismatch_at_receipt", self.ledger.settle_transfer,
            transfer_id="t-1", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1", receipt_no="rc-1", received_quantity=100,
            received_quality_grade="B", loss_reason="")

    # --------------------------------------------------------- 回执/事件幂等

    def test_duplicate_event_id_does_not_change_stock(self) -> None:
        self.declare_passed_lot()
        self.ledger.create_emergency_transfer(
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-1",
            quantity=100, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1")
        shipped_before = self.ledger._lots["lot-1"]["shipped"]
        last_event = self.ledger.events[-1]
        self.ledger.record(last_event)  # 重传
        self.ledger.record(last_event)
        self.assertEqual(shipped_before, self.ledger._lots["lot-1"]["shipped"])
        settle = self.ledger.settle_transfer(
            transfer_id="t-1", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1", receipt_no="rc-1", received_quantity=100,
            received_quality_grade="A", loss_reason="")
        before = self.ledger.availability("sh", "蔬菜", "大白菜 一等")["on_sale"]
        self.ledger.record(settle)
        self.ledger.record(settle)
        after = self.ledger.availability("sh", "蔬菜", "大白菜 一等")["on_sale"]
        self.assertEqual(before, after)

    def test_same_receipt_number_cannot_settle_twice(self) -> None:
        self.declare_passed_lot(quantity=200)
        for transfer_id, waybill, qty in (("t-1", "wb-1", 100), ("t-2", "wb-2", 100)):
            self.ledger.create_emergency_transfer(
                transfer_id=transfer_id, lot_ref="lot-1", actor_ref="jx-dispatch",
                from_market_ref="jx", to_market_ref="sh", waybill_no=waybill,
                quantity=qty, quality_grade="A", owner_ref="coop-1",
                source_authorization=f"auth-{transfer_id}")
        self.ledger.settle_transfer(
            transfer_id="t-1", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1", receipt_no="rc-DUP", received_quantity=100,
            received_quality_grade="A", loss_reason="")
        self.assert_raises_code(
            "duplicate_receipt", self.ledger.settle_transfer,
            transfer_id="t-2", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-2", receipt_no="rc-DUP", received_quantity=100,
            received_quality_grade="A", loss_reason="")

    # ------------------------------------------------------- 运单冻结核对

    def test_waybill_quantity_or_destination_conflict_freezes(self) -> None:
        self.declare_passed_lot()
        self.ledger.create_emergency_transfer(
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-1",
            quantity=100, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1")
        self.assert_raises_code(
            "waybill_frozen", self.ledger.update_transit,
            transfer_id="t-1", actor_ref="sh-dispatch", waybill_no="wb-1",
            location_ref="杭州枢纽", observed_at=ts("2026-09-26T02:00:00+08:00"),
            quantity=90, to_market_ref="sh")
        # 冻结事件已留痕，冻结期间在途更新与签收都被拒绝
        self.assertEqual("WAYBILL_FROZEN", self.ledger.events[-1]["event_type"])
        self.assert_raises_code(
            "waybill_frozen", self.ledger.update_transit,
            transfer_id="t-1", actor_ref="sh-dispatch", waybill_no="wb-1",
            location_ref="杭州枢纽", observed_at=ts("2026-09-26T03:00:00+08:00"),
            quantity=100, to_market_ref="sh")
        self.assert_raises_code(
            "waybill_frozen", self.ledger.settle_transfer,
            transfer_id="t-1", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1", receipt_no="rc-1", received_quantity=90,
            received_quality_grade="A", loss_reason="改道减量10")
        # 接收市场无权自行解冻
        self.assert_raises_code(
            "source_disposal_required", self.ledger.reconcile_waybill,
            transfer_id="t-1", actor_ref="sh-dispatch", waybill_no="wb-1",
            resolution="corrected", corrected_quantity=90,
            corrected_to_market_ref="sh")
        # 来源市场核对确认减量 90：账目同步修正（10 退回在库）
        self.ledger.reconcile_waybill(
            transfer_id="t-1", actor_ref="jx-dispatch", waybill_no="wb-1",
            resolution="corrected", corrected_quantity=90,
            corrected_to_market_ref="sh")
        lot = self.ledger._lots["lot-1"]
        self.assertEqual(90, self.ledger._transfers["t-1"]["quantity"])
        self.assertEqual(910, lot["free"])
        self.assertAlmostEqual(1000, lot["free"] + lot["shipped"]
                               + lot["reserved"] + lot["released"])
        # 解冻后按核对后的数量签收
        self.ledger.settle_transfer(
            transfer_id="t-1", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1", receipt_no="rc-1", received_quantity=90,
            received_quality_grade="A", loss_reason="")

    # ------------------------------------------------------------- 价格纪律

    def _one_receipt(self, receipt_no: str = "rc-1", qty: float = 100) -> None:
        self.declare_passed_lot(quantity=200)
        self.ledger.create_emergency_transfer(
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-1",
            quantity=qty, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1")
        self.ledger.settle_transfer(
            transfer_id="t-1", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1", receipt_no=receipt_no, received_quantity=qty,
            received_quality_grade="A", loss_reason="")

    def test_price_guidance_must_rest_on_received_facts(self) -> None:
        self.declare_passed_lot()
        # 在途没有回执 -> 不能作为指导价依据
        self.ledger.create_emergency_transfer(
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-1",
            quantity=100, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1")
        self.assert_raises_code(
            "receipt_not_found_for_price", self.ledger.issue_price_guidance,
            guidance_id="g-1", actor_ref="sh-price", market_ref="sh",
            category="蔬菜", spec="大白菜 一等", guidance_price=3.5,
            basis_receipt_ids=["rc-1"])
        # 到货签收后才能作为依据
        self.ledger.settle_transfer(
            transfer_id="t-1", actor_ref="sh-dispatch", receiver_ref="sh-dispatch",
            waybill_no="wb-1", receipt_no="rc-1", received_quantity=100,
            received_quality_grade="A", loss_reason="")
        self.ledger.issue_price_guidance(
            guidance_id="g-1", actor_ref="sh-price", market_ref="sh",
            category="蔬菜", spec="大白菜 一等", guidance_price=3.5,
            basis_receipt_ids=["rc-1"])
        explanation = self.ledger.explain_price("g-1")
        self.assertEqual(["rc-1"], [r["receipt_no"] for r in explanation["basis_receipts"]])
        # 没有回执依据不得发布指导价
        self.assert_raises_code(
            "basis_required", self.ledger.issue_price_guidance,
            guidance_id="g-2", actor_ref="sh-price", market_ref="sh",
            category="蔬菜", spec="大白菜 一等", guidance_price=3.5,
            basis_receipt_ids=[])

    def test_exemption_requester_cannot_approve_even_with_approver_role(self) -> None:
        self.ledger.request_price_exemption(
            request_id="e-1", actor_ref="sh-price", market_ref="sh",
            category="蔬菜", spec="大白菜 一等", requester_ref="sh-price",
            reason="台风绕行成本")
        # sh-price 同时具备 approver 角色，仍不得自批
        self.assert_raises_code(
            "self_approval_forbidden", self.ledger.approve_price_exemption,
            request_id="e-1", actor_ref="sh-price", approver_ref="sh-price")
        # 无审批角色的市场人员也不行
        self.assert_raises_code(
            "role_required", self.ledger.approve_price_exemption,
            request_id="e-1", actor_ref="sh-dispatch", approver_ref="sh-dispatch")
        self.ledger.approve_price_exemption(
            request_id="e-1", actor_ref="office-audit", approver_ref="office-audit")
        self.assertEqual("approved", self.ledger._exemptions["e-1"]["status"])
        # 重复审批被拒绝
        self.assert_raises_code(
            "exemption_already_decided", self.ledger.approve_price_exemption,
            request_id="e-1", actor_ref="office-audit", approver_ref="office-audit")

    # ------------------------------------------------------- 模拟时钟与释放

    def test_post_holiday_release_returns_locked_goods(self) -> None:
        self.declare_passed_lot(quantity=100, usable_until=ts("2026-12-01T00:00:00+08:00"))
        self.ledger.reserve_quantity(
            commitment_id="c-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            market_ref="jx", quantity=60, release_after=HOLIDAY_END)
        self.assertEqual(40, self.ledger.availability("jx")["on_sale_in_warehouse"])
        self.ledger.advance_to(ts("2026-10-08T09:00:00+08:00"))
        self.assertEqual(100, self.ledger.availability("jx")["on_sale_in_warehouse"])
        self.assertEqual("released_post_holiday",
                         self.ledger._commitments["c-1"]["status"])
        self.assertEqual("LOT_RELEASED", self.ledger.events[-1]["event_type"])

    def test_expiring_goods_are_released_but_in_transit_is_untouched(self) -> None:
        self.declare_passed_lot(quantity=100, usable_until=ts("2026-09-28T10:00:00+08:00"))
        self.ledger.reserve_quantity(
            commitment_id="c-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            market_ref="jx", quantity=30)
        self.ledger.create_emergency_transfer(
            transfer_id="t-1", lot_ref="lot-1", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="sh", waybill_no="wb-1",
            quantity=50, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-1",
            sacrifices=[{"commitment_id": "c-1", "quantity": 20}])
        # 在库 40 + 承诺余 10 = 50 临期释放；在途 50 不受影响
        self.ledger.advance_to(ts("2026-09-29T00:00:00+08:00"))
        avail = self.ledger.availability("jx")
        self.assertEqual(50, avail["released_expiring"])
        self.assertEqual(50, self.ledger.availability("sh")["in_transit_inbound"])
        lot = self.ledger._lots["lot-1"]
        self.assertAlmostEqual(100, lot["free"] + lot["reserved"]
                               + lot["shipped"] + lot["released"])

    def test_clock_cannot_rewind(self) -> None:
        self.ledger.advance_to(ts("2026-09-26T00:00:00+08:00"))
        self.assert_raises_code(
            "clock_rewind_not_allowed", self.ledger.advance_to,
            ts("2026-09-25T00:00:00+08:00"))

    # ------------------------------------------------------- 故障恢复/并发

    def test_replay_after_failure_neither_reships_nor_resettles(self) -> None:
        self._one_receipt()
        snapshot = self.ledger.availability("sh")
        event_ids = [e["event_id"] for e in self.ledger.events]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.json"
            self.ledger.save(path)
            recovered = Ledger.load(
                path, directory=DIRECTORY, now=START)
            self.assertEqual(event_ids, [e["event_id"] for e in recovered.events])
            self.assertEqual(snapshot, recovered.availability("sh"))
            # 故障窗口内“补发”的同一事件流再次进入，也不会重复
            for event in recovered.events:
                recovered.record(event)
            self.assertEqual(snapshot, recovered.availability("sh"))

    def test_version_conflict_is_rejected(self) -> None:
        self.declare_passed_lot()
        # supply_lot/lot-1 已到 v2（申报 v1 + 检验 v2），迟到的 v2 同版本事件必须冲突
        stale = {
            "event_id": "stale-1", "event_type": "INSPECTION_RECORDED",
            "aggregate_type": "supply_lot", "aggregate_id": "lot-1",
            "occurred_at": "2026-09-25T09:00:00+08:00", "version": 2,
            "payload": {"actor_ref": "jx-inspect", "lot_ref": "lot-1",
                        "status": "passed"},
        }
        self.assert_raises_code("event_conflict", self.ledger.record, stale)

    def test_contract_violation_is_rejected(self) -> None:
        bad = {
            "event_id": "bad-1", "event_type": "LOT_DECLARED",
            "aggregate_type": "supply_lot", "aggregate_id": "lot-bad",
            "occurred_at": "2026-09-25T10:00:00",  # 无时区
            "version": 1, "payload": {},
        }
        self.assert_raises_code("contract_violation", self.ledger.record, bad)

    # --------------------------------------------------------------- 解释口径

    def test_explain_market_shortage_and_surplus(self) -> None:
        self._one_receipt(qty=40)
        self.ledger.report_store_gap(
            gap_id="gap-1", actor_ref="sh-dispatch", market_ref="sh",
            store_ref="store-9", category="蔬菜", spec="大白菜 一等", quantity=100,
            observed_at=ts("2026-09-27T08:00:00+08:00"))
        answer = self.ledger.explain_market("sh", "蔬菜", "大白菜 一等")
        self.assertTrue(any("因此缺货" in r for r in answer["reasons"]))

        surplus = Ledger(directory=DIRECTORY, now=START)
        surplus.declare_lot(
            lot_id="lot-hz", actor_ref="hz-dispatch", category="水产", spec="梭子蟹",
            origin_market_ref="hz", owner_ref="coop-9", quantity=500, quality_grade="A",
            usable_until=ts("2026-09-28T10:00:00+08:00"))
        # 杭州市场没有检验员登记在目录中；用已到货回执验证余量口径
        self.declare_passed_lot(lot_id="lot-2", quantity=300)
        self.ledger.create_emergency_transfer(
            transfer_id="t-9", lot_ref="lot-2", actor_ref="jx-dispatch",
            from_market_ref="jx", to_market_ref="hz", waybill_no="wb-9",
            quantity=200, quality_grade="A", owner_ref="coop-1",
            source_authorization="auth-9")
        self.ledger.settle_transfer(
            transfer_id="t-9", actor_ref="hz-dispatch", receiver_ref="hz-dispatch",
            waybill_no="wb-9", receipt_no="rc-9", received_quantity=200,
            received_quality_grade="A", loss_reason="")
        answer = self.ledger.explain_market("hz", "蔬菜", "大白菜 一等")
        self.assertTrue(any("仍有余量" in r for r in answer["reasons"]))

    def test_snapshot_explains_state_at_a_point_in_time(self) -> None:
        self._one_receipt()
        before_arrival = self.ledger.snapshot_at(ts("2026-09-25T07:30:00+08:00"))
        self.assertEqual(0, before_arrival.availability("sh")["on_sale"])
        self.assertEqual(100, self.ledger.availability("sh")["on_sale"])


if __name__ == "__main__":
    unittest.main()
