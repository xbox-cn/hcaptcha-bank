#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NoneCap 纯协议取 KEY(零浏览器) —— 已实测打通
1) GET  /login        抽 $ACTION_* hidden -> login action id
2) POST /login        multipart + Next-Action:<login_id>  -> nc_session
3) GET  /keys         抽 key_id
4) 从页面内 JS chunk 挖 Server Action id(createServerReference / 40位hex)
5) POST /keys         Next-Action:<reveal_id> + Content-Type: text/plain + body ["<key_id>"]
                      -> {"key": "nc_live_..."}  ✓
用法:
  python nonecap_key.py --email a@b.com --password 'xxx' [--proxy http://127.0.0.1:7890] [--json out.json]
"""
import argparse, json, os, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
try:
    import requests
except ImportError:
    print("需要 requests: pip install requests"); sys.exit(2)

BASE = "https://dashboard.nonecap.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
PROXY = ""

RE_HEX40 = re.compile("([0-9a-f]{40,})")
RE_CHUNK = re.compile("/_next/static/chunks/[A-Za-z0-9_.~-]+[.]js")
RE_KEYID = re.compile("key_[0-9A-Z]{20,}")
RE_KEY   = re.compile("nc_live_[A-Za-z0-9_-]{8,}")
RE_SREF  = re.compile("createServerReference[)]?[(][ ]*[\x22\x27]([0-9a-f]{40,})")
RE_HIDDEN = re.compile("<input[^>]*name=[\x22]([^\x22]*[$]ACTION[^\x22]*)[\x22][^>]*value=[\x22]([^\x22]*)[\x22]")
RE_HIDDEN2 = re.compile("<input[^>]*value=[\x22]([^\x22]*)[\x22][^>]*name=[\x22]([^\x22]*[$]ACTION[^\x22]*)[\x22]")

def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    if PROXY:
        s.proxies = {"http": PROXY, "https": PROXY}
    return s

def parse_hidden(html):
    out = {}
    for m in RE_HIDDEN.finditer(html):  out[m.group(1)] = m.group(2)
    for m in RE_HIDDEN2.finditer(html): out[m.group(2)] = m.group(1)
    return out

def login(s, email, pw):
    r = s.get(BASE + "/login", timeout=30)
    hid = parse_hidden(r.text)
    aid = None
    for k, v in hid.items():
        if k.endswith("$ACTION_1:0"):
            try: aid = json.loads(v.replace("&quot;", chr(34))).get("id")
            except Exception: pass
    if not aid:
        m = RE_HEX40.search(r.text); aid = m.group(1) if m else None
    print("  [login] GET %s  action_id=%s" % (r.status_code, aid))
    print("  [login] hidden: %s" % list(hid.keys()))
    if not aid:
        print("  [login] 取不到 action id ✗"); return False
    files = []
    for k, v in hid.items():
        if "ACTION_REF" in k:   files.append((k, (None, "")))
        elif "ACTION_1:0" in k: files.append((k, (None, v)))
        elif "ACTION_1:1" in k: files.append((k, (None, v)))
        elif "ACTION_KEY" in k: files.append((k, (None, v)))
    if not files:
        files = [("_1_$ACTION_REF_1", (None, "")),
                 ("_1_$ACTION_1:0", (None, json.dumps({"id": aid, "bound": "$@1"}))),
                 ("_1_$ACTION_1:1", (None, "[{}]"))]
    files.append(("_1_email", (None, email)))
    files.append(("_1_password", (None, pw)))
    files.append(("0", (None, '[{},"$K1"]')))
    h = {"Next-Action": aid, "Accept": "text/x-component",
         "Origin": BASE, "Referer": BASE + "/login"}
    r2 = s.post(BASE + "/login", files=files, headers=h, timeout=40)
    ok = "nc_session" in s.cookies.get_dict()
    print("  [login] POST %s  session=%s  cookies=%s" % (r2.status_code, "OK" if ok else "FAIL", list(s.cookies.get_dict())))
    return ok

def list_keys(s):
    r = s.get(BASE + "/keys", timeout=30)
    ids = sorted(set(RE_KEYID.findall(r.text)))
    print("  [keys] GET %s  找到 key_id: %s" % (r.status_code, ids[:3]))
    return ids, r.text

def action_ids(s, html):
    """只从 JS chunk 派生(页面里的 hex 多是 create/其他 action, 会改状态)"""
    ids = []
    def add(x):
        if x and x not in ids: ids.append(x)
    for c in sorted(set(RE_CHUNK.findall(html)))[:25]:
        try: js = s.get(BASE + c, timeout=25).text
        except Exception: continue
        for m in RE_SREF.finditer(js): add(m.group(1))
        for m in RE_HEX40.finditer(js): add(m.group(1))
    return ids

def reveal(s, key_id, html):
    cands = action_ids(s, html)
    print("  [reveal] action id 候选 %d 个" % len(cands))
    for aid in cands:
        # 每次尝试前重新取当前 key_id(有些 action 会新建 key 改变列表)
        rk = s.get(BASE + "/keys", timeout=25)
        cur = sorted(set(RE_KEYID.findall(rk.text)))
        kid = cur[0] if cur else key_id
        h = {"Next-Action": aid, "Accept": "text/x-component", "Origin": BASE,
             "Referer": BASE + "/keys", "Content-Type": "text/plain;charset=UTF-8"}
        try:
            r = s.post(BASE + "/keys", data=json.dumps([kid]), headers=h, timeout=30)
        except Exception as e:
            print("     %s… 异常 %s" % (aid[:12], str(e)[:60])); continue
        body = r.text or ""
        m = RE_KEY.search(body)
        if m:
            print("  [reveal] OK aid=%s… %s  key_id=%s" % (aid[:12], r.status_code, kid))
            return m.group(0)
        print("     %s… %s len=%d kid=%s %s" % (aid[:12], r.status_code, len(body), kid[-8:], body[:60].replace(chr(10), " ")))
    print("  [reveal] 全部候选失败")
    return None

def main():
    global PROXY
    ap = argparse.ArgumentParser()
    ap.add_argument("--email"); ap.add_argument("--password")
    ap.add_argument("--json"); ap.add_argument("--proxy", default=os.environ.get("NONECAP_PROXY", ""))
    a = ap.parse_args()
    PROXY = a.proxy
    email, pw = a.email, a.password
    if not email:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys", "API_KEYS.txt")
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                if line.strip() and not line.startswith("#"):
                    parts = [x.strip() for x in line.split("|")]
                    email, pw = parts[1], parts[2]; break
    if not email or not pw:
        print("缺账号: --email/--password"); sys.exit(2)
    print("账号:", email)
    s = session()
    if not login(s, email, pw):
        print("登录失败 ✗"); sys.exit(1)
    ids, html = list_keys(s)
    if not ids:
        print("未找到 key_id ✗"); sys.exit(1)
    key = reveal(s, ids[0], html)
    if not key:
        print("Reveal 失败 ✗"); sys.exit(1)
    print("")
    print("=== KEY = %s ===" % key)
    if a.json:
        json.dump({"email": email, "password": pw, "key": key, "key_id": ids[0]},
                  open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("已存", a.json)
    print("::result::email=%s key=%s" % (email, key))
    return 0

if __name__ == "__main__":
    sys.exit(main() or 0)
