import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from festival_supply.contracts import validate_event
from festival_supply.ledger import DomainError, Ledger, event

NORTH = "M-NORTH"
SOUTH = "M-SOUTH"
EAST = "M-EAST"
CAT = "蔬菜/上海青/一级"
T0 = "2026-09-30T08:00:00+08:00"
FRESH = "2026-10-05T08:00:00+08:00"
RELEASE = "2026-10-08T08:00:00+08:00"


def lot_declared(lot="L1", qty=5000, origin=NORTH, grade="A", fresh=FRESH, cat=CAT, at=T0):
    return event(f"e-{lot}-declare", "LOT_DECLARED", "supply_lot", lot, at, 1,
                 category_spec=cat, unit="kg", quantity=qty, origin_market=origin,
                 quality_grade=grade, fresh_until=fresh)


def inspection(lot, status, at, version):
    return event(f"e-{lot}-insp-{status}-{version}", "INSPECTION_RECORDED",
                 "supply_lot", lot, at, version, status=status)


def pass_lot(lot="L1", at=T0):
    return [lot_declared(lot=lot, at=at), inspection(lot, "passed", at, 2)]


def gap(market, cat=CAT, qty=800, gid="G1", at=T0):
    return event(f"e-gap-{gid}", "GAP_DECLARED", "market_gap", f"gap-{gid}", at, 1,
                 market_ref=market, category_spec=cat, quantity=qty)


def reserve(cid, lot, market, qty, actor=NORTH, release=RELEASE, at=T0):
    return event(f"e-{cid}", "QUANTITY_RESERVED", "purchase_commitment", cid, at, 1,
                 lot_id=lot, actor_market=actor, market_ref=market, quantity=qty,
                 release_at=release)


def transfer(oid, lot, qty, to, *, actor=NORTH, frm=NORTH, responsible=NORTH,
             emergency=False, waybill=None, is_plan=False, parent=None,
             sacrifices=(), grade="A", at=T0):
    return event(f"e-{oid}-order", "TRANSFER_ORDERED", "transfer_order", oid, at, 1,
                 lot_id=lot, actor_market=actor, from_market=frm, to_market=to,
                 quantity=qty, quality_grade=grade, responsible_market=responsible,
                 emergency=emergency, waybill_no=waybill, is_plan=is_plan,
                 parent_order_id=parent, sacrifice_commitment_ids=list(sacrifices))


def transit(uid, waybill, status, at, new_dest=None):
    payload = {"waybill_no": waybill, "status": status,
               "location_ref": "节点-1", "observed_at": at}
    if new_dest:
        payload["new_to_market"] = new_dest
    return event(f"e-transit-{uid}", "TRANSIT_UPDATED", "transit_leg",
                 f"leg-{uid}", at, 1, **payload)


def arrive(oid, waybill, receiver, qty, receipt, at, *, confirmer=None):
    return event(f"e-{oid}-arrive", "ARRIVAL_CONFIRMED", "transfer_order", oid, at, 2,
                 receipt_no=receipt, waybill_no=waybill, receiver_market=receiver,
                 confirmed_by=confirmer or receiver, received_quantity=qty)


def settle(oid, reason="", at=T0):
    return event(f"e-{oid}-settle", "TRANSFER_SETTLED", "transfer_order", oid, at, 3,
                 loss_reason=reason)


def full_transfer(oid, lot, qty, to, waybill, *, received=None, reason="",
                  emergency=False, sacrifices=(), at=T0, responsible=NORTH):
    received = qty if received is None else received
    return [
        transfer(oid, lot, qty, to, emergency=emergency, waybill=waybill,
                 sacrifices=sacrifices, at=at, responsible=responsible),
        arrive(oid, waybill, to, received, f"R-{oid}", at),
        settle(oid, reason or ("运输损耗" if received < qty else ""), at),
    ]


class LedgerRuleTests(unittest.TestCase):
    def assert_rejected(self, events, code):
        led = Ledger()
        with self.assertRaises(DomainError) as ctx:
            led.append_all(events)
        self.assertEqual(code, ctx.exception.code)

    def assert_append_rejected(self, led, evt, code):
        with self.assertRaises(DomainError) as ctx:
            led.append(evt)
        self.assertEqual(code, ctx.exception.code)

    # -- 可售口径 -------------------------------------------------------------

    def test_declared_or_detained_is_not_sellable(self):
        led = Ledger([lot_declared()])
        lot = led.view().lots["L1"]
        self.assertEqual(0.0, led.view().allocatable(lot))
        # 检验暂扣同样不可分配
        led.append(inspection("L1", "detained", T0, 2))
        self.assertEqual(0.0, led.view().allocatable(led.view().lots["L1"]))
        # 放行（released 视为 passed）后可分配
        led.append(inspection("L1", "released", T0, 3))
        self.assertEqual(5000.0, led.view().allocatable(led.view().lots["L1"]))

    def test_reserve_cannot_exceed_real_share(self):
        # 未检验放行：不能锁货
        self.assert_rejected([lot_declared(), reserve("C1", "L1", SOUTH, 100)],
                             "insufficient_share")
        # 放行后：并发两笔 3000，只有一笔能成
        led = Ledger(pass_lot())
        led.append(reserve("C1", "L1", SOUTH, 3000))
        with self.assertRaises(DomainError) as ctx:
            led.append(reserve("C2", "L1", EAST, 3000))
        self.assertEqual("insufficient_share", ctx.exception.code)
        self.assertEqual(2000.0, led.view().allocatable(led.view().lots["L1"]))

    def test_inflight_and_intransit_are_not_sellable_anywhere(self):
        led = Ledger(pass_lot() + full_transfer("T1", "L1", 3000, SOUTH, "W1"))
        north = led.explain_market(NORTH)["categories"][CAT]
        # 已结算出库：北方余量只剩 2000
        self.assertEqual(2000.0, north["free"])
        south = led.explain_market(SOUTH)["categories"][CAT]
        self.assertEqual(3000.0, south["received"])

    def test_arrived_pending_settlement_not_counted(self):
        led = Ledger(pass_lot() + [
            transfer("T1", "L1", 3000, SOUTH, waybill="W1"),
            arrive("T1", "W1", SOUTH, 3000, "R1", T0),
        ])
        south = led.explain_market(SOUTH)["categories"][CAT]
        self.assertEqual(3000.0, south["arrived_pending_settlement"])
        self.assertEqual(0.0, south["free"])

    # -- 处置权与签收权 -------------------------------------------------------

    def test_only_origin_market_may_dispose(self):
        self.assert_rejected(
            pass_lot() + [reserve("C1", "L1", SOUTH, 100, actor=SOUTH)],
            "disposal_denied",
        )
        self.assert_rejected(
            pass_lot() + [transfer("T1", "L1", 100, EAST, actor=SOUTH, frm=NORTH,
                                   waybill="W1")],
            "disposal_denied",
        )

    def test_receiver_market_confirms_arrival_only(self):
        events = pass_lot() + [
            transfer("T1", "L1", 100, SOUTH, waybill="W1"),
            arrive("T1", "W1", SOUTH, 100, "R1", T0, confirmer=NORTH),
        ]
        self.assert_rejected(events, "receiver_only")

    # -- 职责分离 -------------------------------------------------------------

    def test_price_monitor_cannot_approve_own_exemption(self):
        ask = event("X1", "EXEMPTION_REQUESTED", "price_exemption", "X1", T0, 1,
                    market_ref=SOUTH, requester_id="monitor-li", reason="台风运费")
        self.assert_rejected(
            [ask, event("X1a", "EXEMPTION_APPROVED", "price_exemption", "X1", T0, 2,
                        approver_id="monitor-li")],
            "self_approval",
        )
        led = Ledger([ask])
        led.append(event("X1b", "EXEMPTION_APPROVED", "price_exemption", "X1", T0, 2,
                         approver_id="director-wang"))
        self.assertEqual("director-wang", led.view().exemptions["X1"].approver_id)

    # -- 应急分拆守恒 ---------------------------------------------------------

    def test_emergency_split_conserves_total_grade_responsibility(self):
        led = Ledger(pass_lot())
        # 父单：3000 的应急额度，只是计划，不占库存
        led.append(transfer("P1", "L1", 3000, SOUTH, emergency=True, is_plan=True))
        led.append(transfer("K1", "L1", 2000, SOUTH, emergency=True, waybill="W1",
                            parent="P1"))
        led.append(transfer("K2", "L1", 1000, EAST, emergency=True, waybill="W2",
                            parent="P1"))
        # 超出父单总量
        self.assert_append_rejected(
            led, transfer("K3", "L1", 1, EAST, emergency=True, waybill="W3", parent="P1"),
            "split_total_exceeded",
        )
        # 责任归属不得变更
        self.assert_append_rejected(
            led, transfer("K4", "L1", 100, EAST, emergency=True, waybill="W4",
                          parent="P1", responsible=EAST),
            "responsibility_changed",
        )
        # 分拆不得借机降级
        self.assert_append_rejected(
            led, transfer("K5", "L1", 100, EAST, emergency=True, waybill="W5",
                          parent="P1", grade="B"),
            "grade_mismatch",
        )
        # 非应急调拨不能分拆
        led.append(transfer("P2", "L1", 100, SOUTH, emergency=False, is_plan=True))
        self.assert_append_rejected(
            led, transfer("K6", "L1", 50, SOUTH, emergency=False, waybill="W6",
                          parent="P2"),
            "split_requires_emergency",
        )

    # -- 物流回执与运单 -------------------------------------------------------

    def test_duplicate_receipt_does_not_double_count(self):
        led = Ledger(pass_lot() + [
            transfer("T1", "L1", 100, SOUTH, waybill="W1"),
            arrive("T1", "W1", SOUTH, 90, "R1", T0),
        ])
        before = led.view().orders["T1"].received_qty
        # 同号回执重传（新事件 id）：静默忽略，不重复增减
        dup = dict(arrive("T1", "W1", SOUTH, 90, "R1", T0))
        dup["event_id"] = "e-T1-arrive-retx"
        led.append(dup)
        self.assertEqual(before, led.view().orders["T1"].received_qty)
        self.assertEqual(1, len(led.view().receipts))

    def test_waybill_duplicate_and_freeze(self):
        led = Ledger(pass_lot())
        led.append(transfer("T1", "L1", 100, SOUTH, waybill="W1"))
        # 完全重复运单
        with self.assertRaises(DomainError) as ctx:
            led.append(transfer("T2", "L1", 100, SOUTH, waybill="W1"))
        self.assertEqual("waybill_duplicate", ctx.exception.code)
        # 同号不同数量：冻结
        with self.assertRaises(DomainError) as ctx:
            led.append(transfer("T3", "L1", 120, SOUTH, waybill="W1"))
        self.assertEqual("waybill_frozen", ctx.exception.code)
        self.assertTrue(led.view().waybills["W1"]["frozen"])
        # 冻结期间在途更新被拒
        with self.assertRaises(DomainError):
            led.append(transit("u1", "W1", "in_transit", T0))

    def test_diversion_changes_destination_with_audit_trail(self):
        led = Ledger(pass_lot() + [
            transfer("T1", "L1", 100, SOUTH, waybill="W1"),
            transit("u1", "W1", "diverted", T0, new_dest=EAST),
            arrive("T1", "W1", EAST, 100, "R1", T0),
            settle("T1"),
        ])
        info = led.explain_transfer("T1")
        self.assertEqual(SOUTH, info["diverted_from"])
        self.assertEqual(EAST, info["to_market"])

    # -- 故障恢复幂等 ---------------------------------------------------------

    def test_recovery_replay_does_not_ship_or_settle_twice(self):
        led = Ledger(pass_lot() + full_transfer("T1", "L1", 100, SOUTH, "W1"))
        snapshot = {
            "in_flight": led.view().lots["L1"].in_flight,
            "out": led.view().lots["L1"].transferred_out,
            "receipts": len(led.view().receipts),
        }
        # 模拟故障后从事件日志重建，并重放结算事件
        led2 = Ledger(led.events)
        self.assertFalse(led2.append(settle("T1")))  # 相同 event_id：幂等忽略
        lot2 = led2.view().lots["L1"]
        self.assertEqual(snapshot["in_flight"], lot2.in_flight)
        self.assertEqual(snapshot["out"], lot2.transferred_out)
        self.assertEqual(snapshot["receipts"], len(led2.view().receipts))

    # -- 损耗 -----------------------------------------------------------------

    def test_loss_requires_reason_and_is_recorded(self):
        events = pass_lot() + [
            transfer("T1", "L1", 100, SOUTH, waybill="W1"),
            arrive("T1", "W1", SOUTH, 90, "R1", T0),
            settle("T1", reason=""),
        ]
        self.assert_rejected(events, "loss_reason_required")
        led = Ledger(pass_lot() + full_transfer(
            "T1", "L1", 100, SOUTH, "W1", received=90, reason="挤压变质"))
        info = led.explain_transfer("T1")
        self.assertEqual(10.0, info["loss_qty"])
        self.assertEqual("挤压变质", info["loss_reason"])

    # -- 牺牲承诺 -------------------------------------------------------------

    def test_emergency_transfer_must_name_sacrificed_commitments(self):
        led = Ledger(pass_lot())
        led.append(reserve("C1", "L1", SOUTH, 4000))  # 仅剩 1000 可分配
        # 不声明牺牲 → 拒绝
        with self.assertRaises(DomainError) as ctx:
            led.append(transfer("T1", "L1", 3000, EAST, emergency=True, waybill="W1"))
        self.assertEqual("sacrifices_insufficient", ctx.exception.code)
        # 显式牺牲 C1 的 2000 → 通过并留痕
        led.append(transfer("T1", "L1", 3000, EAST, emergency=True, waybill="W1",
                            sacrifices=["C1"]))
        info = led.explain_transfer("T1")
        self.assertEqual([("C1", 2000.0)],
                         [(s["commitment_id"], s["quantity"])
                          for s in info["sacrificed_commitments"]])
        self.assertEqual(2000.0, led.view().commitments["C1"].sacrificed_qty)

    # -- 模拟日期 -------------------------------------------------------------

    def test_simulated_date_in_transit_and_post_holiday_release(self):
        led = Ledger([
            lot_declared(lot="L1", fresh="2026-10-12T08:00:00+08:00"),
            inspection("L1", "passed", "2026-09-30T08:00:00+08:00", 2),
            reserve("C1", "L1", SOUTH, 3000, release="2026-10-08T10:00:00+08:00",
                    at="2026-09-30T09:00:00+08:00"),
            transfer("T1", "L1", 1000, EAST, waybill="W1",
                     at="2026-09-30T10:00:00+08:00"),
        ])
        # 09-30：1000 在途（不计可售），3000 锁定，可分配 1000
        v = led.view("2026-09-30T12:00:00+08:00")
        self.assertEqual(1000.0, v.allocatable(v.lots["L1"]))
        east = led.explain_market(EAST, "2026-09-30T12:00:00+08:00")["categories"]
        self.assertEqual(1000.0, east[CAT]["inbound_in_transit"])
        # 10-09：节后承诺自动释放，在途之外全部可分配
        v = led.view("2026-10-09T08:00:00+08:00")
        self.assertEqual(4000.0, v.allocatable(v.lots["L1"]))

    def test_simulated_date_near_expiry_and_expired(self):
        led = Ledger([
            lot_declared(lot="L2", qty=1000, fresh="2026-10-02T10:00:00+08:00"),
            inspection("L2", "passed", "2026-09-30T08:00:00+08:00", 2),
        ])
        # 10-01：保鲜窗口前 24 小时内，标临期但仍可售
        north = led.explain_market(NORTH, "2026-10-01T12:00:00+08:00")["categories"][CAT]
        self.assertEqual(1000.0, north["near_expiry"])
        self.assertEqual(1000.0, north["free"])
        # 10-03：超过保鲜窗口，不可售
        north = led.explain_market(NORTH, "2026-10-03T08:00:00+08:00")["categories"][CAT]
        self.assertEqual(0.0, north["free"])
        self.assertEqual(1000.0, north["expired"])

    # -- 价格依据 -------------------------------------------------------------

    def test_price_assessment_uses_only_arrived_facts(self):
        led = Ledger(pass_lot() + [
            transfer("T1", "L1", 1000, SOUTH, waybill="W1"),
            arrive("T1", "W1", SOUTH, 1000, "R1", T0),
        ])
        # 引用不存在/未到货的凭据
        bad = event("A1", "GUIDANCE_PRICE_ASSESSED", "price_assessment", "A1", T0, 1,
                    market_ref=SOUTH, category_spec=CAT, guidance_price=6.5,
                    evidence_receipts=["R-NOPE"])
        with self.assertRaises(DomainError) as ctx:
            led.append(bad)
        self.assertEqual("evidence_not_arrived", ctx.exception.code)
        led.append(event("A2", "GUIDANCE_PRICE_ASSESSED", "price_assessment", "A2", T0, 1,
                         market_ref=SOUTH, category_spec=CAT, guidance_price=6.5,
                         evidence_receipts=["R1"]))
        basis = led.price_basis("A2")
        self.assertEqual(1000.0, basis["arrived_qty"])
        self.assertEqual(["R1"], [x["receipt_no"] for x in basis["evidence"]])

    # -- 可解释性 -------------------------------------------------------------

    def test_explain_shortage_and_surplus(self):
        led = Ledger(pass_lot() + [
            reserve("C1", "L1", SOUTH, 4500),
            gap(NORTH, qty=800, gid="1"),
        ])
        north = led.explain_market(NORTH)["categories"][CAT]
        self.assertEqual("shortage", north["status"])  # 可售 500 < 缺口 800
        led.append(gap(NORTH, qty=-400, gid="2"))
        north = led.explain_market(NORTH)["categories"][CAT]
        self.assertEqual("surplus", north["status"])  # 缺口降至 400，余量 100
        joined = " ".join(north["reasons"])
        self.assertIn("采购承诺", joined)

    # -- 信封契约交叉校验 -----------------------------------------------------

    def test_ledger_events_satisfy_envelope_contract(self):
        import json
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text("utf-8"))
        events = (pass_lot()
                  + [gap(SOUTH), reserve("C1", "L1", SOUTH, 100)]
                  + full_transfer("T1", "L1", 100, EAST, "W1"))
        for e in events:
            self.assertEqual([], [(i.field, i.code) for i in validate_event(e, schema)])

    def test_version_must_be_contiguous_per_aggregate(self):
        led = Ledger(pass_lot())
        bad = inspection("L1", "passed", T0, 5)  # 应为 3（pass_lot 未含检验时为 2）
        with self.assertRaises(DomainError) as ctx:
            led2 = Ledger([lot_declared(), bad])
        self.assertEqual("version_conflict", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
