#!/usr/bin/env python3
"""wg 服务器: 轮询 GitHub Release(shards) 拉取 GA 已归类分片并合并入库
无 token 依赖(公开仓库匿名 API ✓); 状态记在 /data/.pull_state.json
"""
import json, subprocess, sys, time
from pathlib import Path

REPO = "xbox-cn/hcaptcha-bank"
TAG = "shards"
STATE = Path("/data/.pull_state.json")
INCOMING = Path("/data/incoming")
MERGED_LOG = Path("/data/.pull_merged.log")
INCOMING.mkdir(parents=True, exist_ok=True)

def sh(cmd, timeout=300):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return f"ERR {e}", -1

def main():
    out, rc = sh(f"curl -sS --max-time 60 https://api.github.com/repos/{REPO}/releases/tags/{TAG}", 90)
    try:
        rel = json.loads(out)
    except Exception:
        print(f"[{time.strftime('%F %T')}] API 解析失败 rc={rc}: {out[:200]}")
        return 1
    if "assets" not in rel:
        print(f"[{time.strftime('%F %T')}] Release {TAG} 不存在或无 assets: {str(rel)[:150]}")
        return 0
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    assets = [a for a in rel["assets"] if a["name"].endswith(".tar")]
    if not assets:
        print(f"[{time.strftime('%F %T')}] 无分片资产")
        return 0
    processed = 0
    for a in sorted(assets, key=lambda x: x["name"]):
        name, stamp = a["name"], a["updated_at"]
        if state.get(name) == stamp:
            continue
        dest = INCOMING / name
        dl, rc = sh(f'curl -sS -L --max-time 1800 -o "{dest}" "{a["browser_download_url"]}"', 1900)
        if rc != 0 or not dest.exists() or dest.stat().st_size == 0:
            print(f"[{time.strftime('%F %T')}] 下载失败 {name} rc={rc} {dl[:120]}")
            continue
        print(f"[{time.strftime('%F %T')}] 下载 {name} {dest.stat().st_size//1048576}MB → 合并")
        outp, rc2 = sh(f"python3 /opt/wgbank/merge.py {dest}", 3600)
        print("  " + outp.replace("\n", "\n  "))
        if rc2 == 0:
            state[name] = stamp
            with MERGED_LOG.open("a") as f:
                f.write(f"{time.strftime('%F %T')} {name} {stamp} {outp.splitlines()[0] if outp else ''}\n")
            processed += 1
    STATE.write_text(json.dumps(state))
    if processed:
        print(f"[{time.strftime('%F %T')}] 本轮合并 {processed} 个分片")
    return 0

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        sys.exit(main())
    while True:
        try: main()
        except Exception as e: print(f"[{time.strftime('%F %T')}] 异常 {e}")
        time.sleep(300)
