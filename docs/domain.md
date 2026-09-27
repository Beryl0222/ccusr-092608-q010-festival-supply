# 领域约定

跨市场节日保供调拨账：中秋与国庆双节叠加时，蔬菜、水果、返港海鲜同时进入高峰，
调度员每天处理产地装车、在途改道、检验暂扣、采购锁货与门店缺口。账本以
**品类规格、货源批次、所有权、在途节点、检验状态、保鲜窗口、采购承诺、
到货签收、损耗、指导价依据、应急调拨** 为核心。

## 聚合

- `supply_lot`：货源批次（品类规格、数量、来源市场、品质等级、保鲜窗口、检验状态）。
- `market_gap`：门店缺口申报（市场 × 品类规格）。
- `purchase_commitment`：采购锁货承诺，带节后释放时间。
- `transit_leg`：运单在途节点（由运单号关联调拨单）。
- `transfer_order`：调拨单；应急调拨的父单为计划额度（`is_plan`），可拆分多张子单。
- `price_exemption`：异常价格豁免申请与批准。
- `price_assessment`：指导价判断，只引用已到货签收凭据。

## 事件

| 事件 | 聚合 | 语义 |
| --- | --- | --- |
| `LOT_DECLARED` | supply_lot | 产地申报货源，只是“已组织货源”，不自动可售 |
| `GAP_DECLARED` | market_gap | 门店缺口变化 |
| `INSPECTION_RECORDED` | supply_lot | 检验 `passed`/`detained`/`released` |
| `QUANTITY_RESERVED` | purchase_commitment | 来源市场锁定份额给某采购市场 |
| `TRANSFER_ORDERED` | transfer_order | 发起调拨；`is_plan` 为应急父额度，子单承运 |
| `TRANSIT_UPDATED` | transit_leg | `in_transit`/`arrived`/`diverted`（改道需新去向） |
| `ARRIVAL_CONFIRMED` | transfer_order | 接收市场按实际数量签收，产生物流回执 |
| `TRANSFER_SETTLED` | transfer_order | 到货后结算；有损耗必须填原因 |
| `EXEMPTION_REQUESTED` / `EXEMPTION_APPROVED` | price_exemption | 异常豁免申请与批准 |
| `GUIDANCE_PRICE_ASSESSED` | price_assessment | 基于已到货事实给出指导价 |

所有发生时间都必须携带时区；版本号在同一聚合内从 1 开始连续递增；
基础信封校验不会改写调用方输入。

## 业务守恒规则（由 `src/festival_supply/ledger.py` 执行）

1. **已组织货源 ≠ 可售**：只有检验放行、未锁定、未出库且在保鲜窗口内的份额
   （`allocatable`）才能被承诺或调拨；在途、暂扣、临期过期数量不混入可售判断。
2. **一车货不能重复承诺**：锁货与发运都只能扣减真正可分配份额；并发订单
   按事件顺序重放，超份额报 `insufficient_share`。
3. **来源市场保留处置权**：锁货、发起调拨的行为人必须是批次来源市场；
   接收市场只能确认实际到货（`receiver_only`），不能替对方签收。
4. **职责分离**：承担价格监测的人（豁免申请人）不能批准自己提出的异常豁免
   （`self_approval`）。
5. **应急调拨守恒**：父单只授权额度（不占库存、不承运）；子单分拆的
   **总量不得超过父单**（`split_total_exceeded`）、品质等级必须与批次一致
   （`grade_mismatch`，不得借分拆降级）、责任归属必须与父单一致。
6. **物流回执幂等**：相同 `receipt_no` 重传不重复增减库存；结算事件重放同样幂等，
   故障恢复后不会重复发货或结算。
7. **运单一致性**：运单号重复使用报 `waybill_duplicate`；同号但数量或去向不同
   立即冻结（`waybill_frozen`），系统自动追加持久化的 `WAYBILL_FROZEN` 事件，
   重放后冻结不丢失；冻结期间禁止在途更新与签收，本仓库只记录冻结、不自动放行，
   解冻须人工核对。
8. **在途改道**：`TRANSIT_UPDATED` 的 `diverted` 改变去向，并在调拨解释中
   保留原始去向（`diverted_from`）。
9. **牺牲可解释**：调拨超出当前可分配份额时，必须显式声明牺牲哪些采购承诺，
   系统按顺序核销并记录到 `explain_transfer`；声明不足报 `sacrifices_insufficient`。
10. **价格只用已到货事实**：指导价判断引用的每条凭据都必须是已签收回执
    （`evidence_not_arrived`），在途货量不进入价格稳定判断。
11. **模拟日期**：`Ledger.view(as_of=...)` 按 `occurred_at` 过滤重放，可检验
    在途、临期（保鲜窗口前 24 小时标记 `near_expiry`）与节后承诺自动释放。

## 解释性查询

- `explain_market(market, as_of)`：按品类列出在库、可售、锁定、暂扣、在途、
  到货待结算、已到货、临期/过期与缺口，并给出中文原因链（为何缺货或仍有余量）。
- `explain_transfer(order_id)`：调拨双方、数量等级、责任归属、父单/分拆、
  改道痕迹、牺牲的承诺、实收、损耗与原因。
- `price_basis(assessment_id)`：指导价及其引用的每条到货签收凭据与到货数量。

## 事件存储

账本只追加事件；任意时点状态都由重放得到。重复 `event_id` 视为重传，幂等忽略。
上层服务负责持久化与冲突隔离；本仓库定义可稳定交换的事实和全部业务守恒规则。
