"""
hCaptcha 题型采样 (通用 URL 版): 打开目标页 → 找 hCaptcha checkbox iframe → 点 → 连续 refresh N 次,
记录每题 prompt / 题型 / crumbs, 保存 canvas 原图 (toDataURL) 或 3x3 格子每格截图 + 示例图,
并嗅探 /getcaptcha 响应 (若为明文 JSON 则直接记录 request_type / requester_question)。

    python hcaptcha_sample.py --url https://store.steampowered.com/join/ --n 12 --out samples_steam
    python hcaptcha_sample.py --n 12 --locale en-US --out samples_en          # 默认官方 demo
"""
import os
import sys
import time
import json
import base64
import io
import argparse
from pathlib import Path
from collections import Counter

import numpy as np
from PIL import Image

from camoufox.sync_api import Camoufox

import hc_cv

for _s in (sys.stdout, sys.stderr):   # Windows 控制台 GBK 编码下打印 emoji/特殊符号不崩
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
DEMO_URL = "https://accounts.hcaptcha.com/demo"
CHALLENGE_IFRAME = "iframe[src*='frame=challenge']"
CHECKBOX_IFRAME = "iframe[src*='frame=checkbox']"
DRAG_WORDS = ("拖", "drag", "拖動", "move the", "place the", "拼块", "放入", "放置", "puzzle", "piece", "into place", "missing")

JS_INFO = """() => {
    const q = (s) => Array.from(document.querySelectorAll(s));
    const cv = document.querySelector('canvas');
    const tiles = q('.task-image');
    const exs = q('.challenge-example .image').map(e => e.style.backgroundImage || getComputedStyle(e).backgroundImage);
    return {
      prompt: q('.prompt-text').map(e => e.innerText.trim()),
      h2: q('h2').map(e => e.innerText.trim()),
      canvas: cv ? {w: cv.width, h: cv.height} : null,
      tiles: tiles.length,
      tile_bg: tiles.slice(0, 2).map(t => { const im = t.querySelector('.image'); return im ? (im.style.backgroundImage || '').slice(0, 100) : null; }),
      submit: q('.button-submit').map(e => ({text: e.innerText.trim(), aria: e.getAttribute('aria-label')})),
      crumbs: q('.Crumb').length,
      examples: exs.filter(s => s && s.includes('url(')).length,
      error: (document.querySelector('.display-error .error-text') || {}).innerText || '',
      error_visible: (() => { const e = document.querySelector('.display-error'); if (!e) return false; const r = e.getBoundingClientRect(); const cs = getComputedStyle(e); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0'; })(),
      lang: (document.querySelector('#display-language') || {}).innerText || '',
    };
}"""

JS_CANVAS_PNG = """() => {
    const cv = document.querySelector('canvas');
    if (!cv) return null;
    try { return cv.toDataURL('image/png'); } catch (e) { return 'ERR:' + e.name + ':' + e.message; }
}"""


def frame_by_url(page, needle):
    for f in page.frames:
        if needle in f.url:
            return f
    return None


def classify(info):
    p = " ".join(info["prompt"]).lower()
    if info["tiles"] >= 9:
        return "grid"
    if info["tiles"]:
        return f"tiles{info['tiles']}"
    if any(k in p for k in DRAG_WORDS):
        return "drag"
    if info["canvas"]:
        return "canvas"
    return "other"


def save_canvas(page, ch, path_png, retries=4):
    """toDataURL; 空白(刚切题还没画完)时等待重试, 避免存下空白样本"""
    data = None
    for _ in range(retries):
        data = ch.evaluate(JS_CANVAS_PNG)
        if data and data.startswith("data:image/png;base64,"):
            raw = base64.b64decode(data.split(",", 1)[1])
            try:
                img = np.array(Image.open(io.BytesIO(raw)).convert("RGB"))
                if not hc_cv.is_blank(hc_cv.to_panel(img)):
                    path_png.write_bytes(raw)
                    return "toDataURL"
            except Exception:
                break
        time.sleep(0.8)
    try:
        page.frame_locator(CHALLENGE_IFRAME).first.locator("canvas").screenshot(path=str(path_png))
        return f"screenshot (retries={retries})"
    except Exception as e:
        return f"failed ({data}; {type(e).__name__})"


def save_tiles(page, folder):
    folder.mkdir(exist_ok=True)
    fl = page.frame_locator(CHALLENGE_IFRAME).first
    n = fl.locator(".task-image").count()
    ok = 0
    for i in range(n):
        try:
            fl.locator(".task-image").nth(i).screenshot(path=str(folder / f"tile_{i}.png"))
            ok += 1
        except Exception:
            pass
    for j in range(fl.locator(".challenge-example .image").count()):
        try:
            fl.locator(".challenge-example .image").nth(j).screenshot(path=str(folder / f"example_{j}.png"))
        except Exception:
            pass
    return f"tiles {ok}/{n}"


def checkbox_checked(page):
    cb = frame_by_url(page, "frame=checkbox")
    if not cb:
        return None
    try:
        return cb.evaluate("() => document.querySelector('#checkbox')?.getAttribute('aria-checked')")
    except Exception:
        return None


def click_checkbox(page):
    page.wait_for_selector(CHECKBOX_IFRAME, timeout=30000)
    iframe_el = page.locator(CHECKBOX_IFRAME).first
    iframe_el.scroll_into_view_if_needed(timeout=10000)
    time.sleep(0.8)
    box = page.frame_locator(CHECKBOX_IFRAME).first.locator("#checkbox")
    box.wait_for(state="visible", timeout=20000)
    bb = box.bounding_box()
    page.mouse.move(bb["x"] + 5, bb["y"] + 5)
    page.mouse.move(bb["x"] + bb["width"] / 2 - 3, bb["y"] + bb["height"] / 2 - 2, steps=6)
    page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)


def page_diag(page, out, tag):
    print(f"   frames ({len(page.frames)}):")
    for f in page.frames:
        print("     ", f.url[:150])
    sk = page.evaluate("() => Array.from(document.querySelectorAll('[data-sitekey]')).map(e => e.getAttribute('data-sitekey'))")
    print(f"   sitekeys: {sk}")
    page.screenshot(path=str(out / f"page_{tag}.png"), full_page=True)



# ---------------------------------------------------------------- 题型配额 (GA 采集用)
# 当某个题型已收集 >= BANK_QUOTA 张图时, 本 shard 不再采集该题型(提前终止)
BANK_QUOTA = int(os.environ.get("BANK_QUOTA", "200"))
BANK_COUNTS = os.environ.get("BANK_COUNTS", "")


def _bank_type(prompt: str) -> str:
    """题型分类: 优先复用 tools/build_bank.py 的规则(单一事实来源), 失败则退化为本文件内置规则"""
    try:
        import sys as _s
        from pathlib import Path as _P
        _s.path.insert(0, str(_P(__file__).resolve().parent / "tools"))
        from build_bank import ptype as _pt
        return _pt(prompt)
    except Exception:
        t = (prompt or "").strip(); L = t.lower()
        if "药瓶" in t or ("拖入" in t and "槽" in t): return "drag_bottle"
        if "布料" in t or "布制" in t: return "attr_cloth"
        if "关上" in t or "关闭" in t or "can be closed" in L: return "attr_closable"
        if "发光" in t or "emit light" in L: return "attr_light"
        if "缺失的连" in t or "断裂处" in t or "missing link" in L: return "missing_link"
        if "两个打破规律" in t: return "chars_pair"
        if "同一类型" in t or "three matching shapes" in L: return "grid_triple"
        return "other"


def _load_counts():
    import json as _j
    try:
        with open(BANK_COUNTS, encoding="utf-8") as f:
            return _j.load(f)
    except Exception:
        return {}


def _quota_ok(prompt, counts, will_add):
    """该题型是否还有配额"""
    t = _bank_type(prompt)
    have = int(counts.get(t, 0)) + will_add.get(t, 0)
    return have < BANK_QUOTA, t, have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEMO_URL)
    ap.add_argument("--proxy", default=os.environ.get("V2_PROXY", "http://127.0.0.1:7890"))
    ap.add_argument("--locale", default=None, help="例如 en-US; 默认跟随 geoip")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--out", default="samples")
    ap.add_argument("--attempts", type=int, default=3, help="checkbox 直接通过(无挑战)时重载重试次数")
    args = ap.parse_args()
    out = HERE / args.out
    out.mkdir(exist_ok=True)
    proxy = args.proxy or None
    bank_counts = _load_counts()
    will_add = {}
    quota_full = set()

    kw = dict(headless=False, humanize=False, i_know_what_im_doing=True,
              config={'forceScopeAccess': True}, disable_coop=True,
              firefox_user_prefs={'widget.windows.window_occlusion_tracking.enabled': False})
    if proxy:
        kw['proxy'] = {"server": proxy}
        kw['geoip'] = True
    if args.locale:
        kw['locale'] = args.locale

    getcaptcha = []

    def on_response(resp):
        u = resp.url
        if "/getcaptcha" in u or "/checkcaptcha" in u:
            rec = {"t": round(time.time() - t0, 1), "url": u.split("?")[0][-60:], "status": resp.status,
                   "ctype": resp.headers.get("content-type", "")}
            try:
                body = resp.text()
                rec["len"] = len(body)
                try:
                    j = json.loads(body)
                    rec["keys"] = sorted(j.keys())[:20] if isinstance(j, dict) else type(j).__name__
                    for k in ("request_type", "requester_question", "pass", "success", "generated_pass_UUID"):
                        if isinstance(j, dict) and k in j:
                            rec[k] = j[k] if k != "generated_pass_UUID" else "<token>"
                    if isinstance(j, dict) and "tasklist" in j:
                        rec["tasklist"] = len(j["tasklist"])
                except Exception:
                    rec["body_head"] = body[:80]
            except Exception as e:
                rec["body_err"] = type(e).__name__
            getcaptcha.append(rec)

    rows = []
    t0 = time.time()
    with Camoufox(**kw) as browser:
        page = browser.new_page()
        page.on("response", on_response)
        got_challenge = False
        for attempt in range(1, args.attempts + 1):
            for _att in range(3):
                try:
                    page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
                    break
                except Exception as _e:
                    print(f"goto retry {_att + 1}/3: {str(_e)[:90]}", flush=True)
                    time.sleep(4)
            time.sleep(3)
            if attempt == 1:
                page_diag(page, out, "loaded")
            try:
                click_checkbox(page)
            except Exception as e:
                print(f"❌ checkbox 点击失败: {type(e).__name__}: {str(e)[:120]}")
                page_diag(page, out, f"fail{attempt}")
                return
            print(f"🖱️ checkbox clicked at {time.time()-t0:.1f}s (attempt {attempt})")
            deadline = time.time() + 15
            while time.time() < deadline:
                ch = frame_by_url(page, "frame=challenge")
                try:
                    if ch and ch.locator(".prompt-text").count() > 0 and ch.locator(".prompt-text").first.is_visible():
                        got_challenge = True
                        break
                except Exception:
                    pass
                if checkbox_checked(page) == "true":
                    break
                time.sleep(0.5)
            if got_challenge:
                break
            print(f"   无挑战直接通过 (checkbox={checkbox_checked(page)}), 重载重试")
            time.sleep(2)
        if not got_challenge:
            print("❌ 多次尝试均未出现挑战")
            return
        time.sleep(1.5)
        for i in range(1, args.n + 1):
            ch = frame_by_url(page, "frame=challenge")
            if not ch:
                print(f"[{i}] no challenge frame; checkbox={checkbox_checked(page)}")
                break
            try:
                ch.wait_for_selector(".prompt-text", timeout=10000)
                info = ch.evaluate(JS_INFO)
            except Exception as e:
                print(f"[{i}] evaluate failed: {type(e).__name__}; checkbox={checkbox_checked(page)}")
                time.sleep(3)
                continue
            prompt_txt = " ".join(info["prompt"]) if isinstance(info["prompt"], list) else str(info["prompt"])
            ok_quota, _bt, _have = _quota_ok(prompt_txt, bank_counts, will_add)
            if not ok_quota:
                print(f"[{i}] 题型 {_bt} 已达配额({_have}>={BANK_QUOTA}), 本 shard 收工")
                quota_full.add(_bt)
                break
            will_add[_bt] = will_add.get(_bt, 0) + 1
            print(f"[{i}] type={_bt} have={_have}<{BANK_QUOTA}")
            kind = classify(info)
            stem = out / f"s{i:02d}_{kind}"
            if kind == "grid" or kind.startswith("tiles"):
                how = save_tiles(page, Path(str(stem)))
            else:
                how = save_canvas(page, ch, Path(str(stem) + ".png"))
                if info["examples"]:
                    try:
                        page.frame_locator(CHALLENGE_IFRAME).first.locator(".challenge-example .image").first.screenshot(path=str(stem) + ".example.png")
                    except Exception:
                        pass
            try:
                page.locator(CHALLENGE_IFRAME).first.screenshot(path=str(stem) + ".frame.png")
            except Exception:
                pass
            gc = getcaptcha[-1] if getcaptcha else {}
            row = {"i": i, "kind": kind, "prompt": info["prompt"], "h2": info["h2"], "canvas": info["canvas"], "tiles": info["tiles"],
                   "tile_bg": info["tile_bg"], "submit": info["submit"], "crumbs": info["crumbs"], "examples": info["examples"],
                   "lang": info["lang"], "error": info["error"], "error_visible": info["error_visible"], "saved": how, "request_type": gc.get("request_type"), "requester_question": gc.get("requester_question")}
            rows.append(row)
            rt = f" rt={gc.get('request_type')}" if gc.get("request_type") else ""
            err = f" err={info['error']!r}/{info['error_visible']}" if info['error'] else ""
            print(f"[{i}] {kind:7s} crumbs={info['crumbs']} ex={info['examples']} tiles={info['tiles']} submit={[s['text'] for s in info['submit']]} save={how[:14]}{rt}{err} prompt={info['prompt']}")
            if i < args.n:
                try:
                    ch.locator(".refresh").first.click(timeout=5000)
                except Exception as e:
                    print(f"   refresh failed: {type(e).__name__}")
                    break
                time.sleep(3.5)
    (out / "samples.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "getcaptcha.json").write_text(json.dumps(getcaptcha, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n题型分布: {dict(Counter(r['kind'] for r in rows))}  带示例图: {sum(1 for r in rows if r['examples'])}  耗时 {time.time()-t0:.0f}s")
    if getcaptcha:
        g = getcaptcha[0]
        print(f"getcaptcha 响应: {len(getcaptcha)} 次, 首个 status={g.get('status')} len={g.get('len')} keys={g.get('keys', g.get('body_head', g.get('body_err')))}")


if __name__ == "__main__":
    main()
