# 领域约定

描述节日多市场货源、在途状态、应急调拨和价格依据的事件，以及由这些事件重放出来的调拨账规则。

聚合对象包括 `supply_lot`（货源批次）、`transit_leg`（在途节点）、`purchase_commitment`（采购承诺）、`transfer_order`（应急调拨单）、`price_guidance`（指导价）、`price_exemption`（异常豁免）、`store_gap`（门店缺口）。

所有发生时间都必须携带时区，版本号从 1 开始按聚合递增，基础校验不会改写调用方输入。相同事件标识的业务幂等、冲突隔离和状态推进由上层服务 `src/festival_supply/ledger.py` 负责。

## 事件与必填载荷

| 事件 | 聚合 | 必填载荷要点 |
| --- | --- | --- |
| `LOT_DECLARED` | `supply_lot` | 品类规格、来源市场、所有权人、数量、品质等级、保鲜窗口 `usable_until` |
| `TRANSIT_UPDATED` | `transit_leg` | 调拨单、运单号、位置、观察时间、数量、去向市场 |
| `INSPECTION_RECORDED` | `supply_lot` | `status`：`passed` / `detained` / `released` / `condemned` |
| `QUANTITY_RESERVED` | `purchase_commitment` | 货源批次、锁定市场、数量；可选节后释放时点 `release_after` |
| `EMERGENCY_TRANSFER_CREATED` | `transfer_order` | 货源批次、调出/接收市场、运单号、数量、品质等级、所有权人、来源市场授权 `source_authorization`、拆分 `splits`、牺牲承诺 `sacrifices` |
| `TRANSFER_SETTLED` | `transfer_order` | 接收人、运单号、**物流回执号** `receipt_no`、实收量、到货品级、损耗原因 |
| `WAYBILL_FROZEN` | `transfer_order` | 运单号、冻结原因；冲突时载荷还携带待核对的数量/去向 |
| `WAYBILL_RECONCILED` | `transfer_order` | 运单号、`resolution`：`confirmed` / `corrected`（更正值随载荷） |
| `LOT_RELEASED` | `supply_lot` | `release_reason`：`post_holiday` / `expiring`，释放量与涉及的承诺 |
| `PRICE_GUIDANCE_ISSUED` | `price_guidance` | 市场、品类规格、指导价、**已签收回执清单** `basis_receipt_ids` |
| `PRICE_EXEMPTION_REQUESTED` / `PRICE_EXEMPTION_APPROVED` | `price_exemption` | 申请人、批准人、市场与品类规格 |
| `STORE_GAP_REPORTED` | `store_gap` | 市场、门店、品类规格、缺口量、观察时间 |

## 账务规则（由事件重放保证）

### 1. “已组织货源”不等于可售

每批货始终满足守恒式：

```
申报量 = 在库可分配 + 已锁采购承诺 + 已发应急调拨 + 已释放/销毁
```

可售口径只计入：来源市场在库、检验通过（暂扣后 `released` 也算）、未过保鲜窗口的货，加上**已签收到货**的货。以下都不算可售，也不能进入价格判断：

- 已装车/在途但未签收（`in_transit_inbound`）；
- 检验暂扣（`detained`）或销毁（`condemned`）；
- 已被采购承诺锁定的份额；
- 超过保鲜窗口已释放的货。

因此同一车货不可能被两个市场重复承诺：并发采购订单按顺序提交，只能锁定 `free` 中真正可分配的份额，超出即 `insufficient_quantity`，绝不超卖。

### 2. 处置权与签收权分立

- 货源只能由**来源市场**申报、锁定、调出或释放；应急调拨必须携带 `source_authorization` 授权凭据。
- **接收市场只能确认实际到货**，调出方不能替其签收；父调拨单不直接签收，拆分后逐个子批次确认。
- 签收量不得超过发运量；实收少于发运必须登记 `loss_reason`；到货品级与发运不一致直接拒绝入库。

### 3. 应急调拨拆分守恒与承诺牺牲

- 拆分各子批次数量之和必须等于调拨总量（`split_quantity_mismatch`）。
- 品质等级与所有权（责任归属）固定在父单，子批次只能继承，不得改变。
- 挪用任何已锁定的采购承诺，必须在 `sacrifices` 中点名承诺与让出量；支持部分牺牲，余额继续锁定。未点名而超用可分配份额的调拨被拒绝。
- 每次牺牲都在 `explain_transfer` 中可查：运营人员能回答“某次调拨牺牲了哪些承诺”。

### 4. 运单冻结与核对

- 在途节点上报只更新位置/观察时间，**不重复增减库存**。
- 同一运单号上报的数量或去向与首次登记不一致时，系统在同一提交流程内追加 `WAYBILL_FROZEN` 事件（冲突上报本身仍被拒绝）；冻结期间禁止继续上报和签收。
- 解冻只能由来源市场执行：`confirmed` 维持原登记，`corrected` 以核对事件携带的更正值修正来源市场账目（增量从在库出、减量退回在库，守恒式重新校验）。
- 同一物流回执号 `receipt_no` 只能用于一次签收（`duplicate_receipt`）；同一 `event_id` 重传原样返回，库存不发生第二次变化。

### 5. 价格纪律

- 指导价的每条依据都必须是**本市场、本品类规格、已签收**的到货回执；在途、暂扣、锁而未到的数量一律不参与（`explain_price` 列出依据回执）。
- 异常豁免的申请人不得成为批准人——即使同一操作人同时具备监测与审批角色也拒绝（`self_approval_forbidden`）；无 `approver` 角色不得审批。

### 6. 模拟时钟与节后释放

`Ledger.advance_to(t)` 只能向前推进时钟，按时间点依次生成释放事件：

- 到达采购承诺自己的 `release_after`（如节后）时点：锁定量归还来源市场在库；
- 到达批次 `usable_until`：在库与仍锁定的货一并退出可分配（锁定承诺标记破裂），**已在应急调拨途中的货不受影响**。

### 7. 故障恢复

事件流是唯一事实来源（`save` / `load` / `replay`）。所有数量变动都发生在事件投影函数中，因此：

- 重放同一事件流得到完全相同的账目，不会重复发货或重复结算；
- 按聚合的版本号做乐观并发控制，迟到/重复版本返回 `event_conflict`；
- `snapshot_at(t)` 可复盘任意时点“当时为何缺货/有余量”。

## 三类运营解释

- `explain_market(market, category, spec)`：可售、锁定、在途、暂扣、销毁、临期释放、已到货分项，结合门店缺口给出“为何缺货/仍有余量”的中文理由。
- `explain_transfer(transfer_id)`：拆分守恒明细、被牺牲的承诺清单、授权凭据、实收/损耗/回执、冻结中的运单。
- `explain_price(guidance_id)`：指导价数值与其依据的全部已签收到货回执。
