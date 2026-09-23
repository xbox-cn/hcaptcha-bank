# -*- coding: utf-8 -*-
"""每个 GA shard 跑若干轮采样: 每轮一个全新 session(≤12 题), 产物落 out/"""
import os, random, subprocess, sys, time
from pathlib import Path

shard = int(os.environ.get("SHARD", "1"))
rounds = int(os.environ.get("ROUNDS", "3"))
per = int(os.environ.get("PER_SESSION", "12"))
out = Path("out"); out.mkdir(parents=True, exist_ok=True)
time.sleep(random.uniform(0, 20))          # 打散 20 台机器的起跑时间
for r in range(rounds):
    d = out / f"s{shard}_r{r}"
    cmd = [sys.executable, "-u", "hcaptcha_sample.py",
           "--url", "https://store.steampowered.com/join/",
           "--locale", os.environ.get("LOCALE", "zh-CN"),
           "--proxy", os.environ.get("PROXY", ""),
           "--n", str(per), "--out", str(d)]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.log").write_text((p.stdout or "") + (p.stderr or ""), encoding="utf-8")
    n = len(list(d.glob("*.png")))
    print(f"[shard {shard}] round {r}: rc={p.returncode} {time.time()-t0:.0f}s files={n}", flush=True)
    if r + 1 < rounds:
        time.sleep(random.uniform(5, 15))
print(f"[shard {shard}] done")
