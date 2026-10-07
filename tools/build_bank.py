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
    t = t.split(" [tile")[0].strip()      # 去掉 tile 后缀
    L = t.lower()
    # ---------------- 英文题面(补齐) ----------------
    if "letter" in L and "drag" in L:                              return "drag_letter"
    if "letter to the place" in L:                                 return "drag_letter"
    if "bottle" in L and "slot" in L:                              return "drag_bottle"
    if "made of cloth" in L or "made of fabric" in L or "cloth" in L or "fabric" in L: return "attr_cloth"
    if "while swimming" in L or "swimming" in L or "wear in water" in L:               return "attr_swim_wear"
    if "for the garden" in L or "planting" in L or "garden" in L:  return "attr_garden"
    if "two rings" in L:                                           return "rings_two"
    if "not connected" in L or "ring" in L and "connect" in L:     return "rings_unconnected"
    if "can carry" in L or "carry" in L and "vehicle" in L:        return "vehicle_carry"
    if "framed piece" in L or "with a frame" in L:                 return "drag_frame_puzzle"
    if "ingredients" in L or "dish" in L:                          return "grid_sample"
    if "goes with" in L or "used with" in L or "go together" in L: return "grid_assoc"
    if "same kind" in L or "of the same type" in L:                return "grid_triple"
    if "no legs" in L or "without legs" in L:                      return "animal_nolegs"
    if "with legs" in L or "move with legs" in L:                  return "animal_legs"
    if "gills" in L:                                               return "animal_gills"
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
    if "比样本" in t or "lighter than" in L or "heavier than" in L:   return "attr_lighter"
    if "游泳" in t or "swimming" in L:                             return "attr_swim_wear"
    if "可以移动的车" in t or "能移动的交通工具" in t:               return "vehicle_move"
    if "浴缸" in t:                                                return "attr_bath_toy"
    if "生长在地下" in t:                                           return "attr_underground_food"
    if "舀取" in t or "舀起" in t:                                  return "attr_scoop"
    if "较短的线段" in t or "shorter line" in L:                     return "lines_shortest"
    if "配合使用" in t or "可与样本" in t:                            return "grid_assoc"
    if "没有腿" in t or "no legs" in L:                            return "animal_nolegs"
    if "用腿移动" in t or "move with legs" in L:                    return "animal_legs"
    if "带框的碎片" in t or "framed piece" in L:                    return "drag_frame_puzzle"
    if "柔软且有弹性" in t or "soft and elastic" in L:             return "attr_soft_elastic"
    if "空气充气" in t or "inflate" in L or "inflatable" in L:      return "attr_inflatable"
    if "能被这辆车移动" in t or "能被该车辆拖行" in t:             return "vehicle_move"
    if "可以使用该物体" in t or "物体的所有用途" in t:               return "attr_uses"
    if "正确的轮廓" in t or "匹配的轮廓" in t:                       return "drag_silhouette"
    if "完成图案" in t:                                            return "drag_pattern"
    if "空白格子" in t:                                            return "drag_shape_slot"
    if "能运载" in t or "can carry" in L or "transport" in L:       return "vehicle_carry"
    if "两个环" in t or "two rings" in L:                          return "rings_two"
    if "种植花园" in t or "garden" in L:                           return "attr_garden"
    if "未与大圆相连" in t or "圆环" in t:                        return "rings_unconnected"
    if "倒影" in t or "reflection" in L:                          return "mirror"
    if "烤箱" in t or "oven" in L:                                return "attr_oven"
    if "缺少方块" in t or "缺方块的塔" in t or "missing block" in L:      return "tower_missing_block"
    if "鳃呼吸" in t or "gills" in L or "breathe" in L:              return "animal_gills"
    if "缺失的连" in t or "断裂处" in t or "断口" in t or "missing link" in L or "missing connection" in L or "break in the chain" in L:
        return "missing_link"
    if "缺失" in t or "missing" in L:                                  return "missing_spot"
    # --- chars 家族 ---
    if "线条" in t or "partly blocked" in L or "blocked by a line" in L:   # 按题面数量拆变体(各自独立配额)
        if "三" in t or "three" in L:   return "chars_line_triple"
        if "两" in t or "two" in L:     return "chars_line_pair"
        if "二" in t:                   return "chars_line_pair"
        return "chars_line_single"
    if "partly blocked" in t and "character" in L: return "chars_line_single"
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
            # 宫格题: 把 s{i}_grid/tile_*.png 也纳入(每格单独成样本)
            for sub in sorted(d.glob(f"s{i:02d}_*")):
                if sub.is_dir():
                    for t in sorted(sub.glob("tile_*.png")):
                        yield i, kind, prompt + f" [tile {t.stem}]", t


def main():
    use_ga = "--no-ga" not in sys.argv
    jobs = jobs_local() + (jobs_ga() if use_ga else [])
    out = ROOT / "bank_by_type"
    incremental = "--rebuild" not in sys.argv          # 默认增量, 保留已有
    if not incremental and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    # 增量模式: 先把已有图片的哈希登记进来
    pre = {}
    if incremental:
        for f in out.rglob("*.png"):
            try:
                pre[hashlib.md5(f.read_bytes()).hexdigest()] = f
            except Exception:
                pass
    safe = re.compile(r'[<>:"/\|?*]')
    seen_hash, rows, cnt, imgs, dup, noimg = set(pre), [], Counter(), Counter(), 0, 0
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
            if "tile" in f.name:      t += "_grid"      # 九宫格题(每格单独存)
            elif "_canvas" in f.name: t += "_canvas"    # 画布题(整张 toDataURL)
            (out / t).mkdir(exist_ok=True)
            dest = out / t / safe.sub("_", f"{tag}__{f.name}")
            shutil.copy(f, dest)
            rows.append({"type": t, "source": tag, "idx": i, "kind": kind, "prompt": prompt, "file": dest.name, "md5": h[:10]})
            cnt[t] += 1
            imgs[t] += 1
            n += 1
        if n == 0:
            noimg += 1
    fn = ["type", "source", "idx", "kind", "prompt", "file", "md5"]
    man = out / "manifest.csv"
    old_rows = []
    if incremental and man.exists():                      # 追加模式: 保留历史记录
        try:
            old_rows = [r for r in csv.DictReader(man.open(encoding="utf-8-sig"))]
        except Exception:
            old_rows = []
    have = {r.get("file") for r in old_rows}
    with man.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fn); w.writeheader()
        for r in old_rows:
            w.writerow({k: r.get(k, "") for k in fn})
        for r in rows:
            if r["file"] not in have:
                w.writerow(r)
    total = len(list(out.rglob("*.png")))
    print(f"{out.name}/: 本次新增 {len(rows)} 张 → 目录现有 {total} 张  (session {len(jobs)}, 跳过重复 {dup}, 无图 session {noimg})")
    print(f"本次新增分布: {dict(cnt.most_common())}")
    for t, n in cnt.most_common():
        print(f"  {t:15s} {n:5d} 图")


if __name__ == "__main__":
    main()
