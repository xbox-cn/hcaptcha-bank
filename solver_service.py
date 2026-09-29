# -*- coding: utf-8 -*-
"""本地 hCaptcha 求解服务 (给 Chrome 扩展调用)
用法: python solver_service.py [--port 8723] [--debug]
接口:
  GET  /health          -> {"ok":true,"solvers":[...]}
  POST /solve           -> {"type":..,"action":"click|drag|refresh","points":[[x,y]..],"conf":..,"reason":..}
       body: {"prompt":str, "canvas":"data:image/png;base64,..", "tiles":[...], "w":1000,"h":940,"example":0}
坐标一律用 canvas 原始像素(1000x940), 扩展侧再换算到 CSS 像素。
"""
from __future__ import annotations
import argparse, base64, io, json, sys, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / "tools"))

import hc_cv                                   # noqa: E402
import hc_drag                                 # noqa: E402
try:
    import solvers.registry as registry          # noqa: E402
except Exception:
    registry = None

from build_bank import ptype as prompt_type      # noqa: E402  单一事实来源(28 类)


def b64_to_rgb(data: str) -> np.ndarray | None:
    if not data:
        return None
    try:
        raw = data.split(",", 1)[1] if data.startswith("data:") else data
        return np.array(Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB"))
    except Exception:
        return None


def load_module(module_path: str):
    """按 'solvers.attr.attr_cloth' / 'solvers/attr/attr_cloth.py' 形式导入"""
    import importlib
    if not module_path:
        return None
    name = module_path.replace("/", ".").removesuffix(".py")
    try:
        return importlib.import_module(name)
    except Exception:
        try:
            return importlib.import_module(name.split(".")[-1])
        except Exception:
            return None


# 题型 → (模块路径, 调用方式)  —— 与 solvers/registry.py 对齐
DISPATCH = {
    "attr_cloth":          ("solvers.attr.attr_cloth", "canvas_or_tiles"),
    "attr_zipper":         ("solvers.attr.attr_zipper", "canvas_or_tiles"),
    "grid_assoc":          ("solvers.grid.grid_assoc", "canvas_or_tiles"),
    "attr_light":          ("solvers.attr.attr_light", "canvas_or_tiles"),
    "chars_line_single":   ("solvers.chars.chars_line_single", "canvas_prompt"),
    "attr_closable":       ("solvers.attr.attr_closable", "canvas"),
    "chars_pair":          ("solvers.chars.chars_pair", "canvas"),
    "missing_link":        ("solvers.spatial.missing_link", "canvas"),
    "mirror":              ("solvers.spatial.mirror", "canvas"),
    "roll":                ("solvers.spatial.roll", "canvas"),
    "drag_bottle":         ("solvers.drag.drag_bottle", "canvas_prompt"),
    "puzzle_wrong":        ("solvers.spatial.puzzle_wrong", "canvas"),
    # 未归档(暂交 refresh)
    "missing_spot":        (None, "refresh"),
    "grid_sample":         (None, "refresh"),
    "chars_lattice":       (None, "refresh"),   # chars 家族在 hc_cv 内, 见 T_SOLVERS
}

# hc_cv.solve(canvas, prompt) 直接覆盖的题型
CHARS_LINE = {"chars_line_pair", "chars_line_triple"}   # single 已由归档模块处理
CV_TYPES = CHARS_LINE | {"chars_lattice", "chars_pair", "shapes", "grid_triple", "chars_odd"}
# hc_drag.solve_drag(canvas, prompt) 覆盖的拖拽家族
DRAG_TYPES = {"drag_bottle", "drag_screw", "drag_shape", "drag_letter", "drag_place",
              "drag_puzzle", "missing_spot", "tower_missing_block", "puzzle_wrong"}
T_SOLVERS = CV_TYPES | DRAG_TYPES


def want_count(prompt: str) -> int:
    """从题面解析期望点击数量: 三个/3/three->3, 两个/2/two->2, 否则 1"""
    t = (prompt or "").lower()
    if "三" in t or "3" in t or "three" in t: return 3
    if "两" in t or "2" in t or "two" in t:   return 2
    return 1


def trim_by_count(res, prompt: str) -> dict:
    """按题面数量裁剪结果(hc_cv 家族恒返回 3 点), 用 scores 排序后取 top-N, 并重算边际 conf"""
    pts = list(getattr(res, "points", None) or [])
    scores = list(getattr(res, "scores", None) or [])
    n = want_count(prompt)
    if not pts: return {"ok": False, "points": [], "conf": 0.0, "reason": getattr(res, "reason", "")}
    if scores and len(scores) == len(pts):
        order = sorted(range(len(pts)), key=lambda i: -float(scores[i]))
        pts = [pts[i] for i in order]; scores = [float(scores[i]) for i in order]
    n = max(1, min(n, len(pts)))
    conf = 0.0
    if scores:
        nxt = scores[n - 1 + 1] if len(scores) >= n + 1 else 0.0
        conf = float(max(0.0, min(1.0, scores[n - 1] - nxt)))
    return {"ok": True, "points": [[float(p[0]), float(p[1])] for p in pts[:n]], "conf": conf,
            "reason": f"{getattr(res, 'reason', '')} [want={n}, ranked={[round(s,3) for s in scores[:4]]}]"}


def normalize(res) -> dict:
    """把各 solver 的返回值统一成 {ok, points, conf, reason, dragFrom}"""
    if res is None:
        return {"ok": False, "points": [], "conf": 0.0, "reason": "solver returned None"}
    if isinstance(res, dict):
        pts = res.get("points") or res.get("clicks") or []
        return {"ok": bool(pts), "points": [[float(p[0]), float(p[1])] for p in pts],
                "conf": float(res.get("confidence", res.get("conf", 0.0)) or 0.0),
                "reason": str(res.get("reason", ""))[:200],
                "dragFrom": res.get("dragFrom") or res.get("src")}
    pts = getattr(res, "points", None) or []
    return {"ok": bool(getattr(res, "ok", bool(pts))), "points": [[float(p[0]), float(p[1])] for p in pts],
            "conf": float(getattr(res, "confidence", 0.0) or 0.0),
            "reason": str(getattr(res, "reason", ""))[:200]}


def solve_request(req: dict) -> dict:
    t0 = time.time()
    prompt = (req.get("prompt") or "").strip()
    kind = req.get("kind") or ""
    typ = prompt_type(prompt)
    canvas = b64_to_rgb(req.get("canvas") or "")
    tiles = [x for x in (b64_to_rgb(t) for t in (req.get("tiles") or [])) if x is not None]
    out = {"type": typ, "prompt": prompt, "action": "refresh", "points": [], "conf": 0.0, "reason": ""}
    if canvas is None and tiles:
        # 宫格题: 只有 9 格 tile → 拼一张 3x3 合成 canvas(每个 tile 缩到 333x233)
        h, w = 940, 1000
        canvas = np.full((h, w, 3), 255, np.uint8)
        for i, t in enumerate(tiles[:9]):
            r_, c_ = divmod(i, 3)
            im = Image.fromarray(t).resize((w // 3, h // 3))
            canvas[r_ * (h // 3):(r_ + 1) * (h // 3), c_ * (w // 3):(c_ + 1) * (w // 3)] = np.array(im)
    if canvas is None:
        out["reason"] = "no canvas & no tiles"
        return out
    panel = canvas[hc_cv.PANEL_Y0:] if hasattr(hc_cv, "PANEL_Y0") else canvas[240:]
    try:
        if typ in CV_TYPES:                       # chars / shapes / grid_triple / chars_pair
            res = hc_cv.solve(canvas, prompt)
            n = trim_by_count(res, prompt) if (typ in CHARS_LINE or typ in ("chars_lattice", "chars_odd")) else normalize(res)
            out.update({"action": "click" if n["points"] else "refresh", "points": n["points"],
                        "conf": n["conf"], "reason": n["reason"]})
            return out
        if typ in DRAG_TYPES:                     # 拖拽家族
            res = hc_drag.solve_drag(canvas, prompt)
            n = normalize(res)
            out.update({"action": "drag" if (n.get("dragFrom") or n["points"]) else "refresh",
                        "points": n["points"], "conf": n["conf"], "reason": n["reason"]})
            if n.get("dragFrom"): out["dragFrom"] = n["dragFrom"]
            return out
        mod_path, how = DISPATCH.get(typ, (None, "refresh"))
        if mod_path is None:
            out["reason"] = f"{typ}: 无求解器 → refresh"
            return out
        mod = load_module(mod_path)
        if mod is None:
            out["reason"] = f"{typ}: 模块 {mod_path} 导入失败"
            return out
        if how == "canvas_or_tiles" and tiles:
            res = mod.solve(tiles)
        elif how == "canvas_prompt":
            res = mod.solve(canvas, prompt=prompt)
        else:
            res = mod.solve(canvas)
        n = normalize(res)
        pts = n["points"]
        if pts and tiles and all(abs(p[0]) < 9 and abs(p[1]) < 9 for p in pts):
            # 求解器返回的是格索引 → 换算为合成 canvas 的格心
            H, W = canvas.shape[:2]
            grid = []
            for p_ in pts:
                i = int(p_[0]) * 3 + int(p_[1]) if p_[1] < 9 else int(p_[0])
                r_, c_ = divmod(int(i), 3)
                grid.append([c_ * (W // 3) + W // 6, r_ * (H // 3) + H // 6])
            pts = grid
            out["indices"] = [int(p_[0]) * 3 + int(p_[1]) for p_ in n["points"]]
        out.update({"action": "click" if pts else "refresh", "points": pts,
                    "conf": n["conf"], "reason": n["reason"]})
        if n.get("dragFrom"):
            out["action"] = "drag"
            out["dragFrom"] = n["dragFrom"]
    except Exception as e:
        out["reason"] = f"{typ}: {type(e).__name__}: {e}"
        out["trace"] = traceback.format_exc()[-400:]
    out["ms"] = int((time.time() - t0) * 1000)
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):       # 静音
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(200, {"ok": True})

    def do_GET(self):
        if self.path.startswith("/health"):
            rows = []
            if registry:
                try:
                    rows = [{"type": m.type_name, "status": m.status, "acc": m.accuracy} for m in registry.REGISTRY.values()]
                except Exception:
                    rows = []
            self._send(200, {"ok": True, "types": len(DISPATCH), "solvers": rows[:40]})
        else:
            self._send(404, {"ok": False})

    def do_POST(self):
        if not self.path.startswith("/solve"):
            self._send(404, {"ok": False}); return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            self._send(400, {"ok": False, "error": str(e)}); return
        try:
            res = solve_request(req)
        except Exception as e:
            res = {"ok": False, "action": "refresh", "error": f"{type(e).__name__}: {e}"}
        self._send(200, res)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8723)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"求解服务已启动: http://{a.host}:{a.port}   (Ctrl+C 退出)")
    print(f"已接题型 {len(DISPATCH)} 个; hc_cv 家族: {sorted(T_SOLVERS)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
