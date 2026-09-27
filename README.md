# 跨市场节日保供调拨账

中秋、国庆双节叠加时，蔬菜、水果、返港海鲜同时进入高峰。本仓库定义跨市场调拨账的
领域事件契约，并提供执行全部业务守恒规则的事件溯源账本与可解释查询。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的单事件联调样例。
- `data/scenario.jsonl`：完整双节保供场景（申报、检验暂扣、锁货、应急调拨牺牲承诺、
  在途改道、签收损耗、豁免审批、指导价依据）。
- `src/festival_supply/`
  - `contracts.py`：事件信封基础校验。
  - `ledger.py`：事件溯源账本、守恒规则与解释性查询。
  - `cli.py`：校验与回放命令行入口。
- `tests/`：信封契约与全部不变量测试。
- `docs/domain.md`：领域对象、事件语义与守恒规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 单事件校验

```bash
PYTHONPATH=src python3 -m festival_supply.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 回放事件日志并解释

支持 JSONL（每行一个事件）或 JSON 数组，可叠加 `--as-of` 推进模拟日期：

```bash
# 某市场为何缺货或仍有余量
PYTHONPATH=src python3 -m festival_supply.cli replay data/scenario.jsonl \
  --explain-market M-SOUTH

# 某次调拨牺牲了哪些承诺、改道与损耗
PYTHONPATH=src python3 -m festival_supply.cli replay data/scenario.jsonl \
  --explain-transfer to-emg-veg

# 指导价采用了哪些已到货事实
PYTHONPATH=src python3 -m festival_supply.cli replay data/scenario.jsonl \
  --price-basis gp-veg-1001

# 模拟节后承诺自动释放（在途、临期同理）
PYTHONPATH=src python3 -m festival_supply.cli replay data/scenario.jsonl \
  --as-of 2026-10-09T00:00:00+08:00 --explain-market M-NORTH
```

## 核心规则速览

- 已组织货源 ≠ 可售：在途、检验暂扣、未放行、已锁定、过保鲜窗口均不可承诺。
- 来源市场保留处置权；接收市场只能确认实际到货。
- 价格监测人不能批准自己提出的异常豁免。
- 应急调拨可分拆，但总量、品质等级、责任归属守恒；超份额调拨必须显式声明牺牲的承诺。
- 相同物流回执重传不重复增减库存；运单同号而数量/去向不同立即冻结核对。
- 指导价只引用已到货签收凭据；故障恢复靠事件重放，不重复发货或结算。
