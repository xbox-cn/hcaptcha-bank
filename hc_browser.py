"""统一浏览器启动层: CloakBrowser (Stealth Chromium, 默认) / Camoufox (旧后端)

cloak 关键点 (对应 OutlookRegister 项目的环境):
  - geoip=True        : 按代理出口 IP 自动设 locale/timezone + WebRTC 出口 IP 伪装 (需 geoip2, 已装)
  - locale/timezone   : 可显式指定 (优先于 geoip)
  - humanize=True     : windmouse 级人类化鼠标/键盘/滚动 (Patchright 后端)
  - backend           : 'patchright' | 'playwright'
  - viewport=None     : 不模拟视口 (用真实窗口大小) + --window-size=W,H
  - launch_persistent_context(user_data_dir): 持久化 profile (带历史, 比全新 profile 更像真人)

用法:
    from hc_browser import open_browser
    with open_browser(engine="cloak", proxy="http://127.0.0.1:7890", locale="en-US",
                      profile_dir="recon/profiles/steam01", humanize=True) as ctx:
        page = ctx.new_page()
        ...
"""
from __future__ import annotations

from pathlib import Path


def open_browser(engine: str = "cloak", headless: bool = False, proxy: str | None = None, direct: bool = False,
                 locale: str | None = None, timezone: str | None = None, profile_dir: str | None = None,
                 humanize: bool = False, backend: str = "patchright", window: tuple[int, int] | None = (1920, 1080),
                 user_agent: str | None = None, viewport: dict | None = None,
                 camoufox_kwargs: dict | None = None, **kw):
    """返回可直接 `with` 使用的上下文 (BrowserContext/浏览器包装), 具备 new_page()"""
    eng = (engine or "cloak").lower()
    if eng.startswith("cloak"):
        import cloakbrowser as cb
        # 视口 = 窗口内区 (humanize 层需要 viewport; 不能为 None)
        if viewport is None and window:
            viewport = {"width": max(1024, int(window[0]) - 13), "height": max(640, int(window[1]) - 126)}
        if direct:
            proxy = {"server": "direct://"}          # 强制直连 (绕过系统代理)
        common: dict = dict(headless=headless, backend=backend, humanize=humanize,
                            proxy=(proxy or None), geoip=(bool(proxy) and not direct) and not (locale or timezone),
                            locale=locale or None, timezone=timezone or None,
                            viewport=(viewport if not headless else viewport))
        args = list(kw.pop("args", []) or [])
        if direct:
            args.append("--no-proxy-server")
        if window:
            args.append(f"--window-size={int(window[0])},{int(window[1])}")
        if args:
            common["args"] = args
        if user_agent:
            common["user_agent"] = user_agent
        common.update(kw)
        if profile_dir:
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            return cb.launch_persistent_context(str(profile_dir), **common)
        return cb.launch_context(**common)
    if eng.startswith("camoufox"):
        from camoufox.sync_api import Camoufox
        ck = dict(camoufox_kwargs or {})
        ck.setdefault("headless", headless)
        if proxy:
            ck.setdefault("proxy", {"server": proxy})
            ck.setdefault("geoip", True)
        if locale:
            ck.setdefault("locale", locale)
        return Camoufox(**ck)
    raise ValueError(f"unknown engine {engine!r}")


def probe_geo(proxy: str | None, timeout: float = 12.0) -> dict:
    """通过代理查出口 IP 的国家/时区/语言 (给需要显式 locale/timezone 的场景用)"""
    import json
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy})) if proxy \
        else urllib.request.build_opener()
    try:
        with opener.open("http://ip-api.com/json/?fields=status,countryCode,timezone,query", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return d if d.get("status") == "success" else {}
    except Exception:
        return {}
