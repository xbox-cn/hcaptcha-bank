# -*- coding: utf-8 -*-
"""按小时看题型规律: python tools/report.py [data/records.jsonl]"""
import json, sys
from collections import defaultdict
from pathlib import Path

recs = [json.loads(l) for l in Path(sys.argv[1] if len(sys.argv) > 1 else "data/records.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
byhour = defaultdict(lambda: defaultdict(int))
for r in recs:
    for p, n in (r.get("prompts") or {}).items():
        byhour[r["hour_utc"]][p] += n
for h in sorted(byhour):
    tot = sum(byhour[h].values())
    top = sorted(byhour[h].items(), key=lambda kv: -kv[1])[:4]
    print(f"{h}Z  n={tot:4d}  " + " | ".join(f"{p[:28]}={n}" for p, n in top))
