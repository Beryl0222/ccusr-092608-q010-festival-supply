# 跨市场节日保供调拨账

描述节日多市场货源、在途状态、应急调拨和价格依据的事件，并由事件流重放出可核对的调拨账。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/festival_supply/`
  - `contracts.py`：基础契约校验（信封、时区、版本、事件载荷）。
  - `ledger.py`：事件溯源调拨账——数量守恒、处置权/签收权分立、拆分守恒、运单冻结核对、指导价依据、豁免自批禁止、模拟时钟、故障恢复与三类运营解释。
  - `cli.py`：命令行事件校验入口。
- `tests/`：契约测试与全部业务不变量测试。
- `docs/domain.md`：领域对象、事件语义与账务规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m festival_supply.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 调拨账用法要点

- 操作前先登记操作人目录（`market_ref` 与角色：`dispatch` / `inspector` / `price_monitor` / `approver`）。
- 命令方法（`declare_lot`、`reserve_quantity`、`create_emergency_transfer`、`settle_transfer`、`issue_price_guidance` 等）会自动生成递增版本事件；也可以直接 `record(event)` 投递外部事件。
- 违反领域规则抛出 `LedgerError(code, message)`，`code` 为稳定错误码（如 `insufficient_quantity`、`waybill_frozen`、`self_approval_forbidden`）。
- `advance_to(t)` 推进模拟日期；`save(path)` / `Ledger.load(path)` 完成故障恢复，重放不重复发货或结算。
- 解释口径：`explain_market`（为何缺货/有余量）、`explain_transfer`（牺牲了哪些承诺、守恒明细）、`explain_price`（指导价依据了哪些已到货回执）。
