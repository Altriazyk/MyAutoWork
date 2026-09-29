"""用浏览器的调试协议（CDP）操作网页。

**为什么走 CDP 而不是 Selenium / Playwright。** 那两个都要额外装一个驱动或者一整个
浏览器（几百兆），而且 Playwright 的浏览器和用户平时用的那个是两份，登录状态不共享。
CDP 是 Chromium **自带的**协议，只要浏览器带 ``--remote-debugging-port`` 启动就能连 ——
用的是**用户自己的浏览器和登录状态**，这对自动化来说是决定性的：很多网站要登录才看得见。

**为什么不用界面自动化去点浏览器。** 那条路（``win.uia`` + 找图）能用，但网页的 DOM
一变就失效，而且拿不到"页面里到底有什么"。CDP 直接问页面本身，稳得多也快得多。

**连接是常驻的。** 插件进程本身是长驻的，所以连上之后 ``open`` / ``click`` / ``read``
这些动作共用同一个连接，不用每个节点重连一次。连接挂在 ``ctx.on_cleanup`` 上 ——
插件被卸载时会把它关掉，否则那个 WebSocket 会一直被占着。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from myautowork import (
    Bool,
    Context,
    Enum,
    File,
    Integer,
    Number,
    String,
    Text,
    action,
)

__all__ = [
    "launch", "open_", "js", "click", "type_text", "read", "screenshot",
    "cookies", "record_start", "record_dump", "record_stop", "close",
]

#: ``Page.addScriptToEvaluateOnNewDocument`` 返回的编号，停录制时要拿它撤掉。
_INJECTION: str | None = None


def _recorder_source() -> str:
    # 路径在这里现算：``PLUGIN_DIR`` 定义在文件更靠下的位置，写成模块级常量会在
    # 导入时 NameError。
    path = PLUGIN_DIR / "recorder.js"
    if not path.is_file():
        raise FileNotFoundError(f"录制脚本不见了：{path}")
    return path.read_text(encoding="utf-8")

#: 默认调试端口。Edge / Chrome 都认这个参数。
DEFAULT_PORT = 9222

#: 找浏览器的顺序。Edge 在 Windows 上是必装的，所以作为兜底。
_CANDIDATES = (
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)

#: 当前连着的浏览器。插件进程是长驻的，连上就一直用。
_BROWSER: "_Browser | None" = None


#: 插件自己所在的目录。配置目录默认放在它下面。
PLUGIN_DIR = Path(__file__).resolve().parent


def _default_profile() -> Path:
    """自动启动的浏览器用哪个配置目录。

    **放在插件自己里面**（``plugins/web.browser/.profile/``），这样它是自包含的：
    删插件时登录状态跟着一起删，把插件目录拷到别的机器上也带着登录状态。

    **必须是固定的、不会被系统清理的位置。** 登录状态（cookie、localStorage、会话）
    全在这个目录里 —— 放 ``%TEMP%`` 的话，系统清一次临时文件，用户就得重新登录一遍，
    而现象是"昨天还好好的，今天怎么退出了"，完全联想不到是临时目录被清了。

    代价：它会涨到几百兆，而且**属于插件的目录会变得很大**。不想要的话在
    「配置目录」里填别的地方。
    """
    profile = PLUGIN_DIR / ".profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _find_browser(explicit: str = "") -> Path:
    if explicit.strip():
        path = Path(explicit.strip().strip('"'))
        if path.is_file():
            return path
        raise FileNotFoundError(f"找不到这个浏览器：{path}")

    for pattern in _CANDIDATES:
        expanded = Path(os.path.expandvars(pattern))
        if expanded.is_file():
            return expanded
    raise FileNotFoundError(
        "没找到 Edge 或 Chrome。装一个，或者在「浏览器路径」里直接填完整路径"
    )


class _Browser:
    """一个 CDP 连接。

    ``websocket-client`` 是同步的，够用而且简单 —— 这是个自动化动作，不是服务器，
    不需要 asyncio 那一套。
    """

    def __init__(self, port: int, ws_url: str, proc: subprocess.Popen | None = None) -> None:
        import websocket  # noqa: PLC0415 - 只在真正连的时候才需要

        self.port = port
        self.proc = proc
        # **关掉代理**：本机调试端口走代理会连不上，而且失败信息很难懂。
        #
        # **``suppress_origin=True`` 是必须的。** Chromium 从 v111 起会拒绝带 ``Origin``
        # 头的 WebSocket 连接（防的是"网页里藏一段脚本去连你本机的调试端口"），
        # 表现是握手直接 403，报错让人一愣。websocket-client 默认会发这个头。
        #
        # 用"不发 Origin"而不是"启动时加 ``--remote-allow-origins=*``"，
        # 是因为**连用户自己手动启动的浏览器时，我们加不了命令行参数** ——
        # 那种情况只有这条路走得通。
        self.ws = websocket.create_connection(
            ws_url,
            timeout=30.0,
            http_proxy_host=None,
            http_proxy_port=None,
            suppress_origin=True,
        )
        self._seq = 0

    # -- CDP 调用 -------------------------------------------------------------

    def send(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0):
        self._seq += 1
        message_id = self._seq
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))

        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"{method} 等了 {timeout:g} 秒还没回应")
            raw = self.ws.recv()
            if not raw:
                raise ConnectionError("浏览器把连接关了")
            data = json.loads(raw)
            # 事件（没有 id）会在命令回应之间插进来，跳过它们。
            if data.get("id") != message_id:
                continue
            if "error" in data:
                error = data["error"]
                raise RuntimeError(f"{method} 失败：{error.get('message') or error}")
            return data.get("result") or {}

    def evaluate(self, expression: str, *, timeout: float = 30.0) -> Any:
        """在页面里跑一段 JS，返回它的值。

        ``awaitPromise`` 打开是为了能直接用 async 写法 —— 很多取值要等网络。
        """
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise RuntimeError(f"页面里的这段 JS 报错了：{text}")
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def _http_json(port: int, path: str, *, timeout: float = 2.0) -> Any:
    """问调试端口要一份 JSON（``/json/version``、``/json/list`` 这些）。"""
    url = f"http://127.0.0.1:{port}{path}"
    # **一律绕开代理。** 系统代理开着的时候，对本机端口的请求也会被塞进代理，
    # 表现是"连不上调试端口"，而那个错误完全指不到代理头上。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _port_alive(port: int) -> bool:
    try:
        _http_json(port, "/json/version")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return True


def _pick_target(targets: list[dict[str, Any]], prefer: str = "") -> dict[str, Any] | None:
    """从调试端口给出的标签页里挑一个。

    **不能无脑取第一个。** 浏览器自己会开页面 —— Edge 首次启动会弹一个
    ``edge://sync-confirmation-dialog/``，它排在列表前面，于是"连上了"但连的是
    浏览器的对话框。表现是启动成功、后面所有动作都在一个空页面上做，什么都不报错。
    """
    pages = [
        t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
    ]
    if not pages:
        return None

    if prefer:
        host = urlparse(prefer).netloc
        if host:
            for target in pages:
                if host in str(target.get("url") or ""):
                    return target

    # 退一步：挑一个不是浏览器自己那个界面的。
    for target in pages:
        url = str(target.get("url") or "")
        if not url.startswith(("edge://", "chrome://", "devtools://", "about:")):
            return target
    return pages[0]


def _connect(port: int, *, prefer: str = "", timeout: float = 15.0) -> "_Browser":
    """连到调试端口上的一个标签页。端口没起来就等一会儿。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            targets = _http_json(port, "/json/list")
        except Exception:
            time.sleep(0.3)
            continue
        picked = _pick_target(targets, prefer)
        if picked is not None:
            return _Browser(port, picked["webSocketDebuggerUrl"])
        time.sleep(0.3)
    raise TimeoutError(
        f"{timeout:g} 秒内没连上调试端口 {port}。浏览器起来了吗？"
        "如果它本来就在跑，看下面的说明 —— 已经在运行的浏览器不会开这个端口"
    )


#: cookie 串里这些是**属性**，不是 cookie。从 DevTools 复制 `document.cookie` 时不会有它们，
#: 但直接复制响应头里的 `Set-Cookie` 就会有 —— 不排掉的话会凭空多出几个叫 Path、
#: Expires 的垃圾 cookie，而且它们看起来还挺像那么回事。
_COOKIE_ATTRS = {
    "path", "domain", "expires", "max-age", "secure", "httponly",
    "samesite", "priority", "partitioned",
}


def _apply_cookie_string(browser: "_Browser", url: str, raw: str) -> tuple[int, list[str]]:
    """把 ``a=b; c=d`` 这样的串写进浏览器。返回 ``(成功几个, 失败说明)``。

    **必须在跳转之前调用。** 先打开页面再设 cookie 的话，页面已经带着未登录的状态
    加载完了 —— 你会看到一个登录页，然后 cookie 才生效，得刷新一下才正常。
    这个顺序问题表现得很像"cookie 没设上"。
    """
    browser.send("Network.enable")

    ok = 0
    problems: list[str] = []
    # 换行也当分隔符 —— 从别处粘贴过来经常是一行一个。
    for piece in raw.replace("\r", "").replace("\n", ";").split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        name, _, value = piece.partition("=")
        name, value = name.strip(), value.strip()
        if not name or name.casefold() in _COOKIE_ATTRS:
            continue
        try:
            result = browser.send(
                "Network.setCookie", {"name": name, "value": value, "url": url}
            )
        except Exception as exc:  # noqa: BLE001 - 单个失败不该让整条都失败
            problems.append(f"{name}：{exc}")
            continue
        if result.get("success"):
            ok += 1
        else:
            problems.append(f"{name}：浏览器拒绝了（域对得上吗？HttpOnly 的伪造不了）")

    if not ok and not problems:
        problems.append(f"这一段里没解析出任何 cookie：{raw.strip()[:60]!r}")
    return ok, problems


def _alive(browser: "_Browser", *, timeout: float = 2.0) -> bool:
    """这个连接现在还能用吗。

    **必须问一句。** 用户关掉浏览器之后，那个 WebSocket 就死了，但插件手里的引用
    还是非 None —— 它不知道自己拿的是个死连接。以前的表现是下次「启动浏览器」走进
    "复用已经连上的"那条分支，然后对着死连接说话，报出底层那句
    "主机关闭连接"。**那句话完全指不到"浏览器已经被关了"这个事实上。**

    走一次最便宜的 CDP 调用（求值 `1`）来判断。本机往返，代价可以忽略。
    """
    try:
        if not getattr(browser.ws, "connected", False):
            return False
        browser.send("Runtime.evaluate", {"expression": "1", "returnByValue": True}, timeout=timeout)
        return True
    except Exception:
        return False


def _close_current() -> None:
    """把当前连接关掉。给清理钩子用 —— 传函数而不是绑定的方法，
    因为连接会被换掉（断了之后重连），绑定方法会指向那个旧的。
    """
    global _BROWSER
    browser, _BROWSER = _BROWSER, None
    if browser is not None:
        browser.close()


def _current() -> "_Browser":
    global _BROWSER

    if _BROWSER is None:
        raise RuntimeError(
            "还没连上浏览器。先用「启动浏览器」那个动作 —— "
            "如果之前连过，多半是浏览器被关掉了"
        )
    if not _alive(_BROWSER):
        # **顺手清掉。** 留着的话下一次还是同一句看不懂的报错；
        # 清掉之后「启动浏览器」会走重新连接那条路，自己就好了。
        _BROWSER = None
        raise RuntimeError(
            "和浏览器的连接已经断了 —— 多半是浏览器被关掉了。\n"
            "重新跑一次「启动浏览器」就能接上（它会重新启动浏览器并连上）"
        )
    return _BROWSER


def _wait_selector(browser: "_Browser", selector: str, timeout: float) -> None:
    """等某个元素出现。**用轮询而不是 MutationObserver** —— 轮询失败时能给出
    "等了多少秒、页面上现在有什么"，观察者只能给一句超时。"""
    escaped = json.dumps(selector)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = browser.evaluate(f"!!document.querySelector({escaped})")
        if found:
            return
        time.sleep(0.2)

    title = browser.evaluate("document.title")
    body = browser.evaluate("(document.body && document.body.innerText || '').slice(0, 200)")
    raise TimeoutError(
        f"等了 {timeout:g} 秒，页面上还是没有 {selector!r}。\n"
        f"当前页面标题：{title}\n"
        f"页面开头：{body!r}"
    )


@action(
    id="launch",
    name="启动浏览器",
    category="网页",
    icon="globe",
    description="用调试端口启动 Edge / Chrome，并连上去。已经连上就直接返回",
    inputs={
        "browser": File(label="浏览器路径", help="留空自动找 Edge / Chrome"),
        "port": Integer(default=DEFAULT_PORT, label="调试端口"),
        "url": String(default="about:blank", label="打开哪个地址"),
        "headless": Bool(default=False, label="无头模式", help="不开窗口，适合服务器上跑"),
        "profile_dir": String(
            default="",
            label="配置目录",
            help="**登录状态就存在这里**。留空用插件目录下的 .profile",
        ),
    },
    outputs={
        "port": Integer(label="调试端口"),
        "title": String(label="页面标题"),
        "url": String(label="当前地址"),
    },
)
def launch(
    ctx: Context,
    browser: Any = "",
    port: int = DEFAULT_PORT,
    url: str = "about:blank",
    headless: bool = False,
    profile_dir: str = "",
    **_: Any,
) -> dict[str, Any]:
    global _BROWSER

    port = int(port or DEFAULT_PORT)

    if _BROWSER is not None and _alive(_BROWSER):
        # 连过、而且现在还是通的，就复用。**不要再启一个** —— 那样会开出一堆浏览器
        # 窗口，而且后面所有动作到底连到哪一个就说不清了。
        current = _BROWSER.evaluate("location.href")
        if url and url != "about:blank" and current != url:
            _BROWSER.send("Page.navigate", {"url": url})
        ctx.info(f"复用已经连上的浏览器（端口 {port}）")
        return {
            "port": port,
            "title": _BROWSER.evaluate("document.title") or "",
            "url": _BROWSER.evaluate("location.href") or "",
        }

    if _BROWSER is not None:
        # 引用还在，但连接已经死了（浏览器被关了）。清掉，往下走重新连。
        ctx.info("上次那个连接已经断了（浏览器被关了？），重新连一个")
        _BROWSER = None

    already = _port_alive(port)
    executable = _find_browser(str(browser or ""))

    if already:
        # 端口上已经有浏览器了 —— 直接连，别启新的。
        ctx.info(f"端口 {port} 上已经有浏览器，直接连它")
    else:
        # **必须用独立的 user-data-dir。** 浏览器已经在跑的时候，再启动一个同 profile 的
        # 实例只会把参数交给已有进程然后自己退出 —— 调试端口根本不会开，
        # 而现象是"启动成功了但连不上"。
        #
        # **它同时是登录状态的存放处。** 目录固定，所以第一次登录过之后，
        # 后面每次启动都还是登录着的。
        profile = Path(str(profile_dir)).expanduser() if str(profile_dir or "").strip() else _default_profile()
        profile.mkdir(parents=True, exist_ok=True)
        argv = [
            str(executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            # 双保险：我们自己启动的浏览器，顺手把这个打开。
            # 但真正让连接成功的是连接端的 suppress_origin（见 _Browser.__init__）——
            # 用户手动启动的浏览器，这个参数是加不上的。
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            # 把浏览器自己的首次运行体验压下去 —— 那个同步确认页会抢在我们要的页面之前，
            # 变成调试端口的第一个标签页。真正兜住的是 _pick_target + 显式跳转，
            # 这两个参数只是少添点乱。
            "--disable-sync",
            "--no-service-autorun",
        ]
        if headless:
            argv.append("--headless=new")
        argv.append(url or "about:blank")

        ctx.info(f"启动 {executable.name}（端口 {port}，配置目录 {profile}）")
        # 和 core.app 一样彻底断开设备：这个浏览器要活过流程，不能被插件进程的
        # 生命周期绑住。
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) if os.name == "nt" else 0,
            close_fds=True,
        )

    _BROWSER = _connect(port, prefer=url)
    _BROWSER.send("Page.enable")

    # **连上之后显式跳转一次。** 命令行给的地址不一定落在我们连上的那个标签页上 ——
    # 浏览器自己会开页面（Edge 首次启动弹的同步确认页就排在前面）。不补这一下，
    # 后面所有动作都在那个对话框上做，而且什么都不报错。
    if url and url != "about:blank":
        current = str(_BROWSER.evaluate("location.href") or "")
        if current != url:
            ctx.info(f"连上的标签页是 {current}，跳到 {url}")
            _BROWSER.send("Page.navigate", {"url": url})
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                if _BROWSER.evaluate("document.readyState") == "complete":
                    break
                time.sleep(0.2)

    # **连上之后把连接交给清理钩子。** 插件被卸载时内核会关掉它 —— 不关的话
    # 那个 WebSocket 会一直占着，浏览器那边也留着一个"有人在调试我"的状态。
    #
    # 传的是 ``_close_current`` 而不是 ``_BROWSER.close``：连接断掉之后会重连，
    # 绑定的方法会指向那个已经作废的旧连接。
    ctx.on_cleanup(_close_current, name="关闭浏览器调试连接")

    title = _BROWSER.evaluate("document.title") or ""
    current = _BROWSER.evaluate("location.href") or ""
    ctx.info(f"已连上：{title or current}")
    return {"port": port, "title": title, "url": current}


@action(
    id="open",
    name="打开网址",
    category="网页",
    icon="link",
    description="让当前标签页跳到某个地址，并等它加载完",
    inputs={
        "url": String(required=True, label="地址"),
        "cookie": Text(
            default="",
            label="带上 cookie",
            help="形如 a=b; c=d（一行一条也行）。**在打开之前就设好**，页面一加载就是登录状态",
        ),
        "wait_seconds": Number(default=0.0, label="额外等几秒", help="等页面自己渲染完"),
        "timeout": Number(default=30.0, label="最多等几秒"),
    },
    outputs={
        "title": String(label="页面标题"),
        "url": String(label="最终地址"),
        "cookies": Integer(label="设上了几个 cookie"),
    },
)
def open_(
    ctx: Context,
    url: str,
    cookie: str = "",
    wait_seconds: float = 0.0,
    timeout: float = 30.0,
    **_: Any,
) -> dict[str, Any]:
    browser = _current()

    # **先设 cookie，再跳转。** 顺序反了的话页面已经以未登录状态加载完了，
    # 你会先看到登录页 —— 那看起来像"cookie 没生效"。
    applied = 0
    if cookie.strip():
        applied, problems = _apply_cookie_string(browser, url, cookie)
        for line in problems:
            ctx.warning(f"cookie 没设上 —— {line}")
        if applied:
            ctx.info(f"先设好 {applied} 个 cookie，再打开")
        elif problems:
            raise RuntimeError(
                "一个 cookie 都没设上，页面不会是登录状态。原因见上面那几条 —— "
                "最常的是域对不上（比如给 example.com 设了别的站的 cookie）"
            )

    browser.send("Page.navigate", {"url": url})

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = browser.evaluate("document.readyState")
        if state == "complete":
            break
        time.sleep(0.2)
    else:
        raise TimeoutError(f"{url} 等了 {timeout:g} 秒还没加载完")

    if wait_seconds > 0:
        time.sleep(float(wait_seconds))

    title = browser.evaluate("document.title") or ""
    final = browser.evaluate("location.href") or ""
    ctx.info(f"打开 {final}（{title}）")
    return {"title": title, "url": final, "cookies": applied}


@action(
    id="js",
    name="执行 JavaScript",
    category="网页",
    icon="code",
    description=(
        "在页面里跑一段 JS 并拿回结果。**这一个动作能干上面所有的活** —— "
        "拿不准用哪个动作时就用它"
    ),
    inputs={
        "code": Text(required=True, label="代码", help="最后一行表达式的值会被返回"),
        "timeout": Number(default=30.0, label="最多等几秒"),
    },
    outputs={"result": Text(label="返回值"), "text": String(label="值的文本形式")},
)
def js(ctx: Context, code: str, timeout: float = 30.0, **_: Any) -> dict[str, Any]:
    result = _current().evaluate(code, timeout=float(timeout))
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    ctx.info(f"JS 返回：{text[:120]}" + ("…" if len(text) > 120 else ""))
    return {"result": result, "text": text}


@action(
    id="click",
    name="点击元素",
    category="网页",
    icon="mouse-pointer",
    description="按选择器找到元素并点它。找不到就等，等到超时才失败",
    inputs={
        "selector": String(required=True, label="选择器", help="CSS 选择器，例如 #submit、.btn"),
        "timeout": Number(default=10.0, label="最多等几秒"),
        "index": Integer(default=0, label="第几个", help="匹配到多个时用哪个，从 0 数"),
        "scroll": Bool(default=True, label="先滚动到它"),
    },
    outputs={"clicked": Bool(label="点到了吗"), "text": String(label="元素上的文字")},
)
def click(
    ctx: Context,
    selector: str,
    timeout: float = 10.0,
    index: int = 0,
    scroll: bool = True,
    **_: Any,
) -> dict[str, Any]:
    browser = _current()
    _wait_selector(browser, selector, float(timeout))

    # **用页面自己调 click()，不是发鼠标事件。** 后者要算坐标，元素被浮动层挡住、
    # 或者不在视口里就会点空 —— 而页面不知道有人在点它，什么错都不报。
    script = f"""
    (() => {{
      const list = document.querySelectorAll({json.dumps(selector)});
      const el = list[{int(index)}];
      if (!el) return {{ok: false, count: list.length}};
      {"el.scrollIntoView({block: 'center'});" if scroll else ""}
      el.click();
      return {{ok: true, count: list.length, text: (el.innerText || el.value || '').trim().slice(0, 200)}};
    }})()
    """
    result = browser.evaluate(script) or {}
    if not result.get("ok"):
        raise RuntimeError(
            f"{selector!r} 等到了，但要第 {index} 个时没有 —— 只匹配到 {result.get('count', 0)} 个"
        )
    ctx.info(f"点了 {selector}：{result.get('text', '')[:60]}")
    return {"clicked": True, "text": str(result.get("text") or "")}


@action(
    id="type_text",
    name="输入文字",
    category="网页",
    icon="type",
    description="往输入框里填字。会先清空原来的内容",
    inputs={
        "selector": String(required=True, label="选择器"),
        "text": Text(required=True, label="填什么"),
        "clear": Bool(default=True, label="先清空"),
        "enter": Bool(default=False, label="填完按回车"),
        "timeout": Number(default=10.0, label="最多等几秒"),
    },
    outputs={"typed": Bool(label="填上了吗")},
)
def type_text(
    ctx: Context,
    selector: str,
    text: str,
    clear: bool = True,
    enter: bool = False,
    timeout: float = 10.0,
    **_: Any,
) -> dict[str, Any]:
    browser = _current()
    _wait_selector(browser, selector, float(timeout))

    # **用原生 setter 赋值再派发 input 事件。** 直接 `el.value = x` 对 React / Vue 这类
    # 框架是无效的 —— 它们监听的是 setter 被调用时派发的事件，属性直接改它们不知道，
    # 表现是"字填进去了但提交时是空的"。
    clear_js = "set(el, '');" if clear else ""
    enter_js = "el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));" if enter else ""
    script = f"""
    (() => {{
      const el = document.querySelector({json.dumps(selector)});
      if (!el) return {{ok: false}};
      el.scrollIntoView({{block: 'center'}});
      el.focus();
      const proto = el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      const set = (node, v) => {{ if (setter) setter.call(node, v); else node.value = v; }};
      {clear_js}
      set(el, {json.dumps(text)});
      el.dispatchEvent(new Event('input', {{bubbles: true}}));
      el.dispatchEvent(new Event('change', {{bubbles: true}}));
      {enter_js}
      return {{ok: true, value: el.value}};
    }})()
    """
    result = browser.evaluate(script) or {}
    if not result.get("ok"):
        raise RuntimeError(f"找不到 {selector!r}，填不进去")
    shown = text if len(text) <= 40 else f"{text[:40]}…"
    ctx.info(f"往 {selector} 填了 {shown!r}" + ("，并回车" if enter else ""))
    return {"typed": True}


@action(
    id="read",
    name="读页面内容",
    category="网页",
    icon="file-text",
    description="取元素的文字、属性，或者整个页面的文本。也可以读多个元素",
    inputs={
        "selector": String(
            default="", label="选择器", help="留空 = 整个页面的文本",
        ),
        "field": Enum(
            ["文字", "HTML", "属性值", "输入的当前值"],
            default="文字",
            label="读什么",
        ),
        "attribute": String(default="href", label="属性名", help="「属性值」时用"),
        "all": Bool(default=False, label="读全部匹配的", help="关掉只读第一个"),
        "timeout": Number(default=10.0, label="最多等几秒"),
    },
    outputs={
        "text": Text(label="读到的东西"),
        "count": Integer(label="匹配到几个"),
        "first": String(label="第一个"),
    },
)
def read(
    ctx: Context,
    selector: str = "",
    field: str = "文字",
    attribute: str = "href",
    all: bool = False,
    timeout: float = 10.0,
    **_: Any,
) -> dict[str, Any]:
    browser = _current()

    if not selector.strip():
        text = browser.evaluate("(document.body && document.body.innerText) || ''") or ""
        ctx.info(f"读了整个页面的文本，{len(text)} 个字")
        return {"text": text, "count": 1, "first": text[:200]}

    _wait_selector(browser, selector, float(timeout))

    pick = {
        "文字": "el.innerText",
        "HTML": "el.innerHTML",
        "属性值": f"el.getAttribute({json.dumps(attribute)})",
        "输入的当前值": "el.value",
    }[field]

    script = f"""
    (() => {{
      const list = [...document.querySelectorAll({json.dumps(selector)})];
      const get = (el) => {{ const v = ({pick}); return v == null ? '' : String(v); }};
      return {{
        values: {'list.map(get)' if all else '[get(list[0])].filter((_, i) => list.length > 0)'},
        count: list.length,
      }};
    }})()
    """
    result = browser.evaluate(script) or {}
    values = [str(v) for v in (result.get("values") or [])]
    text = "\n".join(values)
    ctx.info(f"{selector}：匹配 {result.get('count', 0)} 个，读到 {len(text)} 个字")
    return {"text": text, "count": int(result.get("count") or 0), "first": values[0] if values else ""}


@action(
    id="screenshot",
    name="网页截图",
    category="网页",
    icon="camera",
    description="把当前页面截下来存成 PNG",
    inputs={
        "filename": String(default="", label="文件名", help="留空自动按时间起名"),
        "full_page": Bool(default=False, label="整页", help="包含要滚动才能看到的部分"),
    },
    outputs={"path": String(label="存到哪"), "bytes": Integer(label="字节数")},
)
def screenshot(
    ctx: Context, filename: str = "", full_page: bool = False, **_: Any
) -> dict[str, Any]:
    import base64  # noqa: PLC0415

    browser = _current()
    params: dict[str, Any] = {"format": "png"}
    if full_page:
        params["captureBeyondViewport"] = True

    data = browser.send("Page.captureScreenshot", params)
    raw = base64.b64decode(data.get("data") or "")

    name = filename.strip() or f"web_{time.strftime('%Y%m%d_%H%M%S')}.png"
    target = ctx.artifact(name)
    target.write_bytes(raw)
    ctx.info(f"截图存到 {target}（{len(raw) // 1024} KB）")
    return {"path": str(target), "bytes": len(raw)}


@action(
    id="record_start",
    name="开始录制",
    category="网页",
    icon="circle",
    description=(
        "开始记录你在页面上的点击和输入，**并给每个元素算出一个稳定的选择器**。"
        "之后在浏览器里正常操作，回来用「取回录制」把步骤拿出来"
    ),
    inputs={},
    outputs={"started": Bool(label="开始了吗")},
)
def record_start(ctx: Context, **_: Any) -> dict[str, Any]:
    global _INJECTION

    browser = _current()
    source = _recorder_source()

    # **两个都做。** 注入当前页是"现在就开始录"，注册到新文档是"跳页之后还在录" ——
    # 少了后者，用户点一个链接跳走，录制就悄悄停了，而"录到一半没了"很难联想到是
    # 页面导航导致的。
    result = browser.send("Page.addScriptToEvaluateOnNewDocument", {"source": source})
    _INJECTION = result.get("identifier")
    browser.evaluate(source)

    ctx.info("开始录制。在浏览器里正常操作，然后回来用「取回录制」")
    return {"started": True}


@action(
    id="record_dump",
    name="取回录制",
    category="网页",
    icon="download",
    description=(
        "把录到的步骤拿出来，输出一份**人能读的清单**，照着搭流程就行。"
        "**不会停止录制** —— 可以边看边继续操作"
    ),
    inputs={"clear": Bool(default=False, label="取完清空", help="只想要新录的那一段时用")},
    outputs={
        "steps": Text(label="步骤清单（人看的）"),
        "json": Text(label="原始数据"),
        "count": Integer(label="几步"),
    },
)
def record_dump(ctx: Context, clear: bool = False, **_: Any) -> dict[str, Any]:
    browser = _current()

    # 当前页上的录制器可能还在，直接读内存；页面导航过的话读 sessionStorage。
    # **sessionStorage 是同一个站内的** —— 跨站跳转会把之前录的丢掉，这是这个方案的
    # 已知边界。要跨站连续录，得改成 CDP 的 Runtime.addBinding 由插件实时收事件。
    rows = browser.evaluate(
        "(() => { try { return JSON.stringify("
        "(window.__myautoworkRead ? window.__myautoworkRead() : "
        "JSON.parse(sessionStorage.getItem('__myautowork_log') || '[]'))); } "
        "catch (e) { return '[]'; } })()"
    )
    try:
        log = json.loads(rows or "[]")
    except Exception:
        log = []

    lines: list[str] = []
    for index, row in enumerate(log, 1):
        kind = row.get("type")
        selector = row.get("selector") or ""
        if kind == "click":
            label = row.get("text") or ""
            lines.append(f"{index}. 点击  {selector}" + (f"   （{label}）" if label else ""))
        elif kind == "input":
            lines.append(f"{index}. 输入  {selector}  = {row.get('value', '')!r}")
        else:
            lines.append(f"{index}. {kind}  {selector}")

    if clear:
        browser.evaluate(
            "(() => { window.__myautoworkClear ? window.__myautoworkClear() : "
            "sessionStorage.removeItem('__myautowork_log'); })()"
        )

    text = "\n".join(lines)
    ctx.info(f"录到 {len(log)} 步" + ("（已清空）" if clear else ""))
    return {"steps": text, "json": json.dumps(log, ensure_ascii=False, indent=2), "count": len(log)}


@action(
    id="record_stop",
    name="停止录制",
    category="网页",
    icon="square",
    description="把录制脚本撤掉。已经录到的步骤还在，用「取回录制」照样能拿",
    inputs={},
    outputs={"stopped": Bool(label="停了吗")},
)
def record_stop(ctx: Context, **_: Any) -> dict[str, Any]:
    global _INJECTION

    browser = _current()
    if _INJECTION:
        try:
            browser.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": _INJECTION})
        except Exception:
            pass
        _INJECTION = None

    # **只关"录制"，不关"读取"。** 一开始我顺手把 ``__myautoworkRead`` 也置空了，
    # 结果停止之后「取回录制」拿到 0 步 —— 看起来像"停止会把录到的东西清掉"，
    # 其实是读取器没了，只能回退去读 sessionStorage，而某些页面上那个读不到。
    # 录到的东西要留着，用户就是要在停止之后把它拿出来的。
    browser.evaluate("window.__myautoworkRecording = false;")
    ctx.info("已停止录制（已经录到的步骤还在，用「取回录制」拿走）")
    return {"stopped": True}


@action(
    id="wait_for",
    name="等待条件成立",
    category="网页",
    icon="clock",
    description=(
        "反复跑一段 JS，直到它**返回真**为止。等视频播完、等某个数变了、等按钮可点，"
        "都用它。超时会告诉你页面上现在是什么样"
    ),
    inputs={
        "code": Text(
            required=True,
            label="条件",
            help="一段返回真/假的 JS，例如 document.querySelector('video')?.ended === true",
        ),
        "timeout": Number(default=60.0, label="最多等几秒"),
        "interval": Number(default=0.5, label="多久查一次", help="秒"),
    },
    outputs={
        "ok": Bool(label="成立了吗"),
        "waited": Number(label="等了几秒"),
        "value": Text(label="最后一次的结果"),
    },
)
def wait_for(
    ctx: Context,
    code: str,
    timeout: float = 60.0,
    interval: float = 0.5,
    **_: Any,
) -> dict[str, Any]:
    browser = _current()
    timeout = max(0.1, float(timeout))
    interval = max(0.05, float(interval))

    started = time.monotonic()
    last: Any = None
    while time.monotonic() - started < timeout:
        last = browser.evaluate(code)
        if last:
            waited = time.monotonic() - started
            ctx.info(f"条件成立（等了 {waited:.1f} 秒）")
            return {"ok": True, "waited": round(waited, 2), "value": last}
        time.sleep(interval)

    # 超时要说清楚**现在是什么样** —— 只说"超时了"帮不上任何忙。
    title = browser.evaluate("document.title") or ""
    videos = browser.evaluate(
        "(() => [...document.querySelectorAll('*')]"
        ".filter(e => e instanceof HTMLVideoElement)"
        ".map(v => ({current: +v.currentTime.toFixed(1), duration: v.duration, "
        "paused: v.paused, ended: v.ended})))()"
    )
    raise TimeoutError(
        f"等了 {timeout:g} 秒，条件一直不成立。\n"
        f"条件最后一次的结果：{last!r}\n"
        f"当前页面：{title}\n"
        f"页面上的视频：{videos}"
    )


@action(
    id="cookies",
    name="Cookie",
    category="网页",
    icon="key",
    description=(
        "读、写、清 cookie。**登录状态本来就存在配置目录里**，"
        "这个动作用于导出一份会话去别处复用，或者把别处拿到的会话塞进来"
    ),
    inputs={
        "op": Enum(
            ["读出全部", "取出某个", "写入", "清掉某个", "清空"],
            default="读出全部",
            label="做什么",
        ),
        "name": String(default="", label="名字", help="写入/取出/清掉某个时用"),
        "value": String(default="", label="值"),
        "url": String(default="", label="网址", help="限定哪个站点的 cookie；写入时用它定 domain"),
        "domain": String(default="", label="域", help="不用网址时可以直接给 domain"),
    },
    outputs={
        "text": Text(label="名字=值，每行一条"),
        "count": Integer(label="几个"),
        "value": String(label="取出的值"),
    },
)
def cookies(
    ctx: Context,
    op: str = "读出全部",
    name: str = "",
    value: str = "",
    url: str = "",
    domain: str = "",
    **_: Any,
) -> dict[str, Any]:
    browser = _current()
    # Network 域要开了才有 cookie 相关的命令。
    browser.send("Network.enable")

    if op == "清空":
        browser.send("Network.clearBrowserCookies")
        ctx.info("已清空所有 cookie")
        return {"text": "", "count": 0, "value": ""}

    if op == "写入":
        if not name.strip():
            raise ValueError("「写入」要在「名字」里填个 cookie 名")
        params: dict[str, Any] = {"name": name.strip(), "value": value}
        if url.strip():
            params["url"] = url.strip()
        elif domain.strip():
            params["domain"] = domain.strip()
            params["path"] = "/"
        else:
            # 不给 url 也不给 domain 的话，浏览器不知道该把这个 cookie 归给谁 ——
            # 它不会报错，只会静默地不生效，那更难查。
            current = browser.evaluate("location.href") or ""
            if not current.startswith("http"):
                raise ValueError(
                    "「写入」需要知道这个 cookie 属于哪个站 —— 在「网址」或「域」里给一个，"
                    f"或者先打开一个网页（现在是 {current!r}）"
                )
            params["url"] = current
        result = browser.send("Network.setCookie", params)
        if not result.get("success"):
            raise RuntimeError(
                f"浏览器拒绝写入这个 cookie（{name}）。域和路径对得上吗？"
                "HttpOnly / Secure 的 cookie 不能随便伪造"
            )
        ctx.info(f"写入 cookie {name}")
        return {"text": f"{name}={value}", "count": 1, "value": value}

    if op == "清掉某个":
        if not name.strip():
            raise ValueError("「清掉某个」要在「名字」里填个 cookie 名")
        params = {"name": name.strip()}
        if url.strip():
            params["url"] = url.strip()
        if domain.strip():
            params["domain"] = domain.strip()
        browser.send("Network.deleteCookies", params)
        ctx.info(f"清掉了 cookie {name}")
        return {"text": "", "count": 0, "value": ""}

    # 读的两种
    if url.strip():
        found = (browser.send("Network.getCookies", {"urls": [url.strip()]}) or {}).get("cookies") or []
    else:
        found = (browser.send("Network.getAllCookies") or {}).get("cookies") or []

    rows = [f"{c.get('name')}={c.get('value')}" for c in found]

    if op == "取出某个":
        for cookie in found:
            if cookie.get("name") == name.strip():
                got = str(cookie.get("value") or "")
                ctx.info(f"{name} = {got[:60]}" + ("…" if len(got) > 60 else ""))
                return {"text": f"{name}={got}", "count": 1, "value": got}
        # 找不到走 error 出口，而不是给个空串 —— 空串会让下游拿着空 token 继续跑。
        raise KeyError(
            f"没有叫 {name!r} 的 cookie。当前有：{'、'.join(c.get('name', '') for c in found) or '（一个都没有）'}"
        )

    text = "\n".join(rows)
    ctx.info(f"读出 {len(found)} 个 cookie")
    return {"text": text, "count": len(found), "value": ""}


@action(
    id="close",
    name="关闭连接",
    category="网页",
    icon="x",
    description="断掉调试连接。**不关浏览器** —— 浏览器要留给你自己用",
    inputs={"quit_browser": Bool(default=False, label="连浏览器也关掉")},
    outputs={"closed": Bool(label="断开了吗")},
)
def close(ctx: Context, quit_browser: bool = False, **_: Any) -> dict[str, Any]:
    global _BROWSER

    if _BROWSER is None:
        ctx.info("本来就没连着")
        return {"closed": False}

    browser, _BROWSER = _BROWSER, None
    if quit_browser:
        try:
            browser.send("Browser.close", timeout=3.0)
        except Exception:
            pass
    browser.close()
    ctx.info("已断开" + ("，并关掉了浏览器" if quit_browser else "（浏览器还开着）"))
    return {"closed": True}
