"""跨市场节日保供调拨账核心规则。

事件溯源账本：只追加事件，投影按需重放。所有业务守恒规则集中在
``Projection.apply``，查询（含模拟日期）通过 ``Ledger.view(as_of=...)``
按 ``occurred_at`` 过滤后重放得到，因此故障恢复只需重放事件日志，
不会重复发货或结算。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

NEAR_EXPIRY = timedelta(days=1)


class DomainError(Exception):
    """违反领域守恒规则。``code`` 供调用方稳定分支判断。"""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


def _parse_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- 事件构造


def event(
    event_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    occurred_at: str,
    version: int,
    **payload: Any,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "payload": payload,
    }


# --------------------------------------------------------------------------- 投影状态


@dataclass
class Lot:
    lot_id: str
    category_spec: str
    unit: str
    quantity: float
    origin_market: str
    quality_grade: str
    fresh_until: datetime
    inspection: str = "pending"  # pending / passed / detained / released
    reservations: dict[str, float] = field(default_factory=dict)  # commitment_id -> 剩余锁定量
    in_flight: float = 0.0  # 已开单未结算（含在途、已到未结）
    transferred_out: float = 0.0  # 已结算出库（含损耗）
    sacrificed: float = 0.0


@dataclass
class Commitment:
    commitment_id: str
    lot_id: str
    market_ref: str
    quantity: float
    release_at: datetime
    sacrificed_qty: float = 0.0
    sacrificed_for: list[str] = field(default_factory=list)

    @property
    def active_qty(self) -> float:
        return self.quantity - self.sacrificed_qty


@dataclass
class TransferOrder:
    order_id: str
    lot_id: str
    quantity: float
    quality_grade: str
    from_market: str
    to_market: str
    waybill_no: str | None
    responsible_market: str
    emergency: bool
    parent_id: str | None = None
    child_ids: list[str] = field(default_factory=list)
    sacrifices_ids: list[str] = field(default_factory=list)
    sacrifices: list[tuple[str, float]] = field(default_factory=list)  # (commitment_id, 数量)
    arrived: bool = False
    received_qty: float = 0.0
    receipt_no: str | None = None
    settled: bool = False
    loss_qty: float = 0.0
    loss_reason: str = ""
    diverted_from: str | None = None

    @property
    def is_plan(self) -> bool:
        return self.waybill_no is None


@dataclass
class Receipt:
    receipt_no: str
    waybill_no: str
    order_id: str
    market_ref: str
    quantity: float
    received_at: datetime


@dataclass
class Exemption:
    exemption_id: str
    market_ref: str
    requester_id: str
    approver_id: str | None = None
    reason: str = ""


@dataclass
class Assessment:
    assessment_id: str
    market_ref: str
    category_spec: str
    guidance_price: float
    evidence: list[str]  # receipt_no
    arrived_qty: float


class Projection:
    """事件重放得到的当前状态，并在写入时执行业务不变量。"""

    def __init__(self, as_of: datetime | None = None) -> None:
        self.as_of = as_of
        self.lots: dict[str, Lot] = {}
        self.gaps: dict[tuple[str, str], float] = {}
        self.commitments: dict[str, Commitment] = {}
        self.orders: dict[str, TransferOrder] = {}
        self.waybills: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str,Receipt] = {}
        self.exemptions: dict[str, Exemption] = {}
        self.assessments: dict[str, Assessment] = {}
        self.event_ids: set[str] = set()
        self._versions: dict[str, int] = defaultdict(int)

    # -- 基础守卫 -------------------------------------------------------------

    def _require(self, condition: bool, code: str, message: str) -> None:
        if not condition:
            raise DomainError(code, message)

    def _p(self, e: Mapping[str, Any]) -> Mapping[str, Any]:
        return e["payload"]

    def _is_duplicate_business_event(self, e: Mapping[str, Any]) -> bool:
        """业务键幂等：相同回执/已结算单重传，即使事件标识不同也忽略。"""
        p = e.get("payload")
        if not isinstance(p, Mapping):
            return False
        if e["event_type"] == "ARRIVAL_CONFIRMED":
            return p.get("receipt_no") in self.receipts
        if e["event_type"] == "TRANSFER_SETTLED":
            order = self.orders.get(e["aggregate_id"])
            return order is not None and order.settled
        return False

    def apply(self, e: Mapping[str, Any]) -> bool:
        """应用事件；重复事件为幂等重传，返回 ``False`` 且不改状态。"""
        eid = e["event_id"]
        if eid in self.event_ids:
            return False
        if self._is_duplicate_business_event(e):
            return False
        occurred = _parse_at(e["occurred_at"])
        if self.as_of is not None and occurred > self.as_of:
            return False  # 模拟时间点之前尚未发生
        expected = self._versions[e["aggregate_id"]] + 1
        self._require(
            e["version"] == expected,
            "version_conflict",
            f"聚合 {e['aggregate_id']} 版本应为 {expected}，实际 {e['version']}",
        )
        handler = getattr(self, f"_on_{e['event_type'].lower()}")
        handler(e)
        self._versions[e["aggregate_id"]] = expected
        self.event_ids.add(eid)
        return True

    # -- 可用份额 -------------------------------------------------------------

    def _lot(self, lot_id: str) -> Lot:
        self._require(lot_id in self.lots, "lot_unknown", f"货源批次 {lot_id} 不存在")
        return self.lots[lot_id]

    def allocatable(self, lot: Lot, at: datetime | None = None) -> float:
        """真正可分配的份额：在库、检验放行、未锁定、未出库、未过保鲜期。"""
        at = at or self.as_of
        remaining = lot.quantity - lot.transferred_out - lot.in_flight
        if lot.inspection in ("pending", "detained"):
            return 0.0
        reserved = 0.0
        for cid, qty in lot.reservations.items():
            commit = self.commitments[cid]
            if at is not None and at >= commit.release_at:
                continue  # 节后自动释放
            reserved += qty
        free = remaining - reserved
        if at is not None and at > lot.fresh_until:
            free = 0.0  # 超出保鲜窗口不可售
        return max(free, 0.0)

    # -- 事件处理 -------------------------------------------------------------

    def _on_lot_declared(self, e: Mapping[str, Any]) -> None:
        p = self._p(e)
        lot_id = e["aggregate_id"]
        self._require(lot_id not in self.lots, "lot_exists", f"货源批次 {lot_id} 已申报")
        self._require(p["quantity"] > 0, "quantity_positive", "申报数量必须为正")
        self.lots[lot_id] = Lot(
            lot_id=lot_id,
            category_spec=p["category_spec"],
            unit=p.get("unit", "kg"),
            quantity=float(p["quantity"]),
            origin_market=p["origin_market"],
            quality_grade=p["quality_grade"],
            fresh_until=_parse_at(p["fresh_until"]),
        )

    def _on_gap_declared(self, e: Mapping[str, Any]) -> None:
        p = self._p(e)
        key = (p["market_ref"], p["category_spec"])
        self.gaps[key] = self.gaps.get(key, 0.0) + float(p["quantity"])

    def _on_inspection_recorded(self, e: Mapping[str, Any]) -> None:
        lot = self._lot(e["aggregate_id"])
        status = self._p(e)["status"]
        self._require(
            status in ("passed", "detained", "released"),
            "inspection_status_unknown",
            f"未知检验状态 {status}",
        )
        lot.inspection = "passed" if status == "released" else status

    def _on_quantity_reserved(self, e: Mapping[str, Any]) -> None:
        p = self._p(e)
        lot = self._lot(p["lot_id"])
        # 来源市场保留处置权：只有产地市场能锁定自己批次的货。
        self._require(
            p["actor_market"] == lot.origin_market,
            "disposal_denied",
            f"只有来源市场 {lot.origin_market} 可处置批次 {lot.lot_id}",
        )
        qty = float(p["quantity"])
        self._require(qty > 0, "quantity_positive", "锁定数量必须为正")
        free = self.allocatable(lot, _parse_at(e["occurred_at"]))
        self._require(
            qty <= free + 1e-9,
            "insufficient_share",
            f"批次 {lot.lot_id} 可分配 {free:g}，无法锁定 {qty:g}（在途与暂扣均不可承诺）",
        )
        cid = e["aggregate_id"]
        self._require(cid not in self.commitments, "commitment_exists", f"采购承诺 {cid} 已存在")
        commit = Commitment(
            commitment_id=cid,
            lot_id=lot.lot_id,
            market_ref=p["market_ref"],
            quantity=qty,
            release_at=_parse_at(p["release_at"]),
        )
        self.commitments[cid] = commit
        lot.reservations[cid] = qty

    def _apply_sacrifices(
        self, lot: Lot, order: TransferOrder, shortfall: float, occurred: datetime
    ) -> None:
        needed = shortfall
        for cid in order.sacrifices_ids:
            commit = self.commitments.get(cid)
            self._require(commit is not None, "commitment_unknown", f"采购承诺 {cid} 不存在")
            self._require(commit.lot_id == lot.lot_id, "sacrifice_lot_mismatch", "被牺牲承诺不属于该批次")
            active = lot.reservations.get(cid, 0.0)
            if occurred >= commit.release_at:
                active = 0.0
            take = min(active, needed)
            self._require(take > 0, "sacrifice_inactive", f"承诺 {cid} 无可牺牲的有效锁定")
            lot.reservations[cid] = active - take
            commit.sacrificed_qty += take
            commit.sacrificed_for.append(order.order_id)
            order.sacrifices.append((cid, take))
            needed -= take
            if needed <= 1e-9:
                break
        self._require(
            needed <= 1e-9,
            "sacrifices_insufficient",
            f"调拨超出可分配 {shortfall:g}，声明牺牲的承诺不足以补齐",
        )

    def _register_waybill(self, order: TransferOrder, lot: Lot) -> None:
        wb = self.waybills.get(order.waybill_no) if order.waybill_no else None
        if wb is not None:
            conflict = (
                abs(wb["quantity"] - order.quantity) > 1e-9 or wb["to_market"] != order.to_market
            )
            if conflict or wb["frozen"]:
                wb["frozen"] = True
                raise DomainError(
                    "waybill_frozen",
                    f"运单 {order.waybill_no} 数量或去向与既有记录不一致，已冻结核对",
                    {"waybill_no": order.waybill_no},
                )
            raise DomainError(
                "waybill_duplicate",
                f"运单 {order.waybill_no} 已被调拨单 {wb['order_id']} 使用",
                {"waybill_no": order.waybill_no},
            )
        self.waybills[order.waybill_no] = {
            "order_id": order.order_id,
            "lot_id": lot.lot_id,
            "quantity": order.quantity,
            "to_market": order.to_market,
            "status": "ordered",
            "frozen": False,
        }

    def _on_transfer_ordered(self, e: Mapping[str, Any]) -> None:
        p = self._p(e)
        oid = e["aggregate_id"]
        lot = self._lot(p["lot_id"])
        qty = float(p["quantity"])
        self._require(qty > 0, "quantity_positive", "调拨数量必须为正")
        self._require(
            p["quality_grade"] == lot.quality_grade,
            "grade_mismatch",
            f"调拨等级必须与批次一致（{lot.quality_grade}），应急分拆不得降级",
        )
        parent_id = p.get("parent_order_id")
        parent: TransferOrder | None = None
        if parent_id is not None:
            parent = self.orders.get(parent_id)
            self._require(parent is not None, "parent_unknown", f"父调拨单 {parent_id} 不存在")
            self._require(parent.emergency, "split_requires_emergency", "只有应急调拨可拆分批次")
            self._require(parent.lot_id == lot.lot_id, "split_lot_mismatch", "分拆批次必须与父单同源")
            self._require(
                p["responsible_market"] == parent.responsible_market,
                "responsibility_changed",
                "分拆批次责任归属必须与父单一致",
            )
            used = sum(self.orders[c].quantity for c in parent.child_ids)
            self._require(
                used + qty <= parent.quantity + 1e-9,
                "split_total_exceeded",
                f"分拆总量 {used + qty:g} 超过父单 {parent.quantity:g}，总量必须守恒",
            )
        is_plan = bool(p.get("is_plan", False))
        self._require(
            p["actor_market"] == p["from_market"] == lot.origin_market,
            "disposal_denied",
            f"只有来源市场 {lot.origin_market} 可发起调拨",
        )
        order = TransferOrder(
            order_id=oid,
            lot_id=lot.lot_id,
            quantity=qty,
            quality_grade=p["quality_grade"],
            from_market=p["from_market"],
            to_market=p["to_market"],
            waybill_no=p.get("waybill_no"),
            responsible_market=p["responsible_market"],
            emergency=bool(p["emergency"]),
            parent_id=parent_id,
            sacrifices_ids=list(p.get("sacrifice_commitment_ids", [])),
        )
        if is_plan:
            # 父单只是授权总额度，本身不占库存、不承运。
            self._require(order.waybill_no is None, "plan_has_waybill", "计划父单不应携带运单")
        else:
            self._require(
                isinstance(order.waybill_no, str) and order.waybill_no.strip(),
                "waybill_required",
                "实际调拨必须携带运单号",
            )
            free = self.allocatable(lot, _parse_at(e["occurred_at"]))
            if qty > free + 1e-9:
                self._apply_sacrifices(lot, order, qty - free, _parse_at(e["occurred_at"]))
            self._register_waybill(order, lot)
            lot.in_flight += qty
            self.waybills[order.waybill_no]["status"] = "shipped"
        self.orders[oid] = order
        if parent is not None:
            parent.child_ids.append(oid)

    def _on_waybill_frozen(self, e: Mapping[str, Any]) -> None:
        wb = self.waybills.get(self._p(e)["waybill_no"])
        self._require(wb is not None, "waybill_unknown", "冻结的运单不存在")
        wb["frozen"] = True

    def _on_transit_updated(self, e: Mapping[str, Any]) -> None:
        p = self._p(e)
        wb = self.waybills.get(p["waybill_no"])
        self._require(wb is not None, "waybill_unknown", f"运单 {p['waybill_no']} 不存在")
        self._require(not wb["frozen"], "waybill_frozen", "运单已冻结，须核对解冻后方可更新")
        status = p["status"]
        self._require(
            status in ("in_transit", "arrived", "diverted"),
            "transit_status_unknown",
            f"未知在途状态 {status}",
        )
        if status == "diverted":
            new_dest = p["new_to_market"]
            order = self.orders[wb["order_id"]]
            order.diverted_from = wb["to_market"]
            order.to_market = new_dest
            wb["to_market"] = new_dest
        wb["status"] = status

    def _on_arrival_confirmed(self, e: Mapping[str, Any]) -> None:
        p = self._p(e)
        receipt_no = p["receipt_no"]
        if receipt_no in self.receipts:
            return  # 相同物流回执重传：幂等，不重复增减库存
        wb = self.waybills.get(p["waybill_no"])
        self._require(wb is not None, "waybill_unknown", f"运单 {p['waybill_no']} 不存在")
        self._require(not wb["frozen"], "waybill_frozen", "运单冻结中不能签收到货")
        order = self.orders[wb["order_id"]]
        # 接收市场只能确认实际到货，来源市场不能替对方签收。
        self._require(
            p["confirmed_by"] == order.to_market and p["receiver_market"] == order.to_market,
            "receiver_only",
            f"只有接收市场 {order.to_market} 能确认实际到货",
        )
        qty = float(p["received_quantity"])
        self._require(0 <= qty <= order.quantity + 1e-9, "received_exceeds_shipped",
                      "签收数量不能超过发运数量")
        receipt = Receipt(
            receipt_no=receipt_no,
            waybill_no=p["waybill_no"],
            order_id=order.order_id,
            market_ref=order.to_market,
            quantity=qty,
            received_at=_parse_at(e["occurred_at"]),
        )
        self.receipts[receipt_no] = receipt
        order.arrived = True
        order.received_qty = qty
        order.receipt_no = receipt_no
        wb["status"] = "arrived"

    def _on_transfer_settled(self, e: Mapping[str, Any]) -> None:
        p = self._p(e)
        order = self.orders.get(e["aggregate_id"])
        self._require(order is not None, "order_unknown", "调拨单不存在")
        if order.settled:
            return  # 故障恢复重放：结算幂等，不重复划转
        self._require(order.arrived, "not_arrived", "未签收到货不能结算")
        self._require(not order.is_plan, "plan_not_settleable", "计划父单不能直接结算")
        loss = order.quantity - order.received_qty
        self._require(loss >= -1e-9, "loss_negative", "损耗不能为负")
        if loss > 1e-9:
            self._require(
                bool(str(p.get("loss_reason", "")).strip()),
                "loss_reason_required",
                "存在损耗时必须填写损耗原因",
            )
        lot = self.lots[order.lot_id]
        lot.in_flight -= order.quantity
        lot.transferred_out += order.quantity  # 实收与损耗都离开发源方账目
        lot.sacrificed += sum(q for _, q in order.sacrifices)
        order.settled = True
        order.loss_qty = loss
        order.loss_reason = str(p.get("loss_reason", ""))
        self.waybills[order.waybill_no]["status"] = "settled"

    def _on_exemption_requested(self, e: Mapping[str, Any]) -> None:
        p = self._p(e)
        xid = e["aggregate_id"]
        self.exemptions[xid] = Exemption(
            exemption_id=xid,
            market_ref=p["market_ref"],
            requester_id=p["requester_id"],
            reason=p.get("reason", ""),
        )

    def _on_exemption_approved(self, e: Mapping[str, Any]) -> None:
        p = self._p(e)
        ex = self.exemptions.get(e["aggregate_id"])
        self._require(ex is not None, "exemption_unknown", "异常豁免申请不存在")
        self._require(ex.approver_id is None, "exemption_already_approved", "豁免已批准")
        # 承担价格监测的人不能批准自己提出的异常豁免。
        self._require(
            p["approver_id"] != ex.requester_id,
            "self_approval",
            "价格监测人不能批准自己提出的异常豁免",
        )
        ex.approver_id = p["approver_id"]

    def _on_guidance_price_assessed(self, e: Mapping[str, Any]) -> None:
        p = self._p(e)
        refs = list(p.get("evidence_receipts", []))
        self._require(refs, "evidence_required", "指导价判断必须引用已到货签收凭据")
        total = 0.0
        for ref in refs:
            receipt = self.receipts.get(ref)
            self._require(receipt is not None, "evidence_not_arrived",
                          f"凭据 {ref} 不是已到货事实，不能计入价格判断")
            order = self.orders[receipt.order_id]
            lot = self.lots[order.lot_id]
            self._require(
                lot.category_spec == p["category_spec"],
                "evidence_category_mismatch",
                f"凭据 {ref} 品类与指导价判断不符",
            )
            total += receipt.quantity
        self.assessments[e["aggregate_id"]] = Assessment(
            assessment_id=e["aggregate_id"],
            market_ref=p["market_ref"],
            category_spec=p["category_spec"],
            guidance_price=float(p["guidance_price"]),
            evidence=refs,
            arrived_qty=total,
        )


# --------------------------------------------------------------------------- 账本与查询


class Ledger:
    """追加式事件日志；任何时点的状态都由重放得到。"""

    def __init__(self, events: Iterable[Mapping[str, Any]] = ()) -> None:
        self._events: list[Mapping[str, Any]] = []
        self.append_all(events)

    @staticmethod
    def _project(events: Iterable[Mapping[str, Any]], as_of: datetime | None) -> Projection:
        proj = Projection(as_of=as_of)
        for e in events:
            proj.apply(e)
        return proj

    def append(self, e: Mapping[str, Any]) -> bool:
        # 先针对“当前”状态校验并落账；重复事件直接幂等忽略。
        proj = self._project(self._events, None)
        try:
            applied = proj.apply(e)
        except DomainError as err:
            # 运单冻结是持久状态：冲突事件虽被拒绝，冻结事实必须落账，
            # 否则重放（故障恢复）后冻结会丢失。
            wb_no = err.details.get("waybill_no")
            if err.code == "waybill_frozen" and wb_no is not None:
                freeze_id = f"__freeze-{wb_no}"
                if not any(x["event_id"] == freeze_id for x in self._events):
                    self._events.append(event(
                        freeze_id, "WAYBILL_FROZEN", "transit_leg",
                        f"waybill-{wb_no}", e["occurred_at"],
                        self._next_version(f"waybill-{wb_no}"), waybill_no=wb_no,
                    ))
            raise
        if applied:
            self._events.append(e)
        return applied

    def _next_version(self, aggregate_id: str) -> int:
        return sum(1 for x in self._events if x["aggregate_id"] == aggregate_id) + 1

    def append_all(self, events: Iterable[Mapping[str, Any]]) -> None:
        for e in events:
            self.append(e)

    def view(self, as_of: str | datetime | None = None) -> Projection:
        moment = _parse_at(as_of) if isinstance(as_of, str) else as_of
        return self._project(self._events, moment)

    @property
    def events(self) -> list[Mapping[str, Any]]:
        return list(self._events)

    # -- 解释性查询 -----------------------------------------------------------

    def explain_market(self, market: str, as_of: str | datetime | None = None) -> dict[str, Any]:
        """说明某市场为何缺货或仍有余量。"""
        proj = self.view(as_of)
        at = proj.as_of
        rows: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"on_hand": 0.0, "free": 0.0, "locked": 0.0, "detained": 0.0,
                      "expired": 0.0, "near_expiry": 0.0, "inbound_in_transit": 0.0,
                      "arrived_pending_settlement": 0.0, "received": 0.0,
                      "diverted_away": 0.0, "gap": 0.0, "reasons": []}
        )
        for lot in proj.lots.values():
            if lot.origin_market != market:
                continue
            r = rows[lot.category_spec]
            owned = max(lot.quantity - lot.transferred_out - lot.in_flight, 0.0)
            r["on_hand"] += owned
            if lot.inspection in ("pending", "detained"):
                r["detained"] += owned
                r["reasons"].append(f"批次 {lot.lot_id} 检验{lot.inspection}，{owned:g}{lot.unit} 暂扣不可售")
                continue
            locked = sum(
                q for cid, q in lot.reservations.items()
                if not (at is not None and at >= proj.commitments[cid].release_at)
            )
            r["locked"] += locked
            if at is not None and at > lot.fresh_until:
                r["expired"] += owned
                r["reasons"].append(f"批次 {lot.lot_id} 已过保鲜窗口，{owned:g}{lot.unit} 不可售")
                continue
            free = max(owned - locked, 0.0)
            r["free"] += free
            if at is not None and lot.fresh_until - at <= NEAR_EXPIRY:
                r["near_expiry"] += owned
                r["reasons"].append(f"批次 {lot.lot_id} 临期（{lot.fresh_until:%m-%d %H:%M} 前）")
            if locked:
                r["reasons"].append(f"批次 {lot.lot_id} 已被采购承诺锁定 {locked:g}{lot.unit}")
        # 已结算到货：所有权转移完成，计入接收市场可售余量
        for order in proj.orders.values():
            if order.to_market != market or not order.settled:
                continue
            lot = proj.lots[order.lot_id]
            r = rows[lot.category_spec]
            if at is not None and at > lot.fresh_until:
                r["expired"] += order.received_qty
                r["reasons"].append(
                    f"运单 {order.waybill_no} 到货 {order.received_qty:g}{lot.unit} 已过保鲜窗口，不可售"
                )
                continue
            r["received"] += order.received_qty
            r["on_hand"] += order.received_qty
            r["free"] += order.received_qty
            r["reasons"].append(
                f"运单 {order.waybill_no} 已结算到货 {order.received_qty:g}{lot.unit}"
                + (f"（损耗 {order.loss_qty:g}：{order.loss_reason}）" if order.loss_qty else "")
            )
        # 已到货未结算：暂不记可售
        for order in proj.orders.values():
            if order.to_market != market or not order.arrived or order.settled:
                continue
            lot = proj.lots[order.lot_id]
            r = rows[lot.category_spec]
            r["arrived_pending_settlement"] += order.received_qty
            r["reasons"].append(
                f"运单 {order.waybill_no} 到货 {order.received_qty:g}{lot.unit} 待结算，暂不记可售"
            )
        # 在途（尚未到货）——属于“已组织货源”但不可售；改道他处单独记账
        for wb_no, wb in proj.waybills.items():
            order = proj.orders[wb["order_id"]]
            lot = proj.lots[order.lot_id]
            if order.diverted_from == market and order.to_market != market:
                r = rows[lot.category_spec]
                r["diverted_away"] += wb["quantity"]
                r["reasons"].append(
                    f"运单 {wb_no} 原计划到货 {wb['quantity']:g}{lot.unit}，"
                    f"在途改道至 {order.to_market}，本市场未收到"
                )
                continue
            if wb["to_market"] != market or wb["status"] in ("arrived", "settled"):
                continue
            r = rows[lot.category_spec]
            r["inbound_in_transit"] += wb["quantity"]
            r["reasons"].append(
                f"运单 {wb_no} 在途 {wb['quantity']:g}（{wb['status']}），未到货不计可售"
            )
        for (mkt, cat), gap in proj.gaps.items():
            if mkt == market:
                rows[cat]["gap"] += gap
        for cat, r in rows.items():
            balance = r["free"] - r["gap"]
            r["balance_vs_gap"] = balance
            if balance < -1e-9:
                r["status"] = "shortage"
                r["reasons"].append(f"可售余量 {r['free']:g} 低于门店缺口 {r['gap']:g}，缺 {-balance:g}")
            else:
                r["status"] = "surplus"
                r["reasons"].append(f"可售余量 {r['free']:g} 覆盖缺口 {r['gap']:g}，余 {balance:g}")
        return {"market": market, "as_of": at.isoformat() if at else None, "categories": rows}

    def explain_transfer(self, order_id: str, as_of: str | datetime | None = None) -> dict[str, Any]:
        """说明某次调拨牺牲了哪些承诺、损耗与责任归属。"""
        proj = self.view(as_of)
        order = proj.orders.get(order_id)
        if order is None:
            raise DomainError("order_unknown", f"调拨单 {order_id} 不存在")
        lot = proj.lots[order.lot_id]
        sacrificed = []
        for cid, qty in order.sacrifices:
            commit = proj.commitments[cid]
            sacrificed.append({
                "commitment_id": cid, "market_ref": commit.market_ref,
                "quantity": qty, "unit": lot.unit,
            })
        return {
            "order_id": order_id,
            "emergency": order.emergency,
            "lot_id": order.lot_id,
            "category_spec": lot.category_spec,
            "from_market": order.from_market,
            "to_market": order.to_market,
            "quantity": order.quantity,
            "quality_grade": order.quality_grade,
            "responsible_market": order.responsible_market,
            "parent_order_id": order.parent_id,
            "waybill_no": order.waybill_no,
            "diverted_from": order.diverted_from,
            "sacrificed_commitments": sacrificed,
            "arrived": order.arrived,
            "received_qty": order.received_qty,
            "loss_qty": order.loss_qty,
            "loss_reason": order.loss_reason,
            "settled": order.settled,
        }

    def price_basis(self, assessment_id: str) -> dict[str, Any]:
        """说明指导价采用了哪些已到货事实。"""
        proj = self.view()
        a = proj.assessments.get(assessment_id)
        if a is None:
            raise DomainError("assessment_unknown", f"指导价判断 {assessment_id} 不存在")
        evidence = []
        for ref in a.evidence:
            rcpt = proj.receipts[ref]
            evidence.append({
                "receipt_no": ref,
                "received_at": rcpt.received_at.isoformat(),
                "quantity": rcpt.quantity,
                "order_id": rcpt.order_id,
            })
        return {
            "assessment_id": assessment_id,
            "market_ref": a.market_ref,
            "category_spec": a.category_spec,
            "guidance_price": a.guidance_price,
            "arrived_qty": a.arrived_qty,
            "evidence": evidence,
        }
