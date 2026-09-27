"""从命令行校验领域事件，或回放事件日志并给出解释。

用法:
  python -m festival_supply.cli <schema.json> <event.json>
  python -m festival_supply.cli replay <events.json...> [--as-of 时间]
        (--explain-market 市场 | --explain-transfer 调拨单 | --price-basis 判断)
"""

import json
import sys
from pathlib import Path

from .contracts import validate_event
from .ledger import DomainError, Ledger


def _load_any(path: str):
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2

    if argv[0] != "replay":
        if len(argv) != 2:
            print("用法: python -m festival_supply.cli <schema.json> <event.json>", file=sys.stderr)
            return 2
        schema = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        event = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        issues = validate_event(event, schema)
        if not issues:
            print("valid")
            return 0
        for issue in issues:
            print(f"{issue.field}\t{issue.code}\t{issue.message}")
        return 1

    # replay 模式
    paths, as_of, query, target = [], None, None, None
    i = 1
    while i < len(argv):
        token = argv[i]
        if token == "--as-of":
            as_of = argv[i + 1]
            i += 2
        elif token in ("--explain-market", "--explain-transfer", "--price-basis"):
            query = {"--explain-market": "market",
                     "--explain-transfer": "transfer",
                     "--price-basis": "price"}[token]
            target = argv[i + 1]
            i += 2
        else:
            paths.append(token)
            i += 1
    if not paths or query is None:
        print(__doc__, file=sys.stderr)
        return 2

    events = []
    for path in paths:
        events.extend(_load_any(path))
    try:
        ledger = Ledger(events)
        if query == "market":
            result = ledger.explain_market(target, as_of)
        elif query == "transfer":
            result = ledger.explain_transfer(target, as_of)
        else:
            result = ledger.price_basis(target)
    except DomainError as err:
        print(f"{err.code}\t{err.message}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
