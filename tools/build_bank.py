# -*- coding: utf-8 -*-
"""统一题库归类: 本地 samples_*(samples.json / 日志) + GA bank_out/**(run.log) → bank_by_type/
特性: 全局按图片内容哈希去重(同图只留一份), 输出 manifest.csv
用法: python tools/build_bank.py [--no-ga]
"""
import csv, hashlib, json, re, shutil, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LINE = re.compile(r"^\[(\d+)\]\s+(\S+)\s+.*?prompt=\['(.*?)'\]")


def ptype(p: str) -> str:
    """题面 → 题型目录(中英双语校准; 顺序敏感, 具体规则在前)"""
    t = (p or "").strip()
    L = t.lower()
    # --- drag 家族 ---
    if "药瓶" in t or ("拖入" in t and "槽" in t):                       return "drag_bottle"
    if "screw" in L or "螺丝" in t:                                    return "drag_screw"
    if "布料" in t or "布制" in t:                                      return "attr_cloth"
    if "拉链" in t or "zipper" in L:                                   return "attr_zipper"
    if "关上" in t or "关闭" in t or "可以关" in t or "can be closed" in L or "things that close" in L:
        return "attr_closable"
    if "可以打开" in t or "能打开" in t or "can be opened" in L:         return "attr_openable"
    if "发光" in t or "emit light" in L or "glow" in L or "light source" in L:
        return "attr_light"
    # --- 缺失类 ---
    if "缺失的连" in t or "断裂处" in t or "断口" in t or "missing link" in L or "missing connection" in L or "break in the chain" in L:
        return "missing_link"
    if "缺失" in t or "missing" in L:                                  return "missing_spot"
    # --- chars 家族 ---
    if "线条" in t or "partly blocked" in L or "blocked by a line" in L: return "chars_lattice"
    if "不同的动物" in t or "不同的图标" in t or "找出不同" in t or "does not belong" in L or "different animal" in L:
        return "chars_odd"
    if "两个打破规律" in t or "两个不匹配" in t or "two" in L and "breaking" in L:
        return "chars_pair"
    # --- grid 家族 ---
    if "同一类型" in t or "三个相同" in t or ("three matching shapes" in L) or ("three characters" in L and "blocked" in L):
        return "grid_triple"
    if "rely on the shown food" in L or "以所示食物为食" in t or "food source" in L:
        return "animal_diet"
    if "同一地点" in t or "相同地点" in t or "same place" in L:            return "same_place"
    if "示例" in t and ("食物" in t or "菜品" in t or "食品" in t):        return "grid_sample"
    if "示例" in t or "所示物品" in t:                                    return "grid_assoc"
    if "同一地点" in t or "相同地点" in t or "same place" in L:            return "same_place"
    # --- drag 其他 ---
    if "螺丝" in t or "screw" in L:                                     return "drag_screw"
    if "字母" in t and "拖" in t:                                       return "drag_letter"
    if "形状" in t or "matching shape" in L:
        return "drag_shape"
    if "完成图片拼接" in t or "拼出" in t or "拼成" in t or "complete the image" in L or "puzzle piece" in L:
        return "drag_puzzle"
    if "拖放" in t or "拖到" in t or "拖入" in t or "drag" in L or "empty space" in L:
        return "drag_place"
    # --- 其他语义 ---
    if "absorb liquid" in L or "吸液" in t or "吸水" in t:               return "attr_absorb"
    if "reflection in the mirror" in L or "镜" in t:                     return "mirror"
    if "能在平面上滚动" in t or "滚动的物体" in t or "roll" in L:          return "roll"
    if "配对" in t or "pair" in L:                                      return "pair_match"
    if "摆放不正确" in t or "not placed correctly" in L:                 return "puzzle_wrong"
    if "拼上" in t or "拼起来" in t:                                     return "puzzle_fit"
    return "other"


def jobs_local():
    out = []
    for d in sorted(ROOT.glob("samples_*")):
        if not d.is_dir():
            continue
        sj = d / "samples.json"
        if sj.exists():
            out.append((sj, d, d.name))
    for log in sorted((ROOT / "recon").glob("sample_cn*.log")):
        tag = re.search(r"sample_cn(\d+)_", log.name)
        d = ROOT / f"samples_steam{tag.group(1)}" if tag else None
        if d and d.exists() and not (d / "samples.json").exists():
            out.append((log, d, d.name))
    return out


def jobs_ga():
    out, seen = [], set()
    for lg in sorted((ROOT / "bank_out").glob("**/run.log")):
        rd = lg.parent
        if rd in seen or lg.stat().st_size <= 300:
            continue
        seen.add(rd)
        parts = [p for p in rd.parts if p != "bank_out"]
        rid = next((p for p in parts if p.isdigit() and len(p) >= 8), "run?")
        shard = next((p for p in parts if "shard" in p), rd.parent.name).replace("bank-shard-", "s")
        out.append((lg, rd, f"{rid[-6:]}_{shard}_{rd.name}"))
    return out


def iter_items(job):
    """yield (idx, kind, prompt, png_path)"""
    src, d, tag = job
    if src.name == "samples.json":
        try:
            recs = json.loads(src.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return
        for r in recs:
            i = r.get("i", 0)
            pr = r.get("prompt") or []
            prompt = " ".join(pr) if isinstance(pr, list) else str(pr)
            kind = r.get("kind", "canvas")
            pats = [f"s{i:02d}*.png", f"*_{i:02d}_*.png", f"s{i:02d}.png"]
            for pat in pats:
                for f in sorted(d.glob(pat)):
                    yield i, kind, prompt, f
                else:
                    continue
                break
    else:
        idx2 = {}
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            m = LINE.match(line.strip())
            if m:
                idx2[int(m.group(1))] = (m.group(2), m.group(3).strip())
        for i, (kind, prompt) in idx2.items():
            files = sorted(d.glob(f"s{i:02d}_*.png")) or sorted(d.glob(f"s{i:02d}*"))
            for f in files:
                yield i, kind, prompt, f


def main():
    use_ga = "--no-ga" not in sys.argv
    jobs = jobs_local() + (jobs_ga() if use_ga else [])
    out = ROOT / "bank_by_type"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    safe = re.compile(r'[<>:"/\|?*]')
    seen_hash, rows, cnt, imgs, dup, noimg = set(), [], Counter(), Counter(), 0, 0
    for job in jobs:
        src, d, tag = job
        n = 0
        for i, kind, prompt, f in iter_items(job):
            try:
                h = hashlib.md5(f.read_bytes()).hexdigest()
            except Exception:
                continue
            if h in seen_hash:
                dup += 1
                continue
            seen_hash.add(h)
            t = ptype(prompt)
            (out / t).mkdir(exist_ok=True)
            dest = out / t / safe.sub("_", f"{tag}__{f.name}")
            shutil.copy(f, dest)
            rows.append({"type": t, "source": tag, "idx": i, "kind": kind, "prompt": prompt, "file": dest.name, "md5": h[:10]})
            cnt[t] += 1
            imgs[t] += 1
            n += 1
        if n == 0:
            noimg += 1
    with (out / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["type", "source", "idx", "kind", "prompt", "file", "md5"])
        w.writeheader(); w.writerows(rows)
    print(f"{out.name}/: {len(rows)} 唯一图 / {sum(cnt.values())} 条 )  (session {len(jobs)}, 去重丢弃 {dup}, 无图 session {noimg})")
    for t, n in cnt.most_common():
        print(f"  {t:15s} {n:5d} 图")


if __name__ == "__main__":
    main()
