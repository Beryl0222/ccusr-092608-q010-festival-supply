# 领域约定

描述节日多市场货源、在途状态、应急调拨和价格依据的事件。

聚合对象包括`supply_lot`、`transit_leg`、`purchase_commitment`、`transfer_order`。事件类型包括`LOT_DECLARED`、`TRANSIT_UPDATED`、`INSPECTION_RECORDED`、`QUANTITY_RESERVED`、`TRANSFER_SETTLED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `TRANSIT_UPDATED`：载荷还需包含 `location_ref`, `observed_at`。
- `QUANTITY_RESERVED`：载荷还需包含 `market_ref`, `quantity`。
- `TRANSFER_SETTLED`：载荷还需包含 `received_quantity`, `loss_reason`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
