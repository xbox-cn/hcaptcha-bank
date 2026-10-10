#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单个账号: 临时邮箱 → camoufox 注册(过 Turnstile) → 邮箱验证 → 协议登录取 key
输出: /tmp/key.json  +  打印 KEY=...
在 GA runner 上以 xvfb-run 调用(必须有显示器 ✓ Turnstile 才稳)
"""
import importlib.util, json, os, re, subprocess, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("nk", os.path.join(os.path.dirname(HERE), "nonecap_key.py"))
nk = importlib.util.module_from_spec(spec); spec.loader.exec_module(nk)
MAIL = "https://mail.twcdk.com"; BASE = "https://dashboard.nonecap.com"
PW = os.environ.get("NONECAP_PASSWORD_NEW", "Nonecap!Test2026")

def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace").stdout

def new_mail():
    d = json.loads(sh(["curl","-sS","--max-time","30","-X","POST","-H","Content-Type: application/json",
                       "-d","{}", MAIL+"/api/v1/addresses"]))
    return d["email"], d["token"]

def wait_link(token, tries=30, gap=4):
    for i in range(tries):
        try: data = json.loads(sh(["curl","-sS","--max-time","25", "%s/api/v1/%s/emails" % (MAIL, token)]))
        except Exception: data = None
        items = data if isinstance(data, list) else (data.get("emails") if isinstance(data, dict) else None)
        if items:
            eid = items[0].get("id") or items[0].get("email_id")
            txt = sh(["curl","-sS","--max-time","25", "%s/api/v1/%s/emails/%s" % (MAIL, token, eid)]) if eid else json.dumps(items[0])
            m = re.search(r'https?://dashboard\.nonecap\.com/verify\?token=[A-Za-z0-9._\-]+', txt)
            if m: return m.group(0)
        time.sleep(gap)
    return None

def main():
    from camoufox.sync_api import Camoufox
    email, mtok = new_mail()
    print("邮箱:", email, flush=True)
    br = None; rec = {"email": email, "password": PW, "key": None}
    try:
        ctxm = Camoufox(headless=False, humanize=True, locale="en-US", window=(1400, 920))
        br = ctxm.__enter__()
        ctx = getattr(br, "contexts", None); ctx = ctx[0] if ctx else br
        page = ctx.new_page() if hasattr(ctx, "new_page") else br.pages[0]
        page.goto(BASE + "/signup", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(2500)
        page.fill("#email", email); page.fill("#password", PW)
        try: page.check("input[name=terms]", timeout=8000)
        except Exception as e: print("terms:", str(e)[:60], flush=True)
        tok = ""
        for i in range(25):
            tok = page.evaluate("""() => { const e=document.querySelector('input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]'); return e&&e.value?e.value:""; }""")
            if tok:
                print("Turnstile OK len=%d (第 %ds)" % (len(tok), i*2), flush=True); break
            page.wait_for_timeout(2000)
        if not tok:
            print("Turnstile FAIL ✗", flush=True)
            for f in page.frames:
                if "challenges.cloudflare" in (f.url or ""):
                    try:
                        b = f.locator("body").bounding_box()
                        if b:
                            x, y = b["x"]+20, b["y"]+b["height"]/2
                            page.mouse.move(x-40, y-20); page.wait_for_timeout(150)
                            page.mouse.move(x, y, steps=12); page.mouse.click(x, y)
                            print("  已尝试点复选框", flush=True)
                    except Exception: pass
            page.wait_for_timeout(8000)
            tok = page.evaluate("""() => { const e=document.querySelector('input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]'); return e&&e.value?e.value:""; }""")
            print("  重试后 token:", "OK" if tok else "仍 FAIL", flush=True)
        page.click("button[type=submit]", timeout=15000)
        page.wait_for_timeout(8000)
        body = page.evaluate("() => document.body.innerText.slice(0,500)")
        print("注册后页面:", body.replace(chr(10), " | ")[:280], flush=True)
        link = wait_link(mtok)
        print("验证链接:", "OK" if link else "FAIL", flush=True)
        if not link: sys.exit(2)
        page.goto(link, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)
        print("验证页:", page.evaluate("()=>document.body.innerText.slice(0,120)").replace(chr(10), " ")[:100], flush=True)
        s = nk.session()
        if not nk.login(s, email, PW):
            print("协议登录失败 ✗", flush=True); sys.exit(3)
        ids, html = nk.list_keys(s)
        if not ids: print("无 key_id ✗", flush=True); sys.exit(4)
        key = nk.reveal(s, ids[0], html)
        if not key: print("reveal 失败 ✗", flush=True); sys.exit(5)
        rec["key"] = key; rec["key_id"] = ids[0]
        print("KEY=%s" % key, flush=True)
        print("::result::email=%s key=%s" % (email, key), flush=True)
        # 入库(SQLite, 随 artifact 上传)
        try:
            from store import db, add
            c = db(); add(c, key, email, ids[0], 1300); c.close()
            print("已入库", flush=True)
        except Exception as e:
            print("入库失败(不致命):", str(e)[:80], flush=True)
    finally:
        json.dump(rec, open("/tmp/key.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        try:
            if br: br.close()
        except Exception: pass
    return 0

if __name__ == "__main__":
    sys.exit(main() or 0)
