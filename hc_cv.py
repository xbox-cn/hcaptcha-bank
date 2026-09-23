"""
hCaptcha (Steam 题型池) 离线 CV 模块 —— 只处理本地图片, 不碰网络。

题图: canvas 1000x940, 题图区 (0,240)-(999,939) = 1000x700 ("panel")。
题型:
  chars  "点击被线条部分遮挡的三个字符":  固定火山背景 + 卡通动物贴纸 + 线条; 线压在贴纸上 => 被遮挡
  shapes "点击三个相同的形状":            彩色方块 (panel 内 (170,20)-(829,679)) 上 6 个三角网格多边形, 3 个全等
  puzzle "将正确的拼块放入缺失的位置":    拖拽题, 不做 (刷新)

对外接口:
  classify_prompt(text)                      -> 'chars' | 'shapes' | 'puzzle' | 'unknown'
  StickerDetector().detect(panel_rgb)        -> [{'box': (x0,y0,x1,y1), 'score': s}]   (GroundingDINO-tiny)
  segment_shapes(panel_rgb)                  -> [Blob]
  solve_matching_shapes(canvas_rgb)          -> Result
  StickerClassifier().predict(panel, boxes)  -> 每个贴纸"被遮挡"概率 (DINOv2-small 嵌入 + 逻辑回归, 权重 models/chars_lr.npz, 训练见 train_chars.py)
  solve_chars_blocked(canvas_rgb, detector, classifier) -> Result
  solve(canvas_rgb, prompt, detector)        -> Result   (统一入口, 坐标为 canvas 坐标)
CLI:
  python hc_cv.py --eval samples_index.json --labels labels.json --out eval_out/cv
"""
from __future__ import annotations

import os
import sys
import json
import time
import argparse
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import cv2
from PIL import Image

HERE = Path(__file__).resolve().parent
CANVAS_W, CANVAS_H = 1000, 940
PANEL_Y0 = 240
PANEL_W, PANEL_H = 1000, 700
SQUARE = (170, 20, 830, 680)  # shapes 题彩色方块在 panel 内的 bbox (x0, y0, x1, y1), 半开区间
BG_PATH = HERE / "models" / "chars_bg.png"

CHARS_WORDS = ("线条", "遮挡", "blocked by a line", "partly blocked")
SHAPES_WORDS = ("相同的形状", "matching shapes", "same shapes", "identical shapes")
PUZZLE_WORDS = ("拼块", "缺失", "puzzle", "piece", "drag", "拖", "missing", "into place")


# ----------------------------------------------------------------------------- 基础
def classify_prompt(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in CHARS_WORDS):
        return "chars"
    if any(w in t for w in SHAPES_WORDS):
        return "shapes"
    if any(w in t for w in PUZZLE_WORDS):
        return "puzzle"
    return "unknown"


def load_rgb(path) -> np.ndarray:
    return np.array(Image.open(path).convert("RGB"))


def to_panel(canvas_rgb: np.ndarray) -> np.ndarray:
    """canvas (1000x940) -> 题图区 (1000x700); 已经是 panel 尺寸则原样返回"""
    h, w = canvas_rgb.shape[:2]
    if (w, h) == (PANEL_W, PANEL_H):
        return canvas_rgb
    if (w, h) != (CANVAS_W, CANVAS_H):
        canvas_rgb = cv2.resize(canvas_rgb, (CANVAS_W, CANVAS_H), interpolation=cv2.INTER_AREA)
    return canvas_rgb[PANEL_Y0:PANEL_Y0 + PANEL_H, 0:PANEL_W]


def is_blank(rgb: np.ndarray) -> bool:
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return g.std() < 3 or (np.abs(rgb.astype(int) - 234).max(axis=2) < 8).mean() > 0.9


def ellipse(k: int):
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))


def fill_holes(mask_u8: np.ndarray) -> np.ndarray:
    h, w = mask_u8.shape
    ff = mask_u8.copy()
    pad = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(ff, pad, (0, 0), 255)
    return mask_u8 | cv2.bitwise_not(ff)


@dataclass
class Blob:
    mask: np.ndarray          # uint8 0/255, panel 尺寸
    area: int
    centroid: tuple           # (x, y) panel 坐标
    bbox: tuple               # (x0, y0, x1, y1)
    extra: dict = field(default_factory=dict)


@dataclass
class Result:
    kind: str
    ok: bool
    points: list              # [(x, y)] canvas 坐标
    scores: list
    confidence: float
    reason: str = ""
    elapsed: float = 0.0
    debug: dict = field(default_factory=dict)

    def to_json(self):
        return {"kind": self.kind, "ok": self.ok, "points": [(int(x), int(y)) for x, y in self.points],
                "scores": [round(float(s), 3) for s in self.scores], "confidence": round(float(self.confidence), 3),
                "reason": self.reason, "elapsed": round(self.elapsed, 2)}


# ----------------------------------------------------------------------------- 贴纸定位 (GroundingDINO)
class StickerDetector:
    """GroundingDINO-tiny 开放词表检测, 用来找卡通动物贴纸 (对被半透明线条压淡的贴纸也有效)"""

    def __init__(self, model_id="IDEA-Research/grounding-dino-tiny", prompt="cartoon animal.", threshold=0.3):
        self.model_id, self.prompt, self.threshold = model_id, prompt, threshold
        self._proc = self._model = None
        self.load_time = 0.0

    def load(self):
        if self._model is not None:
            return self
        t = time.time()
        import torch  # noqa
        from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection
        self._proc = AutoProcessor.from_pretrained(self.model_id)
        self._model = AutoModelForZeroShotObjectDetection.from_pretrained(self.model_id).eval()
        self.load_time = time.time() - t
        return self

    def detect(self, rgb: np.ndarray, prompt: str | None = None, max_side=400, min_side=40):
        import torch
        self.load()
        img = Image.fromarray(rgb)
        text = prompt or self.prompt
        inp = self._proc(images=img, text=text, return_tensors="pt")
        with torch.no_grad():
            out = self._model(**inp)
        r = self._proc.post_process_grounded_object_detection(
            out, inp.input_ids, threshold=self.threshold, text_threshold=self.threshold, target_sizes=[img.size[::-1]])[0]
        dets = []
        for b, s in zip(r["boxes"].tolist(), r["scores"].tolist()):
            x0, y0, x1, y1 = [int(round(v)) for v in b]
            w, h = x1 - x0, y1 - y0
            if w < min_side or h < min_side or w > max_side or h > max_side:
                continue
            dets.append({"box": (max(0, x0), max(0, y0), min(rgb.shape[1], x1), min(rgb.shape[0], y1)), "score": float(s)})
        return dedupe_boxes(dets)


def box_area(b):
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def box_inter(a, b):
    return box_area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def merge_split_stickers(dets, max_dim=230, max_gap=55):
    """当贴纸被粗线条/实心遮挡横切为两截时, 智能缝合为同一个完整贴纸框"""
    if len(dets) <= 1:
        return dets
    changed = True
    cur = list(dets)
    while changed:
        changed = False
        n = len(cur)
        for i in range(n):
            for j in range(i + 1, n):
                b1, b2 = cur[i]["box"], cur[j]["box"]
                x10, y10, x11, y11 = b1
                x20, y20, x21, y21 = b2
                w1, h1 = x11 - x10, y11 - y10
                w2, h2 = x21 - x20, y21 - y20

                ux0, uy0 = min(x10, x20), min(y10, y20)
                ux1, uy1 = max(x11, x21), max(y11, y21)
                uW, uH = ux1 - ux0, uy1 - uy0
                if uW > max_dim or uH > max_dim:
                    continue

                # 水平邻接 (贴纸被垂直线切断)
                y_inter = max(0, min(y11, y21) - max(y10, y20))
                x_gap = max(0, max(x10, x20) - min(x11, x21))
                is_horiz = (y_inter >= 0.5 * min(h1, h2)) and (x_gap <= max_gap)

                # 垂直邻接 (贴纸被水平线切断)
                x_inter = max(0, min(x11, x21) - max(x10, x20))
                y_gap = max(0, max(y10, y20) - min(y11, y21))
                is_vert = (x_inter >= 0.5 * min(w1, w2)) and (y_gap <= max_gap)

                if (is_horiz or is_vert) and (0.45 <= uW / max(1, uH) <= 2.2):
                    new_det = {
                        "box": (ux0, uy0, ux1, uy1),
                        "score": max(cur[i]["score"], cur[j]["score"]),
                    }
                    cur = [cur[k] for k in range(n) if k not in (i, j)] + [new_det]
                    changed = True
                    break
            if changed:
                break
    return cur


def dedupe_boxes(dets, iou_thr=0.5, contain_thr=0.7):
    """去掉重复框: IoU>thr 保留高分; 小框 70% 以上落在大框里则去掉小框; 缝合被切断贴纸"""
    dets = sorted(dets, key=lambda d: -d["score"])
    keep = []
    for d in dets:
        ok = True
        for k in keep:
            inter = box_inter(d["box"], k["box"])
            iou = inter / (box_area(d["box"]) + box_area(k["box"]) - inter + 1e-6)
            if iou > iou_thr or inter / (box_area(d["box"]) + 1e-6) > contain_thr:
                ok = False
                break
        if ok:
            keep.append(d)
    # 大框包住小框的情况: 留小的 (贴纸) 去大的? 大框往往是两只贴纸合框, 若大框内有>=2 个 keep 小框则去掉大框
    final = []
    for d in keep:
        inside = [k for k in keep if k is not d and box_inter(d["box"], k["box"]) / (box_area(k["box"]) + 1e-6) > contain_thr]
        if len(inside) >= 2:
            continue
        final.append(d)
    final = merge_split_stickers(final)
    final.sort(key=lambda d: (d["box"][1] // 120, d["box"][0]))
    return final


# ----------------------------------------------------------------------------- shapes
def segment_shapes(panel_rgb: np.ndarray, debug: dict | None = None, relaxed=False) -> list:
    """三角网格 → 边缘密度高 + 颜色接近填充色 → 连通域 → 用颜色细化边界"""
    x0, y0, x1, y1 = SQUARE
    sq = panel_rgb[y0:y1, x0:x1]
    gray = cv2.cvtColor(cv2.medianBlur(sq, 3), cv2.COLOR_RGB2GRAY)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    E = (mag > 60).astype(np.float32)
    dens = cv2.blur(E, (21, 21))
    lab = cv2.cvtColor(cv2.medianBlur(sq, 7), cv2.COLOR_RGB2LAB).astype(np.float32)

    # 填充色: 高密度像素里最大的颜色簇
    hi = dens > max(0.08, float(np.percentile(dens, 95)))
    pts = lab[hi].reshape(-1, 3)
    if len(pts) < 200:
        return []
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5)
    cv2.setRNGSeed(0)  # Reproducible segmentation across processes.
    _, lbl, centers = cv2.kmeans(pts[:: max(1, len(pts) // 5000)].copy(), 3, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    fill = centers[np.bincount(lbl.ravel(), minlength=3).argmax()]
    delta = lab - fill[None, None, :]
    if relaxed:
        delta = delta * np.array([0.45, 1.0, 1.0])
    dist = np.linalg.norm(delta, axis=2)
    allowed = lab[:, :, 0] > fill[0] - 48 if relaxed else np.ones(dist.shape, bool)

    cand = ((dens > 0.05) & (dist < (25 if relaxed else 30)) & allowed).astype(np.uint8) * 255
    cand = cv2.morphologyEx(cand, cv2.MORPH_CLOSE, ellipse(9))
    cand = cv2.morphologyEx(cand, cv2.MORPH_OPEN, ellipse(7))
    colour_ok = ((dist < (25 if relaxed else 28)) & allowed).astype(np.uint8) * 255

    n, labl, st, cen = cv2.connectedComponentsWithStats(cand)
    blobs = []
    for i in range(1, n):
        area = int(st[i][4])
        if area < 1200 or area > 45000:
            continue
        coarse = (labl == i).astype(np.uint8) * 255
        # 细化: 在 coarse 附近取颜色接近填充色的像素, 补洞, 取与 coarse 重叠最大的连通域
        region = cv2.dilate(coarse, ellipse(15))
        ref = cv2.bitwise_and(colour_ok, region)
        ref = cv2.morphologyEx(ref, cv2.MORPH_CLOSE, ellipse(7))
        ref = fill_holes(ref)
        ref = cv2.morphologyEx(ref, cv2.MORPH_OPEN, ellipse(5))
        n2, l2, s2, c2 = cv2.connectedComponentsWithStats(ref)
        best, best_ov = None, 0
        for j in range(1, n2):
            ov = int(((l2 == j) & (coarse > 0)).sum())
            if ov > best_ov:
                best, best_ov = j, ov
        if best is None or best_ov < 0.5 * area:
            m = coarse
            cx, cy = cen[i]
        else:
            m = (l2 == best).astype(np.uint8) * 255
            cx, cy = c2[best]
        ys, xs = np.where(m > 0)
        full = np.zeros(panel_rgb.shape[:2], np.uint8)
        full[y0:y1, x0:x1] = m
        blobs.append(Blob(mask=full, area=int(m.sum() // 255), centroid=(float(cx + x0), float(cy + y0)),
                          bbox=(int(xs.min() + x0), int(ys.min() + y0), int(xs.max() + x0 + 1), int(ys.max() + y0 + 1)),
                          extra={"coarse_area": area}))
    blobs.sort(key=lambda b: (b.centroid[1] // 150, b.centroid[0]))
    if debug is not None:
        debug["dens"] = dens
        debug["dist"] = dist
        debug["fill_lab"] = fill.tolist()
        debug["cand"] = cand
    return blobs


def _norm_mask(blob: Blob, size=160):
    """把 blob mask 裁出来, 按质心居中放到 size x size 画布 (不缩放)"""
    x0, y0, x1, y1 = blob.bbox
    m = blob.mask[y0:y1, x0:x1]
    cx, cy = blob.centroid[0] - x0, blob.centroid[1] - y0
    canvas = np.zeros((size, size), np.uint8)
    ox, oy = int(round(size / 2 - cx)), int(round(size / 2 - cy))
    h, w = m.shape
    sx0, sy0 = max(0, -ox), max(0, -oy)
    dx0, dy0 = max(0, ox), max(0, oy)
    ww, hh = min(w - sx0, size - dx0), min(h - sy0, size - dy0)
    if ww > 0 and hh > 0:
        canvas[dy0:dy0 + hh, dx0:dx0 + ww] = m[sy0:sy0 + hh, sx0:sx0 + ww]
    return canvas


def shape_similarity(a: np.ndarray, b: np.ndarray, step=5):
    """a, b: 居中的 0/255 mask; 返回 (最大 IoU, 角度, 翻转)"""
    size = a.shape[0]
    center = (size / 2 - 0.5, size / 2 - 0.5)
    A = a > 0
    area_a = A.sum()
    best = (0.0, 0, False)
    for flip in (False, True):
        bb = b[:, ::-1] if flip else b
        for ang in range(0, 360, step):
            M = cv2.getRotationMatrix2D(center, ang, 1.0)
            r = cv2.warpAffine(bb, M, (size, size), flags=cv2.INTER_NEAREST) > 0
            inter = (A & r).sum()
            union = area_a + r.sum() - inter
            iou = inter / union if union else 0.0
            if iou > best[0]:
                best = (float(iou), ang, flip)
    return best


def solve_matching_shapes(canvas_rgb: np.ndarray, debug_dir: Path | None = None, tag="") -> Result:
    from hc_shapes import solve_matching_shapes as match
    return match(canvas_rgb, debug_dir, tag)


def draw_shapes_debug(panel, blobs, best, sim, path):
    vis = panel.copy()
    for i, b in enumerate(blobs):
        col = (255, 0, 0) if i in best else (0, 0, 255)
        cnts, _ = cv2.findContours(b.mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(vis, cnts, -1, col, 2)
        cx, cy = int(b.centroid[0]), int(b.centroid[1])
        cv2.putText(vis, str(i), (cx - 8, cy + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 4)
        cv2.putText(vis, str(i), (cx - 8, cy + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.9, col, 2)
    n = len(blobs)
    y = 20
    for i in range(n):
        row = " ".join(f"{sim[i, j]:.2f}" if j != i else " -- " for j in range(n))
        cv2.putText(vis, f"{i}: {row}", (5, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        y += 16
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(vis).save(path)


# ----------------------------------------------------------------------------- chars
class StickerClassifier:
    """贴纸"是否被线条遮挡"分类器: DINOv2-small 嵌入 (贴纸紧裁剪 224x224, CLS+patch-mean) + 逻辑回归 (models/chars_lr.npz)"""

    def __init__(self, model_id="facebook/dinov2-small", weights=HERE / "models" / "chars_lr.npz", pad=0.0, size=224):
        self.model_id, self.weights, self.pad, self.size = model_id, weights, pad, size
        self._proc = self._model = self._lr = None
        self.load_time = 0.0

    def load(self):
        if self._model is None:
            t = time.time()
            import torch  # noqa
            from transformers import AutoImageProcessor, AutoModel
            self._proc = AutoImageProcessor.from_pretrained(self.model_id)
            self._model = AutoModel.from_pretrained(self.model_id).eval()
            self.load_time = time.time() - t
        if self._lr is None and self.weights is not None and Path(self.weights).exists():
            d = np.load(self.weights)
            self._lr = {k: d[k] for k in ("mu", "sd", "w", "b")}
        return self

    def crops(self, panel_rgb, boxes, flip=False):
        out = []
        for (x0, y0, x1, y1) in boxes:
            w, h = x1 - x0, y1 - y0
            X0, Y0 = max(0, int(x0 - self.pad * w)), max(0, int(y0 - self.pad * h))
            X1, Y1 = min(panel_rgb.shape[1], int(x1 + self.pad * w)), min(panel_rgb.shape[0], int(y1 + self.pad * h))
            c = cv2.resize(panel_rgb[Y0:Y1, X0:X1], (self.size, self.size), interpolation=cv2.INTER_AREA)
            out.append(np.ascontiguousarray(c[:, ::-1]) if flip else c)
        return out

    def embed(self, panel_rgb, boxes, flip=False) -> np.ndarray:
        import torch
        self.load()
        ims = [Image.fromarray(c) for c in self.crops(panel_rgb, boxes, flip)]
        inp = self._proc(images=ims, return_tensors="pt")
        with torch.no_grad():
            hs = self._model(**inp).last_hidden_state
        return torch.cat([hs[:, 0], hs[:, 1:].mean(1)], 1).numpy()

    def predict(self, panel_rgb, boxes) -> np.ndarray:
        self.load()
        if self._lr is None:
            raise RuntimeError(f"classifier weights missing: {self.weights} (run train_chars.py)")
        X = self.embed(panel_rgb, boxes)
        z = ((X - self._lr["mu"]) / self._lr["sd"]) @ self._lr["w"] + self._lr["b"]
        return 1.0 / (1.0 + np.exp(-z))


def solve_chars_blocked(canvas_rgb: np.ndarray, detector: StickerDetector, classifier: StickerClassifier | None = None,
                        debug_dir: Path | None = None, tag="", boxes=None) -> Result:
    t0 = time.time()
    panel = to_panel(canvas_rgb)
    dets = boxes if boxes is not None else detector.detect(panel)
    if len(dets) < 3:
        return Result("chars", False, [], [], 0.0, f"only {len(dets)} stickers", time.time() - t0)
    clf = classifier or StickerClassifier()
    probs = clf.predict(panel, [d["box"] for d in dets])
    order = sorted(range(len(dets)), key=lambda i: -probs[i])
    top = order[:3]
    p3 = float(probs[order[2]])
    p4 = float(probs[order[3]]) if len(order) > 3 else 0.0
    conf = float(np.clip(p3 - p4, 0, 1))
    pts = []
    for i in top:
        x0, y0, x1, y1 = dets[i]["box"]
        pts.append(((x0 + x1) / 2, (y0 + y1) / 2 + PANEL_Y0))
    res = Result("chars", True, pts, [float(probs[i]) for i in top], conf,
                 f"stickers={len(dets)} probs={[round(float(probs[i]), 2) for i in order]}", time.time() - t0,
                 {"dets": dets, "probs": [float(v) for v in probs], "order": order})
    if debug_dir is not None:
        draw_chars_debug(panel, dets, probs, top, debug_dir / f"{tag}chars.png")
    return res


def draw_chars_debug(panel, dets, probs, top, path):
    vis = panel.copy()
    for i, d in enumerate(dets):
        x0, y0, x1, y1 = d["box"]
        col = (255, 0, 0) if i in top else (0, 0, 255)
        cv2.rectangle(vis, (x0, y0), (x1, y1), col, 3)
        cv2.putText(vis, f"{i} p={probs[i]:.2f}", (x0 + 3, y0 + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4)
        cv2.putText(vis, f"{i} p={probs[i]:.2f}", (x0 + 3, y0 + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, col, 2)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(vis).save(path)


# ----------------------------------------------------------------------------- 统一入口
def solve(canvas_rgb: np.ndarray, prompt: str, detector: StickerDetector | None = None, classifier: StickerClassifier | None = None,
          debug_dir=None, tag="") -> Result:
    kind = classify_prompt(prompt)
    if kind == "chars":
        return solve_chars_blocked(canvas_rgb, detector or StickerDetector(), classifier, debug_dir, tag)
    if kind == "shapes":
        return solve_matching_shapes(canvas_rgb, debug_dir, tag)
    return Result(kind, False, [], [], 0.0, f"unsupported kind {kind}")


# ----------------------------------------------------------------------------- 离线评估
def evaluate(index_path, labels_path, out_dir, kinds=("chars", "shapes")):
    idx = json.loads(Path(index_path).read_text(encoding="utf-8"))
    labels = json.loads(Path(labels_path).read_text(encoding="utf-8")) if labels_path and Path(labels_path).exists() else {}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    det = StickerDetector()
    clf = StickerClassifier()
    rows = []
    for r in idx:
        if r["kind"] not in kinds:
            continue
        canvas = load_rgb(HERE / r["path"])
        if is_blank(to_panel(canvas)):
            continue
        tag = r["path"].replace("samples_", "").replace("_canvas.png", "").replace("/", "_") + "_"
        res = solve(canvas, r["prompt"], det, clf, out, tag)
        lab = labels.get(r["path"])
        hit = None
        if lab and res.ok:
            gt = [(x, y) for x, y in lab["points"]]
            hit = sum(1 for px, py in res.points if any(abs(px - gx) < 45 and abs(py - gy) < 45 for gx, gy in gt))
        rows.append({"path": r["path"], "kind": r["kind"], "ok": res.ok, "hit": hit, "conf": round(res.confidence, 3),
                     "elapsed": round(res.elapsed, 2), "reason": res.reason})
        print(f"{r['path']:34s} {r['kind']:6s} ok={res.ok} hit={hit} conf={res.confidence:.3f} t={res.elapsed:.1f}s {res.reason}")
    (out / "cv_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for k in kinds:
        rs = [x for x in rows if x["kind"] == k]
        if rs:
            full = sum(1 for x in rs if x["hit"] == 3)
            lab = sum(1 for x in rs if x["hit"] is not None)
            print(f"== {k}: {len(rs)} samples, labelled {lab}, all-3-correct {full}, mean elapsed {np.mean([x['elapsed'] for x in rs]):.1f}s")
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", default="samples_index.json")
    ap.add_argument("--labels", default="labels.json")
    ap.add_argument("--out", default="eval_out/cv")
    ap.add_argument("--kinds", default="chars,shapes")
    a = ap.parse_args()
    evaluate(a.eval, a.labels, a.out, tuple(a.kinds.split(",")))
