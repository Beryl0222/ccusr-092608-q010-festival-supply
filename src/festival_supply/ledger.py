"""跨市场节日保供调拨账：事件溯源领域服务。

只追加（append-only）的事件流是唯一事实来源；内存投影全部可由事件重放恢复，
故障恢复后重放同一事件流不会重复发货或重复结算。

业务规则集中在 ``Ledger.record``：

- 事件标识幂等、按聚合的乐观版本并发控制；
- 货源批次数量守恒：申报量 = 在库 + 已锁承诺 + 已发应急调拨 + 已释放/销毁；
- “已组织货源”不等于可售：未到货、检验暂扣、临期、已锁定均不可再承诺；
- 来源市场保留处置权，接收市场只能确认实际到货；
- 应急调拨拆分时总量、品质等级、责任归属守恒，牺牲的采购承诺必须显式点名；
- 同一物流回执/事件重传不重复增减；运单号相同而数量或去向不同即冻结核对；
- 指导价只能引用已签收的到货事实；异常豁免申请人不得自行批准；
- 模拟时钟推进时处理临期释放与节后释放。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"
_SYSTEM_ACTOR = "_system"
_CST = timezone(timedelta(hours=8))
_EPS = 1e-9


def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class LedgerError(Exception):
    """违反领域规则；``code`` 可供上层稳定分支处理。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Actor:
    ref: str
    market_ref: str | None
    roles: frozenset[str]


def _qty(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LedgerError("quantity_invalid", "数量必须是数值")
    return float(value)


# ------------------------------------------------------------------ 事件构造


def _event(event_type: str, aggregate_type: str, aggregate_id: str, payload: dict,
           *, event_id: str | None, version: int, occurred_at: datetime) -> dict[str, Any]:
    return {
        "event_id": event_id or f"system:{event_type}:{aggregate_id}:v{version}",
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at.isoformat(),
        "version": version,
        "payload": payload,
    }


class Ledger:
    """追加事件流并维护可重放的内存投影。"""

    def __init__(self, schema: Mapping[str, Any] | None = None,
                 directory: Mapping[str, Mapping[str, Any]] | None = None,
                 *, now: datetime | None = None) -> None:
        self.schema = schema if schema is not None else load_schema()
        self.directory = {
            ref: Actor(ref, info.get("market_ref"), frozenset(info.get("roles", ())))
            for ref, info in (directory or {}).items()
        }
        self.directory[_SYSTEM_ACTOR] = Actor(_SYSTEM_ACTOR, None, frozenset({"system"}))
        self._clock = now or datetime(2026, 9, 25, 0, 0, tzinfo=_CST)
        self._events: list[dict[str, Any]] = []
        self._seen: dict[str, dict[str, Any]] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._lots: dict[str, dict[str, Any]] = {}
        self._commitments: dict[str, dict[str, Any]] = {}
        self._transfers: dict[str, dict[str, Any]] = {}
        self._waybills: dict[str, dict[str, Any]] = {}
        self._receipts: list[dict[str, Any]] = []
        self._guidance: dict[str, dict[str, Any]] = {}
        self._exemptions: dict[str, dict[str, Any]] = {}
        self._gaps: list[dict[str, Any]] = []

    # ------------------------------------------------------------ 持久化/恢复

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self._events, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def replay(cls, events: list[Mapping[str, Any]], **kwargs: Any) -> "Ledger":
        """从事件流重建账目。重复事件标识不会被重复应用。"""
        ledger = cls(**kwargs)
        for event in events:
            ledger.record(event)
        return ledger

    @classmethod
    def load(cls, path: str | Path, **kwargs: Any) -> "Ledger":
        events = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.replay(events, **kwargs)

    def snapshot_at(self, as_of: datetime) -> "Ledger":
        """取截至某时点的只读投影（用于“当时为何缺货/余量”复盘）。"""
        past = Ledger(
            schema=self.schema,
            directory={a.ref: {"market_ref": a.market_ref, "roles": list(a.roles)}
                       for a in self.directory.values() if a.ref != _SYSTEM_ACTOR},
            now=self._clock)
        for event in self._events:
            if parse_ts(event["occurred_at"]) <= as_of:
                past.record(event)
        past._clock = as_of
        return past

    @property
    def now(self) -> datetime:
        return self._clock

    # ----------------------------------------------------------------- 命令

    def declare_lot(self, *, lot_id: str, actor_ref: str, category: str, spec: str,
                    origin_market_ref: str, owner_ref: str, quantity: float,
                    quality_grade: str, usable_until: datetime,
                    event_id: str | None = None,
                    occurred_at: datetime | None = None) -> dict[str, Any]:
        return self._emit(
            "LOT_DECLARED", "supply_lot", lot_id,
            {"actor_ref": actor_ref, "category": category, "spec": spec,
             "origin_market_ref": origin_market_ref, "owner_ref": owner_ref,
             "quantity": _qty(quantity), "quality_grade": quality_grade,
             "usable_until": usable_until.isoformat()},
            event_id=event_id, occurred_at=occurred_at)

    def record_inspection(self, *, lot_ref: str, actor_ref: str, status: str,
                          event_id: str | None = None,
                          occurred_at: datetime | None = None) -> dict[str, Any]:
        return self._emit(
            "INSPECTION_RECORDED", "supply_lot", lot_ref,
            {"actor_ref": actor_ref, "lot_ref": lot_ref, "status": status},
            event_id=event_id, occurred_at=occurred_at)

    def reserve_quantity(self, *, commitment_id: str, lot_ref: str, actor_ref: str,
                         market_ref: str, quantity: float,
                         release_after: datetime | None = None,
                         event_id: str | None = None,
                         occurred_at: datetime | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"actor_ref": actor_ref, "lot_ref": lot_ref,
                                   "commitment_id": commitment_id,
                                   "market_ref": market_ref, "quantity": _qty(quantity)}
        if release_after is not None:
            payload["release_after"] = release_after.isoformat()
        return self._emit("QUANTITY_RESERVED", "purchase_commitment", commitment_id,
                          payload, event_id=event_id, occurred_at=occurred_at)

    def create_emergency_transfer(
            self, *, transfer_id: str, lot_ref: str, actor_ref: str,
            from_market_ref: str, to_market_ref: str, waybill_no: str,
            quantity: float, quality_grade: str, owner_ref: str,
            source_authorization: str,
            splits: list[Mapping[str, Any]] | None = None,
            sacrifices: list[Mapping[str, Any]] | None = None,
            event_id: str | None = None,
            occurred_at: datetime | None = None) -> dict[str, Any]:
        parts = [dict(s) for s in (splits or [])]
        return self._emit(
            "EMERGENCY_TRANSFER_CREATED", "transfer_order", transfer_id,
            {"actor_ref": actor_ref, "lot_ref": lot_ref,
             "from_market_ref": from_market_ref, "to_market_ref": to_market_ref,
             "waybill_no": waybill_no, "quantity": _qty(quantity),
             "quality_grade": quality_grade, "owner_ref": owner_ref,
             "source_authorization": source_authorization,
             "splits": parts,
             "sacrifices": [dict(s) for s in (sacrifices or [])]},
            event_id=event_id, occurred_at=occurred_at)

    def update_transit(self, *, transfer_id: str, actor_ref: str, waybill_no: str,
                       location_ref: str, observed_at: datetime, quantity: float,
                       to_market_ref: str, event_id: str | None = None,
                       occurred_at: datetime | None = None) -> dict[str, Any]:
        return self._emit(
            "TRANSIT_UPDATED", "transit_leg", waybill_no,
            {"actor_ref": actor_ref, "transfer_id": transfer_id,
             "waybill_no": waybill_no, "location_ref": location_ref,
             "observed_at": observed_at.isoformat(), "quantity": _qty(quantity),
             "to_market_ref": to_market_ref},
            event_id=event_id, occurred_at=occurred_at)

    def settle_transfer(self, *, transfer_id: str, actor_ref: str, receiver_ref: str,
                        waybill_no: str, receipt_no: str,
                        received_quantity: float, received_quality_grade: str,
                        loss_reason: str, event_id: str | None = None,
                        occurred_at: datetime | None = None) -> dict[str, Any]:
        return self._emit(
            "TRANSFER_SETTLED", "transfer_order", transfer_id,
            {"actor_ref": actor_ref, "receiver_ref": receiver_ref,
             "waybill_no": waybill_no, "receipt_no": receipt_no,
             "received_quantity": _qty(received_quantity),
             "received_quality_grade": received_quality_grade,
             "loss_reason": loss_reason},
            event_id=event_id, occurred_at=occurred_at)

    def freeze_waybill(self, *, transfer_id: str, actor_ref: str, waybill_no: str,
                       reason: str, event_id: str | None = None,
                       occurred_at: datetime | None = None) -> dict[str, Any]:
        return self._emit(
            "WAYBILL_FROZEN", "transfer_order", transfer_id,
            {"actor_ref": actor_ref, "waybill_no": waybill_no, "reason": reason},
            event_id=event_id, occurred_at=occurred_at)

    def reconcile_waybill(self, *, transfer_id: str, actor_ref: str, waybill_no: str,
                          resolution: str, corrected_quantity: float | None = None,
                          corrected_to_market_ref: str | None = None,
                          event_id: str | None = None,
                          occurred_at: datetime | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"actor_ref": actor_ref, "waybill_no": waybill_no,
                                   "resolution": resolution}
        if resolution == "corrected":
            payload["corrected_quantity"] = _qty(corrected_quantity)
            payload["corrected_to_market_ref"] = corrected_to_market_ref
        return self._emit(
            "WAYBILL_RECONCILED", "transfer_order", transfer_id,
            payload, event_id=event_id, occurred_at=occurred_at)

    def issue_price_guidance(self, *, guidance_id: str, actor_ref: str, market_ref: str,
                             category: str, spec: str, guidance_price: float,
                             basis_receipt_ids: list[str], event_id: str | None = None,
                             occurred_at: datetime | None = None) -> dict[str, Any]:
        return self._emit(
            "PRICE_GUIDANCE_ISSUED", "price_guidance", guidance_id,
            {"actor_ref": actor_ref, "guidance_id": guidance_id,
             "market_ref": market_ref, "category": category, "spec": spec,
             "guidance_price": guidance_price,
             "basis_receipt_ids": list(basis_receipt_ids)},
            event_id=event_id, occurred_at=occurred_at)

    def request_price_exemption(self, *, request_id: str, actor_ref: str, market_ref: str,
                                category: str, spec: str, requester_ref: str, reason: str,
                                event_id: str | None = None,
                                occurred_at: datetime | None = None) -> dict[str, Any]:
        return self._emit(
            "PRICE_EXEMPTION_REQUESTED", "price_exemption", request_id,
            {"actor_ref": actor_ref, "request_id": request_id,
             "market_ref": market_ref, "category": category, "spec": spec,
             "requester_ref": requester_ref, "reason": reason},
            event_id=event_id, occurred_at=occurred_at)

    def approve_price_exemption(self, *, request_id: str, actor_ref: str,
                                approver_ref: str, event_id: str | None = None,
                                occurred_at: datetime | None = None) -> dict[str, Any]:
        return self._emit(
            "PRICE_EXEMPTION_APPROVED", "price_exemption", request_id,
            {"actor_ref": actor_ref, "request_id": request_id,
             "approver_ref": approver_ref},
            event_id=event_id, occurred_at=occurred_at)

    def report_store_gap(self, *, gap_id: str, actor_ref: str, market_ref: str,
                         store_ref: str, category: str, spec: str, quantity: float,
                         observed_at: datetime, event_id: str | None = None,
                         occurred_at: datetime | None = None) -> dict[str, Any]:
        return self._emit(
            "STORE_GAP_REPORTED", "store_gap", gap_id,
            {"actor_ref": actor_ref, "gap_id": gap_id, "market_ref": market_ref,
             "store_ref": store_ref, "category": category, "spec": spec,
             "quantity": _qty(quantity), "observed_at": observed_at.isoformat()},
            event_id=event_id, occurred_at=occurred_at)

    def advance_to(self, point_in_time: datetime) -> list[dict[str, Any]]:
        """推进模拟时钟；自动登记临期释放与节后释放事件。

        所有数量变动都在事件投影中完成，因此故障恢复重放同一事件流结果完全一致。
        """
        if point_in_time < self._clock:
            raise LedgerError("clock_rewind_not_allowed", "模拟时钟只能向前推进")
        start_clock = self._clock
        instants: list[datetime] = []
        for commitment in self._commitments.values():
            if commitment["status"] != "active" or commitment.get("release_after") is None:
                continue
            release_at = parse_ts(commitment["release_after"])
            if start_clock < release_at <= point_in_time:
                instants.append(release_at)
        for lot in self._lots.values():
            if lot.get("expired"):
                continue
            usable_until = parse_ts(lot["usable_until"])
            if usable_until <= point_in_time:
                instants.append(usable_until)
        produced: list[dict[str, Any]] = []
        # 逐时点推进：每个时点基于上一时点后的最新状态判定，避免同一承诺被两类释放重复计数。
        for instant in sorted(set(instants)):
            # 节后释放：到达承诺自己的 release_after 时点，锁货归还来源市场。
            for commitment in list(self._commitments.values()):
                if commitment["status"] != "active" or commitment.get("release_after") is None:
                    continue
                if parse_ts(commitment["release_after"]) != instant:
                    continue
                produced.append(self._emit(
                    "LOT_RELEASED", "supply_lot", commitment["lot_ref"],
                    {"actor_ref": _SYSTEM_ACTOR, "lot_ref": commitment["lot_ref"],
                     "release_reason": "post_holiday",
                     "quantity": _qty(commitment["quantity"]),
                     "commitment_id": commitment["id"]},
                    event_id=None, occurred_at=instant))
            for lot in list(self._lots.values()):
                if lot.get("expired") or parse_ts(lot["usable_until"]) != instant:
                    continue
                commitment_ids = [
                    cid for cid, commitment in self._commitments.items()
                    if commitment["lot_ref"] == lot["id"] and commitment["status"] == "active"
                ]
                qty = lot["free"] + sum(self._commitments[cid]["quantity"] for cid in commitment_ids)
                produced.append(self._emit(
                    "LOT_RELEASED", "supply_lot", lot["id"],
                    {"actor_ref": _SYSTEM_ACTOR, "lot_ref": lot["id"],
                     "release_reason": "expiring", "quantity": _qty(qty),
                     "commitment_ids": commitment_ids},
                    event_id=None, occurred_at=max(instant, start_clock)))
        self._clock = point_in_time
        return produced

    # --------------------------------------------------------------- 核心入口

    def record(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """校验并应用一个事件；同一 ``event_id`` 重传原样返回，不重复增减。"""
        duplicate = self._seen.get(event.get("event_id"))  # type: ignore[arg-type]
        if duplicate is not None:
            return duplicate
        issues = validate_event(event, self.schema)
        if issues:
            issue = issues[0]
            raise LedgerError("contract_violation", f"{issue.field}: {issue.message}")
        aggregate_key = (event["aggregate_type"], event["aggregate_id"])
        expected = self._versions.get(aggregate_key, 0) + 1
        if event["version"] != expected:
            raise LedgerError(
                "event_conflict",
                f"聚合 {event['aggregate_id']} 版本冲突：期望 v{expected}，收到 v{event['version']}")
        event = dict(event)
        event["payload"] = dict(event["payload"])
        getattr(self, "_check_" + event["event_type"].lower())(event)
        self._apply(event)
        self._versions[aggregate_key] = event["version"]
        self._events.append(event)
        self._seen[event["event_id"]] = event
        return event

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str,
              payload: dict[str, Any], *, event_id: str | None,
              occurred_at: datetime | None) -> dict[str, Any]:
        version = self._versions.get((aggregate_type, aggregate_id), 0) + 1
        event = _event(event_type, aggregate_type, aggregate_id, payload,
                       event_id=event_id, version=version,
                       occurred_at=occurred_at or self._clock)
        return self.record(event)

    # --------------------------------------------------------------- 规则校验

    def _actor(self, payload: Mapping[str, Any]) -> Actor:
        ref = payload["actor_ref"]
        actor = self.directory.get(ref)
        if actor is None:
            raise LedgerError("actor_unknown", f"未登记的操作人：{ref}")
        return actor

    @staticmethod
    def _require_role(actor: Actor, role: str) -> None:
        if role not in actor.roles:
            raise LedgerError("role_required", f"{actor.ref} 缺少角色 {role}")

    def _lot(self, lot_ref: str) -> dict[str, Any]:
        lot = self._lots.get(lot_ref)
        if lot is None:
            raise LedgerError("lot_unknown", f"货源批次不存在：{lot_ref}")
        return lot

    def _transfer(self, transfer_id: str) -> dict[str, Any]:
        order = self._transfers.get(transfer_id)
        if order is None:
            raise LedgerError("transfer_unknown", f"应急调拨单不存在：{transfer_id}")
        return order

    @staticmethod
    def _cleared(inspection: str) -> bool:
        return inspection in ("passed", "released")

    @staticmethod
    def _promised(lot: Mapping[str, Any]) -> float:
        """真正可分配的份额：在库、检验通过（含暂扣后解除）、未临期。"""
        if not Ledger._cleared(lot["inspection"]) or lot.get("expired"):
            return 0
        return lot["free"]

    def _leaf_orders(self, order: Mapping[str, Any]) -> list[dict[str, Any]]:
        """拆分子批次（若有）否则自身；父单不直接在途/签收。"""
        children = [self._transfers[cid] for cid in order.get("child_ids", ())]
        return children or [order]  # type: ignore[return-value]

    def _check_lot_declared(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        actor = self._actor(body)
        if actor.market_ref != body["origin_market_ref"]:
            raise LedgerError("actor_market_mismatch",
                              "货源必须由来源市场申报（来源市场保留处置权）")
        if body["quantity"] <= 0:
            raise LedgerError("non_positive_quantity", "申报数量必须为正")
        usable_until = body.get("usable_until")
        if not isinstance(usable_until, str):
            raise LedgerError("usable_until_required", "必须登记保鲜窗口截止时间")
        try:
            usable_deadline = parse_ts(usable_until)
        except ValueError:
            raise LedgerError("usable_until_invalid", "保鲜窗口时间格式无效") from None
        if usable_deadline <= parse_ts(event["occurred_at"]):
            raise LedgerError("usable_until_invalid", "保鲜窗口截止时间必须晚于申报时间")
        if event["aggregate_id"] in self._lots:
            raise LedgerError("lot_already_declared", "货源批次已申报")

    def _check_inspection_recorded(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        actor = self._actor(body)
        self._require_role(actor, "inspector")
        lot = self._lot(body["lot_ref"])
        if actor.market_ref != lot["origin_market_ref"]:
            raise LedgerError("actor_market_mismatch", "检验由来源市场登记")

    def _check_quantity_reserved(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        actor = self._actor(body)
        lot = self._lot(body["lot_ref"])
        if actor.market_ref != lot["origin_market_ref"]:
            raise LedgerError("source_disposal_required",
                              "只有来源市场可以把货源锁给采购承诺")
        if lot["inspection"] == "detained":
            raise LedgerError("lot_detained", "检验暂扣的货源不能锁定给采购承诺")
        if not self._cleared(lot["inspection"]):
            raise LedgerError("inspection_not_passed",
                              "检验尚未通过（或已销毁）的货源不能对外承诺")
        if lot.get("expired"):
            raise LedgerError("lot_expired", "已过保鲜窗口的货源不能锁定")
        if body["quantity"] <= 0:
            raise LedgerError("non_positive_quantity", "锁定数量必须为正")
        # 并发订单只能锁定真正可分配的份额：顺序提交，超出即拒绝，绝不超卖。
        if body["quantity"] > self._promised(lot) + _EPS:
            raise LedgerError(
                "insufficient_quantity",
                f"可分配份额不足：申请 {body['quantity']}，仅剩 {self._promised(lot)}")
        if event["aggregate_id"] in self._commitments:
            raise LedgerError("commitment_exists", "采购承诺标识已存在")

    def _normalized_splits(self, body: Mapping[str, Any]) -> list[dict[str, Any]]:
        parts = body["splits"] or [{
            "transfer_id": body.get("_self_id"), "quantity": body["quantity"],
            "to_market_ref": body["to_market_ref"], "waybill_no": body["waybill_no"],
        }]
        normalized: list[dict[str, Any]] = []
        for index, part in enumerate(parts):
            normalized.append({
                "transfer_id": part.get("transfer_id") or f"{body['_self_id']}#p{index + 1}",
                "quantity": part["quantity"],
                "to_market_ref": part.get("to_market_ref", body["to_market_ref"]),
                "waybill_no": part.get("waybill_no", body["waybill_no"]),
            })
        return normalized

    def _check_emergency_transfer_created(self, event: Mapping[str, Any]) -> None:
        body = dict(event["payload"])
        body["_self_id"] = event["aggregate_id"]
        actor = self._actor(body)
        lot = self._lot(body["lot_ref"])
        if body["from_market_ref"] == body["to_market_ref"]:
            raise LedgerError("same_market_transfer", "应急调拨的收发货市场不能相同")
        if actor.market_ref != body["from_market_ref"]:
            raise LedgerError("actor_market_mismatch", "应急调拨必须由调出方市场操作")
        if body["from_market_ref"] != lot["origin_market_ref"]:
            raise LedgerError("source_disposal_required",
                              "来源市场保留处置权：只能从货源所属市场调出")
        if not str(body.get("source_authorization", "")).strip():
            raise LedgerError("source_authorization_required",
                              "应急调拨必须携带来源市场处置授权凭据")
        if not self._cleared(lot["inspection"]):
            raise LedgerError("inspection_not_passed", "检验未通过/暂扣的货源不能调出")
        if lot.get("expired"):
            raise LedgerError("lot_expired", "已过保鲜窗口的货源不能调出")
        qty = body["quantity"]
        if qty <= 0:
            raise LedgerError("non_positive_quantity", "调拨数量必须为正")
        if event["aggregate_id"] in self._transfers:
            raise LedgerError("transfer_exists", "调拨单标识已存在")

        # 拆分守恒：总量、品质等级、责任归属逐项核对。
        parts = self._normalized_splits(body)
        split_total = 0.0
        waybills_seen: set[str] = set()
        for part in parts:
            if part["transfer_id"] != event["aggregate_id"] and part["transfer_id"] in self._transfers:
                raise LedgerError("split_exists", f"拆分子批次已存在：{part['transfer_id']}")
            part_qty = part["quantity"]
            if not isinstance(part_qty, (int, float)) or isinstance(part_qty, bool) or part_qty <= 0:
                raise LedgerError("split_quantity_mismatch", "拆分数量必须为正数")
            if part["to_market_ref"] == body["from_market_ref"]:
                raise LedgerError("same_market_transfer", "拆分子批次去向不能是调出市场")
            if part["waybill_no"] in waybills_seen:
                raise LedgerError("split_waybill_duplicate",
                                  "同一调拨单内运单号不得重复")
            waybills_seen.add(part["waybill_no"])
            split_total += part_qty
        if abs(split_total - qty) > _EPS:
            raise LedgerError(
                "split_quantity_mismatch",
                f"拆分总量 {split_total} 与调拨总量 {qty} 不符（总量守恒）")
        # 品质等级与责任归属在父单上固定，子批次投影时继承（品质/责任守恒）。

        # 被牺牲的采购承诺必须显式点名并给出让出量；不允许悄悄挪用已锁货源。
        # 支持部分牺牲：一条承诺可以只让出一部分，余额继续锁定。
        sacrifice_items = self._validated_sacrifices(body, lot, qty)
        sacrificed = sum(item["quantity"] for item in sacrifice_items)
        available = self._promised(lot) + sacrificed
        if qty > available + _EPS:
            raise LedgerError(
                "insufficient_quantity",
                f"可调拨量不足：需要 {qty}，可分配 {self._promised(lot)}，"
                f"已显式牺牲承诺 {sacrificed}；挪用承诺必须点名登记")

    def _validated_sacrifices(self, body: Mapping[str, Any], lot: Mapping[str, Any],
                              qty: float) -> list[dict[str, Any]]:
        raw_items = body["sacrifices"]
        if not isinstance(raw_items, list):
            raise LedgerError("sacrifices_invalid", "牺牲承诺明细必须是列表")
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise LedgerError("sacrifices_invalid", "每条牺牲明细必须是对象")
            commitment_id = item.get("commitment_id")
            take = item.get("quantity")
            if not isinstance(commitment_id, str) or not commitment_id.strip():
                raise LedgerError("sacrifices_invalid", "牺牲明细缺少采购承诺标识")
            if commitment_id in seen:
                raise LedgerError("sacrifices_invalid",
                                  f"采购承诺 {commitment_id} 在同一调拨中重复登记")
            seen.add(commitment_id)
            commitment = self._commitments.get(commitment_id)
            if commitment is None:
                raise LedgerError("commitment_unknown", f"采购承诺不存在：{commitment_id}")
            if commitment["lot_ref"] != lot["id"]:
                raise LedgerError("commitment_lot_mismatch",
                                  f"采购承诺 {commitment_id} 不属于该货源批次")
            if commitment["status"] != "active":
                raise LedgerError("commitment_not_active",
                                  f"采购承诺 {commitment_id} 已不可挪用")
            if isinstance(take, bool) or not isinstance(take, (int, float)) or take <= 0:
                raise LedgerError("sacrifice_quantity_invalid",
                                  f"承诺 {commitment_id} 的让出量必须为正数")
            if take > commitment["quantity"] + _EPS:
                raise LedgerError(
                    "sacrifice_exceeds_commitment",
                    f"承诺 {commitment_id} 让出 {take} 超过锁定量 {commitment['quantity']}")
            items.append({"commitment_id": commitment_id, "quantity": float(take)})
        sacrificed = sum(item["quantity"] for item in items)
        if sacrificed > qty + _EPS:
            raise LedgerError(
                "sacrifice_exceeds_transfer",
                f"点名牺牲的承诺 {sacrificed} 超过调拨总量 {qty}")
        return items

    def _check_transit_updated(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        actor = self._actor(body)
        order = self._transfer(body["transfer_id"])
        leaf = self._leaf_orders(order)
        if not any(part["status"] == "in_transit" and part["waybill_no"] == body["waybill_no"]
                   for part in leaf):
            raise LedgerError("waybill_mismatch",
                              "运单不属于该调拨单的在途批次，或批次已签收")
        if actor.market_ref not in (order["from_market_ref"], body["to_market_ref"]):
            raise LedgerError("actor_market_mismatch", "只有收发货双方可以上报在途节点")
        waybill = self._waybills[body["waybill_no"]]
        if waybill["frozen"]:
            raise LedgerError("waybill_frozen",
                              f"运单 {body['waybill_no']} 已冻结核对，须先由来源市场核对解冻")
        # 数量或去向与首次登记不一致即冻结（在途改道/改量的受控入口）。
        identity = (body["quantity"], body["to_market_ref"])
        if identity != (waybill["quantity"], waybill["to_market_ref"]):
            self._auto_freeze(body["transfer_id"], body["waybill_no"],
                              "运单号相同而数量或去向不同，需核对",
                              pending_quantity=body["quantity"],
                              pending_to_market=body["to_market_ref"])
            raise LedgerError(
                "waybill_frozen",
                f"运单 {body['waybill_no']} 数量/去向与登记不符，已自动冻结核对")

    def _check_transfer_settled(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        actor = self._actor(body)
        order = self._transfer(event["aggregate_id"])
        if order.get("child_ids"):
            raise LedgerError("settle_split_required", "父单不直接签收，请逐个子批次确认到货")
        if order["status"] != "in_transit":
            raise LedgerError("already_settled", "调拨单已签收，禁止重复结算")
        if actor.market_ref != order["to_market_ref"]:
            raise LedgerError("receiver_must_confirm", "只有接收市场可以确认实际到货")
        if self._waybills.get(body["waybill_no"], {}).get("frozen"):
            raise LedgerError("waybill_frozen", "运单处于冻结状态，核对完成前不得签收")
        if body["waybill_no"] != order["waybill_no"]:
            raise LedgerError("waybill_mismatch", "签收运单号与调拨批次不一致")
        received = body["received_quantity"]
        if received < 0 or received > order["quantity"] + _EPS:
            raise LedgerError("settlement_out_of_range",
                              f"签收量 {received} 超出发运量 {order['quantity']}")
        if not str(body.get("receipt_no", "")).strip():
            raise LedgerError("receipt_required", "签收必须携带物流回执号")
        if body["receipt_no"] in {r["receipt_no"] for r in self._receipts}:
            raise LedgerError("duplicate_receipt", "物流回执已用于签收，禁止重复增减库存")
        if body["received_quality_grade"] != order["quality_grade"]:
            raise LedgerError("grade_mismatch_at_receipt",
                              "到货品级与发运不一致，不得直接入库，须进入异常处理")
        if received < order["quantity"] - _EPS and not str(body.get("loss_reason", "")).strip():
            raise LedgerError("loss_reason_required", "出现损耗必须登记损耗原因")

    def _check_waybill_frozen(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        self._actor(body)
        self._transfer(event["aggregate_id"])
        if body["waybill_no"] in self._waybills and self._waybills[body["waybill_no"]]["frozen"]:
            raise LedgerError("waybill_already_frozen", "运单已处于冻结状态")

    def _check_waybill_reconciled(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        actor = self._actor(body)
        order = self._transfer(event["aggregate_id"])
        if actor.market_ref != order["from_market_ref"]:
            raise LedgerError("source_disposal_required", "运单核对由来源市场（处置方）执行")
        waybill = self._waybills.get(body["waybill_no"])
        if waybill is None or not waybill.get("frozen"):
            raise LedgerError("waybill_not_frozen", "运单未处于冻结状态，无需核对")
        if body["resolution"] == "corrected":
            new_quantity = body.get("corrected_quantity")
            new_to = body.get("corrected_to_market_ref")
            if isinstance(new_quantity, bool) or not isinstance(new_quantity, (int, float)) \
                    or new_quantity <= 0:
                raise LedgerError("non_positive_quantity", "核对后的运单数量必须为正数")
            if not isinstance(new_to, str) or not new_to.strip():
                raise LedgerError("to_market_required", "核对后的去向市场必填")
            if new_to == order["from_market_ref"]:
                raise LedgerError("same_market_transfer", "去向不能是调出市场")
            lot = self._lots[order["lot_ref"]]
            delta = new_quantity - waybill["quantity"]
            if delta > 0 and delta > self._promised(lot) + _EPS:
                raise LedgerError("insufficient_quantity",
                                  "核对增加的数量在来源市场无可用可分配份额")

    def _check_lot_released(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        actor = self._actor(body)
        lot = self._lot(body["lot_ref"])
        if actor.ref != _SYSTEM_ACTOR and actor.market_ref != lot["origin_market_ref"]:
            raise LedgerError("source_disposal_required", "只有来源市场可以释放自有货源")

    def _check_price_guidance_issued(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        actor = self._actor(body)
        self._require_role(actor, "price_monitor")
        if actor.market_ref != body["market_ref"]:
            raise LedgerError("actor_market_mismatch", "指导价由所属市场的价格监测人员发布")
        if not body["basis_receipt_ids"]:
            raise LedgerError("basis_required", "指导价必须至少依据一条已到货签收事实")
        price = body["guidance_price"]
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
            raise LedgerError("price_invalid", "指导价必须是非负数值")
        known = {r["receipt_no"]: r for r in self._receipts}
        for receipt_no in body["basis_receipt_ids"]:
            receipt = known.get(receipt_no)
            if receipt is None:
                raise LedgerError("receipt_not_found_for_price",
                                  f"指导价依据 {receipt_no} 不是已签收的到货事实")
            if receipt["to_market_ref"] != body["market_ref"]:
                raise LedgerError("receipt_market_mismatch",
                                  f"回执 {receipt_no} 不属于市场 {body['market_ref']}")
            if (receipt["category"], receipt["spec"]) != (body["category"], body["spec"]):
                raise LedgerError("receipt_category_mismatch",
                                  f"回执 {receipt_no} 的品类规格与指导价口径不一致")
        if event["aggregate_id"] in self._guidance:
            raise LedgerError("guidance_exists", "指导价标识已存在")

    def _check_price_exemption_requested(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        self._actor(body)
        if not str(body.get("requester_ref", "")).strip():
            raise LedgerError("requester_required", "豁免申请必须登记申请人")
        if event["aggregate_id"] in self._exemptions:
            raise LedgerError("exemption_exists", "豁免申请标识已存在")

    def _check_price_exemption_approved(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        actor = self._actor(body)
        self._require_role(actor, "approver")
        request = self._exemptions.get(event["aggregate_id"])
        if request is None:
            raise LedgerError("exemption_not_found", "豁免申请不存在")
        if request["status"] != "requested":
            raise LedgerError("exemption_already_decided", "豁免申请已有决定，不得重复审批")
        if body["approver_ref"] != actor.ref:
            raise LedgerError("approver_mismatch", "批准人必须是当前操作人")
        if body["approver_ref"] != actor.ref:
            raise LedgerError("approver_mismatch", "批准人必须是当前操作人")
        # 承担价格监测的人不能批准自己提出的异常豁免（即使同时具备审批角色）。
        if body["approver_ref"] == request["requester_ref"]:
            raise LedgerError("self_approval_forbidden", "申请人不得批准自己提出的异常豁免")

    def _check_store_gap_reported(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        actor = self._actor(body)
        if actor.market_ref != body["market_ref"]:
            raise LedgerError("actor_market_mismatch", "门店缺口由所属市场上报")
        if body["quantity"] <= 0:
            raise LedgerError("non_positive_quantity", "缺口数量必须为正")

    def _auto_freeze(self, transfer_id: str, waybill_no: str, reason: str, *,
                     pending_quantity: float, pending_to_market: str) -> None:
        """冲突在同一提交流程内生成 WAYBILL_FROZEN 事件（冲突事件本身仍被拒绝）。"""
        version = self._versions.get(("transfer_order", transfer_id), 0) + 1
        frozen = _event("WAYBILL_FROZEN", "transfer_order", transfer_id,
                        {"actor_ref": _SYSTEM_ACTOR, "waybill_no": waybill_no,
                         "reason": reason,
                         "pending_quantity": _qty(pending_quantity),
                         "pending_to_market_ref": pending_to_market},
                        event_id=None, version=version, occurred_at=self._clock)
        self._apply(frozen)
        self._versions[("transfer_order", transfer_id)] = version
        self._events.append(frozen)
        self._seen[frozen["event_id"]] = frozen

    # ----------------------------------------------------------------- 投影

    def _apply(self, event: Mapping[str, Any]) -> None:
        getattr(self, "_apply_" + event["event_type"].lower())(event)

    def _apply_lot_declared(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        self._lots[event["aggregate_id"]] = {
            "id": event["aggregate_id"], "category": body["category"], "spec": body["spec"],
            "origin_market_ref": body["origin_market_ref"], "owner_ref": body["owner_ref"],
            "declared": body["quantity"], "free": float(body["quantity"]),
            "reserved": 0.0, "shipped": 0.0, "released": 0.0,
            "quality_grade": body["quality_grade"], "usable_until": body["usable_until"],
            "inspection": "pending", "expired": False,
            "declared_at": event["occurred_at"],
        }

    def _apply_inspection_recorded(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        lot = self._lots[event["aggregate_id"]]
        lot["inspection"] = body["status"]
        if body["status"] == "condemned":
            # 销毁：在库与锁定数量全部退出可分配，锁定承诺随之破裂；已发运货不受影响。
            qty = lot["free"]
            for commitment in self._commitments.values():
                if commitment["lot_ref"] == lot["id"] and commitment["status"] == "active":
                    qty += commitment["quantity"]
                    lot["reserved"] -= commitment["quantity"]
                    commitment["status"] = "broken_by_condemnation"
            lot["free"] = 0.0
            lot["released"] += qty
            self._assert_lot_balanced(lot)

    def _apply_quantity_reserved(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        lot = self._lots[body["lot_ref"]]
        lot["free"] -= body["quantity"]
        lot["reserved"] += body["quantity"]
        self._commitments[event["aggregate_id"]] = {
            "id": event["aggregate_id"], "lot_ref": body["lot_ref"],
            "market_ref": body["market_ref"], "quantity": float(body["quantity"]),
            "status": "active", "release_after": body.get("release_after"),
        }
        self._assert_lot_balanced(lot)

    def _apply_emergency_transfer_created(self, event: Mapping[str, Any]) -> None:
        raw = dict(event["payload"])
        raw["_self_id"] = event["aggregate_id"]
        parts = self._normalized_splits(raw)
        lot = self._lots[raw["lot_ref"]]

        sacrificed: list[dict[str, Any]] = []
        sacrificed_total = 0.0
        for item in raw["sacrifices"]:
            commitment = self._commitments[item["commitment_id"]]
            take = float(item["quantity"])
            lot["reserved"] -= take
            commitment["quantity"] -= take
            if commitment["quantity"] <= _EPS:
                commitment["quantity"] = 0.0
                commitment["status"] = "sacrificed"
            else:
                # 部分牺牲：余额继续锁给原采购承诺，节后释放规则照常适用。
                commitment["status"] = "active"
            commitment.setdefault("sacrificed_to", [])
            commitment["sacrificed_to"].append(
                {"transfer_id": event["aggregate_id"], "quantity": take})
            sacrificed.append({"commitment_id": commitment["id"], "quantity": take,
                               "market_ref": commitment["market_ref"]})
            sacrificed_total += take
        # 守恒：牺牲量从已锁份额出，其余从在库可分配出。
        lot["free"] -= raw["quantity"] - sacrificed_total
        lot["shipped"] += raw["quantity"]

        def _base_order() -> dict[str, Any]:
            return {
                "lot_ref": lot["id"], "from_market_ref": raw["from_market_ref"],
                "quality_grade": raw["quality_grade"], "owner_ref": raw["owner_ref"],
                "source_authorization": raw["source_authorization"],
                "parent_id": None, "child_ids": [], "splits": [],
                "sacrificed": sacrificed, "status": "in_transit",
                "received": 0.0, "loss": 0.0, "loss_reason": None,
                "receipt_no": None, "created_at": event["occurred_at"],
            }

        parent = _base_order()
        parent.update({
            "id": event["aggregate_id"],
            "to_market_ref": raw["to_market_ref"], "waybill_no": raw["waybill_no"],
            "quantity": float(raw["quantity"]), "splits": parts,
        })
        self._transfers[event["aggregate_id"]] = parent
        for part in parts:
            self._register_waybill(part["waybill_no"], part["transfer_id"],
                                   part["quantity"], part["to_market_ref"])
            if part["transfer_id"] == event["aggregate_id"]:
                continue
            child = _base_order()
            child.update({
                "id": part["transfer_id"], "parent_id": event["aggregate_id"],
                "to_market_ref": part["to_market_ref"], "waybill_no": part["waybill_no"],
                "quantity": float(part["quantity"]),
            })
            # 责任归属与品质等级随父单继承（子投影不再单列，取自同一字段值）。
            self._transfers[part["transfer_id"]] = child
            parent["child_ids"].append(part["transfer_id"])
        self._assert_lot_balanced(lot)

    def _register_waybill(self, waybill_no: str, transfer_id: str,
                          quantity: float, to_market_ref: str) -> None:
        self._waybills[waybill_no] = {
            "waybill_no": waybill_no, "transfer_id": transfer_id,
            "quantity": float(quantity), "to_market_ref": to_market_ref,
            "frozen": False, "reconciled": False,
            "pending_quantity": None, "pending_to_market_ref": None,
        }

    def _apply_transit_updated(self, event: Mapping[str, Any]) -> None:
        # 在途节点只更新位置/观察时间，绝不重复增减库存。
        order = self._transfers[event["payload"]["transfer_id"]]
        order["last_position"] = event["payload"]["location_ref"]
        order["last_observed_at"] = event["payload"]["observed_at"]

    def _apply_transfer_settled(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        order = self._transfers[event["aggregate_id"]]
        order["status"] = "settled"
        order["received"] = float(body["received_quantity"])
        order["loss"] = order["quantity"] - order["received"]
        order["loss_reason"] = body["loss_reason"]
        order["receipt_no"] = body["receipt_no"]
        order["settled_at"] = event["occurred_at"]
        lot = self._lots[order["lot_ref"]]
        self._receipts.append({
            "receipt_no": body["receipt_no"], "transfer_id": order["id"],
            "lot_ref": order["lot_ref"], "category": lot["category"], "spec": lot["spec"],
            "from_market_ref": order["from_market_ref"],
            "to_market_ref": order["to_market_ref"],
            "quantity": order["quantity"], "received_quantity": order["received"],
            "quality_grade": body["received_quality_grade"],
            "settled_at": event["occurred_at"],
        })
        self._refresh_parent(order)
        self._assert_lot_balanced(lot)
        self._assert_settlement_balanced(order)

    def _refresh_parent(self, child: Mapping[str, Any]) -> None:
        parent_id = child.get("parent_id")
        if parent_id is None:
            return
        parent = self._transfers[parent_id]
        children = [self._transfers[cid] for cid in parent["child_ids"]]
        parent["quantity"] = sum(c["quantity"] for c in children)
        parent["received"] = sum(c["received"] for c in children)
        parent["loss"] = sum(c["loss"] for c in children)
        if all(c["status"] == "settled" for c in children):
            parent["status"] = "settled"

    def _apply_waybill_frozen(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        waybill = self._waybills[body["waybill_no"]]
        waybill["frozen"] = True
        waybill["pending_quantity"] = body.get("pending_quantity")
        waybill["pending_to_market_ref"] = body.get("pending_to_market_ref")

    def _apply_waybill_reconciled(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        waybill = self._waybills[body["waybill_no"]]
        waybill["frozen"] = False
        waybill["reconciled"] = True
        if body["resolution"] == "corrected":
            new_quantity = float(body["corrected_quantity"])
            new_to = body["corrected_to_market_ref"]
            order = self._transfers[event["aggregate_id"]]
            lot = self._lots[order["lot_ref"]]
            delta = new_quantity - waybill["quantity"]
            # 数量更正同步修正来源市场账目：增加量从在库出，减少量退回在库。
            lot["free"] -= delta
            lot["shipped"] += delta
            waybill["quantity"] = new_quantity
            waybill["to_market_ref"] = new_to
            order["quantity"] = new_quantity
            order["to_market_ref"] = new_to
            self._refresh_parent(order)
            self._assert_lot_balanced(lot)
        waybill["pending_quantity"] = None
        waybill["pending_to_market_ref"] = None

    def _apply_lot_released(self, event: Mapping[str, Any]) -> None:
        body = event["payload"]
        lot = self._lots[body["lot_ref"]]
        if body["release_reason"] == "post_holiday":
            # 节后释放：指定采购承诺的锁定量归还来源市场在库。
            commitment = self._commitments[body["commitment_id"]]
            qty = commitment["quantity"]
            lot["reserved"] -= qty
            lot["free"] += qty
            commitment["status"] = "released_post_holiday"
        else:
            # 临期释放：在库可分配 + 仍锁定的承诺一并退出可分配，交还来源市场处置。
            qty = lot["free"]
            for commitment_id in body.get("commitment_ids", ()):
                commitment = self._commitments[commitment_id]
                qty += commitment["quantity"]
                lot["reserved"] -= commitment["quantity"]
                commitment["quantity"] = 0.0
                commitment["status"] = "broken_due_expiry"
            lot["free"] = 0.0
            lot["released"] += qty
            lot["expired"] = True
        self._assert_lot_balanced(lot)

    def _apply_price_guidance_issued(self, event: Mapping[str, Any]) -> None:
        self._guidance[event["aggregate_id"]] = {
            "id": event["aggregate_id"], **event["payload"],
            "issued_at": event["occurred_at"]}

    def _apply_price_exemption_requested(self, event: Mapping[str, Any]) -> None:
        self._exemptions[event["aggregate_id"]] = {
            "id": event["aggregate_id"], **event["payload"], "status": "requested"}

    def _apply_price_exemption_approved(self, event: Mapping[str, Any]) -> None:
        self._exemptions[event["aggregate_id"]]["status"] = "approved"
        self._exemptions[event["aggregate_id"]]["approver_ref"] = event["payload"]["approver_ref"]

    def _apply_store_gap_reported(self, event: Mapping[str, Any]) -> None:
        self._gaps.append({"id": event["aggregate_id"], **event["payload"]})

    # ------------------------------------------------------------- 守恒断言

    def _assert_lot_balanced(self, lot: Mapping[str, Any]) -> None:
        total = lot["free"] + lot["reserved"] + lot["shipped"] + lot["released"]
        if abs(total - lot["declared"]) > 1e-6:
            raise LedgerError("balance_broken",
                              f"批次 {lot['id']} 数量不守恒：{total} != {lot['declared']}")

    def _assert_settlement_balanced(self, order: Mapping[str, Any]) -> None:
        if abs(order["received"] + order["loss"] - order["quantity"]) > 1e-6:
            raise LedgerError("balance_broken",
                              f"调拨 {order['id']} 签收+损耗 != 发运量")

    # ----------------------------------------------------------------- 解释

    def availability(self, market_ref: str, category: str | None = None,
                     spec: str | None = None) -> dict[str, Any]:
        """真实可售口径：只有在库检验通过未临期货 + 已签收到货才算可售。"""
        def _match(lot: Mapping[str, Any]) -> bool:
            return ((category is None or lot["category"] == category)
                    and (spec is None or lot["spec"] == spec))

        on_sale = 0.0
        declared_at_source = 0.0
        in_transit_in = 0.0
        detained = 0.0
        condemned = 0.0
        reserved = 0.0
        expiring = 0.0
        for lot in self._lots.values():
            if lot["origin_market_ref"] != market_ref or not _match(lot):
                continue
            declared_at_source += lot["declared"]
            if self._cleared(lot["inspection"]) and not lot["expired"]:
                on_sale += lot["free"]
                reserved += lot["reserved"]
            if lot["inspection"] == "detained":
                detained += lot["free"] + lot["reserved"]
            if lot["inspection"] == "condemned":
                condemned += lot["released"]
            if lot["expired"]:
                expiring += lot["released"]
        # 在途：只数叶子批次，父子拆分不重复计。
        for order in self._transfers.values():
            if order.get("parent_id") or order["status"] != "in_transit":
                continue
            if order.get("child_ids"):
                leaves = [self._transfers[cid] for cid in order["child_ids"]]
            else:
                leaves = [order]
            for leaf in leaves:
                lot = self._lots[leaf["lot_ref"]]
                if leaf["to_market_ref"] == market_ref and _match(lot):
                    in_transit_in += leaf["quantity"]
        received_in = 0.0
        for receipt in self._receipts:
            if receipt["to_market_ref"] != market_ref or not _match(receipt):
                continue
            received_in += receipt["received_quantity"]
        return {
            "market_ref": market_ref, "category": category, "spec": spec,
            "on_sale": on_sale + received_in,
            "declared_at_source": declared_at_source,
            "on_sale_in_warehouse": on_sale,
            "locked_by_commitments": reserved, "in_transit_inbound": in_transit_in,
            "detained": detained, "condemned": condemned,
            "released_expiring": expiring, "received_on_record": received_in,
        }

    def explain_market(self, market_ref: str, category: str | None = None,
                       spec: str | None = None) -> dict[str, Any]:
        """回答：某市场为何缺货或仍有余量。"""
        avail = self.availability(market_ref, category, spec)
        reasons: list[str] = []
        gaps = [g for g in self._gaps if g["market_ref"] == market_ref
                and (category is None or g["category"] == category)
                and (spec is None or g["spec"] == spec)]
        gap_qty = sum(g["quantity"] for g in gaps)
        if avail["detained"]:
            reasons.append(f"检验暂扣 {avail['detained']}，暂扣货不计可售")
        if avail["condemned"]:
            reasons.append(f"检验销毁 {avail['condemned']}，已退出可分配")
        if avail["in_transit_inbound"]:
            reasons.append(f"在途尚未到货 {avail['in_transit_inbound']}，不计当前可售")
        if avail["locked_by_commitments"]:
            reasons.append(f"已被采购承诺锁定 {avail['locked_by_commitments']}，不能再承诺给他人")
        if avail["released_expiring"]:
            reasons.append(f"超过保鲜窗口已释放 {avail['released_expiring']}")
        if gap_qty > avail["on_sale"]:
            reasons.append(f"门店缺口合计 {gap_qty}，大于当前可售 {avail['on_sale']}，因此缺货")
        elif avail["on_sale"] > 0:
            reasons.append(f"在库可售加已到货 {avail['on_sale']}，仍有余量")
        else:
            reasons.append("当前没有可售库存")
        return {"availability": avail, "store_gaps": gaps, "reasons": reasons}

    def explain_transfer(self, transfer_id: str) -> dict[str, Any]:
        """回答：某次调拨牺牲了哪些承诺、拆分如何守恒、到货与损耗如何。"""
        order = self._transfer(transfer_id)
        leaves = self._leaf_orders(order)
        children = [c for c in leaves if c["id"] != order["id"]]
        frozen_waybills = [c["waybill_no"] for c in leaves
                           if self._waybills[c["waybill_no"]]["frozen"]]
        return {
            "transfer_id": order["id"], "lot_ref": order["lot_ref"],
            "from_market_ref": order["from_market_ref"],
            "to_market_ref": order["to_market_ref"],
            "quantity": order["quantity"], "quality_grade": order["quality_grade"],
            "owner_ref": order["owner_ref"], "status": order["status"],
            "source_authorization": order["source_authorization"],
            "splits": [{"transfer_id": c["id"], "quantity": c["quantity"],
                        "to_market_ref": c["to_market_ref"],
                        "quality_grade": c["quality_grade"],
                        "owner_ref": c["owner_ref"],
                        "status": c["status"]} for c in children],
            "sacrificed_commitments": order["sacrificed"],
            "received_quantity": order["received"], "loss_quantity": order["loss"],
            "loss_reason": (order["loss_reason"]
                            or next((c["loss_reason"] for c in children if c["loss_reason"]), None)),
            "receipt_no": order["receipt_no"],
            "frozen_waybills": frozen_waybills,
        }

    def explain_price(self, guidance_id: str) -> dict[str, Any]:
        """回答：价格判断采用了哪些已到货事实。"""
        guidance = self._guidance.get(guidance_id)
        if guidance is None:
            raise LedgerError("guidance_unknown", f"指导价不存在：{guidance_id}")
        known = {r["receipt_no"]: r for r in self._receipts}
        return {
            "guidance": guidance,
            "basis_receipts": [known[r] for r in guidance["basis_receipt_ids"]],
            "note": "指导价仅依据已签收的到货回执；在途、暂扣、已锁未到货数量均不参与",
        }
