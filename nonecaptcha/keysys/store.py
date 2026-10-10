#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NoneCap API KEY 管理系统(单文件, SQLite)
功能: 上传key(add) / 取key(alloc, 最少用量优先) / 标记key(mark) / 用量记录(usage) / 统计(stats)
状态: active | disabled | exhausted | invalid
用法:
  python store.py add    --key nc_live_xxx --email a@b.com [--key-id key_01...] [--credits N]
  python store.py alloc  [--n 1] [--strategy least-used|round-robin|first]
  python store.py mark   --key nc_live_xxx --status exhausted [--note '原因']
  python store.py usage  --key nc_live_xxx --cost 12.5 [--detail 'SOLVE succeed']
  python store.py stats
  python store.py list   [--status active] [--limit 20]
"""
import argparse, json, os, sqlite3, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DB = os.environ.get("NONECAP_KEYDB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys.db"))

DDL = """
CREATE TABLE IF NOT EXISTS accounts(
  email TEXT PRIMARY KEY, password TEXT, credits INTEGER DEFAULT 0,
  created TEXT, status TEXT DEFAULT 'active', note TEXT);
CREATE TABLE IF NOT EXISTS keys(
  key TEXT PRIMARY KEY, key_id TEXT, email TEXT, created TEXT,
  status TEXT DEFAULT 'active', credits_used REAL DEFAULT 0, used_count INTEGER DEFAULT 0,
  last_used TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS usage_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, key TEXT, cost REAL, detail TEXT);
"""

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.executescript(DDL); return c

def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def add(c, key, email=None, key_id=None, credits=None, note=None):
    c.execute("INSERT OR REPLACE INTO keys(key,key_id,email,created,status,credits_used,used_count) VALUES(?,?,?,?,COALESCE((SELECT status FROM keys WHERE key=?),'active'),COALESCE((SELECT credits_used FROM keys WHERE key=?),0),COALESCE((SELECT used_count FROM keys WHERE key=?),0))",
              (key, key_id, email, now(), key, key, key))
    if email and credits is not None:
        c.execute("INSERT OR REPLACE INTO accounts(email,password,credits,created) VALUES(?,COALESCE((SELECT password FROM accounts WHERE email=?),NULL),?,COALESCE((SELECT created FROM accounts WHERE email=?),?))",
                  (email, email, credits, email, now()))
    if note: c.execute("UPDATE keys SET note=? WHERE key=?", (note, key))
    c.commit()

def add_account(c, email, password, credits=None):
    c.execute("INSERT OR REPLACE INTO accounts(email,password,credits,created) VALUES(?,?,COALESCE(?,0),COALESCE((SELECT created FROM accounts WHERE email=?),?))",
              (email, password, credits, email, now()))
    c.commit()

def alloc(c, n=1, strategy="least-used", status="active"):
    q = "SELECT * FROM keys WHERE status=?" 
    if strategy == "least-used": q += " ORDER BY credits_used ASC, used_count ASC, created ASC"
    elif strategy == "round-robin": q += " ORDER BY COALESCE(last_used,'') ASC"
    else: q += " ORDER BY created ASC"
    rows = c.execute(q + " LIMIT ?", (status, n)).fetchall()
    return [dict(r) for r in rows]

def mark(c, key, status, note=None):
    c.execute("UPDATE keys SET status=?, note=COALESCE(?,note) WHERE key=?", (status, note, key))
    c.commit(); return c.total_changes

def usage(c, key, cost=0.0, detail=None):
    c.execute("INSERT INTO usage_log(ts,key,cost,detail) VALUES(?,?,?,?)", (now(), key, cost, detail))
    c.execute("UPDATE keys SET credits_used=credits_used+?, used_count=used_count+1, last_used=? WHERE key=?", (cost, now(), key))
    c.commit()

def stats(c):
    k = c.execute("SELECT status, COUNT(*) n, SUM(credits_used) used FROM keys GROUP BY status").fetchall()
    a = c.execute("SELECT COUNT(*) n, SUM(credits) cr FROM accounts").fetchone()
    u = c.execute("SELECT COUNT(*) n, SUM(cost) cost FROM usage_log").fetchone()
    return {"keys_by_status": {r["status"]: r["n"] for r in k},
            "credits_used_total": sum((r["used"] or 0) for r in k),
            "accounts": a["n"], "accounts_credits": a["cr"] or 0,
            "usage_calls": u["n"] or 0, "usage_cost": u["cost"] or 0}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["add","alloc","mark","usage","stats","list","add-account"])
    ap.add_argument("--key"); ap.add_argument("--email"); ap.add_argument("--password")
    ap.add_argument("--key-id"); ap.add_argument("--credits", type=float)
    ap.add_argument("--status"); ap.add_argument("--note"); ap.add_argument("--detail")
    ap.add_argument("--cost", type=float, default=0.0); ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--strategy", default="least-used"); ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(); c = db()
    if a.cmd == "add":           add(c, a.key, a.email, a.key_id, a.credits, a.note); print("added", a.key)
    elif a.cmd == "add-account": add_account(c, a.email, a.password, a.credits); print("account added", a.email)
    elif a.cmd == "alloc":
        rows = alloc(c, a.n, a.strategy, a.status or "active")
        for r in rows: c.execute("UPDATE keys SET last_used=? WHERE key=?", (now(), r["key"])); c.commit()
        print(json.dumps([r["key"] for r in rows] if not a.json else rows, ensure_ascii=False, indent=1))
    elif a.cmd == "mark":        print("marked", mark(c, a.key, a.status, a.note))
    elif a.cmd == "usage":       usage(c, a.key, a.cost, a.detail); print("logged")
    elif a.cmd == "stats":       print(json.dumps(stats(c), ensure_ascii=False, indent=1))
    elif a.cmd == "list":
        q = "SELECT key,status,email,credits_used,used_count,created FROM keys"
        if a.status: q += " WHERE status=?"
        rows = c.execute(q + " ORDER BY created DESC LIMIT ?", ((a.status,) if a.status else ()) + (a.limit,)).fetchall()
        for r in rows: print("  %-46s %-10s %-26s used=%.1f calls=%d" % (r["key"], r["status"], r["email"] or "-", r["credits_used"], r["used_count"]))
if __name__ == "__main__": main()
