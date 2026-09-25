# -*- coding: utf-8 -*-
"""汇总本次 run 的所有 shard 产物 → 追加一条记录到 data/records.jsonl
记录内容: 时间(UTC/小时桶) / 挑战数 / 题型(题面)分布 / 挑战结构(canvas/drag/grid)分布
"""
import json, os, re, time
from collections import Counter
from pathlib import Path

LINE = re.compile(r"^\[(\d+)\]\s+(\S+)\s+.*?prompt=\['(.*?)'\]")
rows = []
for log in sorted(Path("art").rglob("run.log")):
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LINE.match(line.strip())
        if m:
            rows.append({"kind": m.group(2), "prompt": m.group(3).strip(), "shard": log.parent.name})
rec = {
    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "hour_utc": time.strftime("%Y-%m-%dT%H", time.gmtime()),
    "run_id": os.environ.get("GITHUB_RUN_ID", ""),
    "n": len(rows),
    "prompts": dict(Counter(r["prompt"] for r in rows).most_common()),
    "kinds": dict(Counter(r["kind"] for r in rows).most_common()),
}
out = Path("data/records.jsonl")
out.parent.mkdir(parents=True, exist_ok=True)
with out.open("a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print(json.dumps(rec, ensure_ascii=False)[:600])


def update_counts():
    """把本次 run 的题型计数累加进 data/counts.json(供采集端配额判断)"""
    import json as _j, re as _re
    from collections import Counter as _C
    from pathlib import Path as _P
    pat = _re.compile(r"^\[(\d+)\]\s+type=(\S+)\s+have=")
    got = _C()
    for log in sorted(_P("art").rglob("run.log")):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = pat.match(line.strip())
            if m:
                got[m.group(2)] += 1
    cp = _P("data/counts.json")
    cur = {}
    if cp.exists():
        try:
            cur = _j.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
    for k, v in got.items():
        cur[k] = int(cur.get(k, 0)) + v
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(_j.dumps(cur, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print("counts:", _j.dumps(cur, ensure_ascii=False))


if __name__ == "__main__":
    update_counts()
