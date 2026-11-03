"""Endless Test ↔ N.E.K.O. bridge.

三个职责：
1. 起一个本地 HTTP 服务，把《无尽测试》单文件页面分享出去。
2. 把游戏数据统一存在插件目录（state.json），网页/面板/全屏窗口共用同一份。
3. 给 Neko 提供入口：开局、读档案、记战报、重置。

启动方式：
    launch_web      默认浏览器打开
    launch_panel    插件面板内嵌（返回面板地址）
    launch_window   独立全屏窗口（Edge/Chrome --app 模式，无地址栏）
"""

from __future__ import annotations

import base64 as _b64
import hashlib as _hashlib
import json
import os
import shutil
import struct as _struct
import subprocess
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
)

try:
    from plugin.sdk.plugin import llm_tool
except Exception:  # 旧 SDK 没有 llm_tool 时退化成普通入口，至少不炸
    def llm_tool(*_a, **_k):
        def _wrap(fn):
            return fn
        return _wrap

try:
    from . import brain
except Exception:  # pragma: no cover - 独立运行时兜底
    import brain  # type: ignore



_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def _ws_accept(key: str) -> str:
    return _b64.b64encode(_hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()

def _ws_send_text(sink: Any, text: str) -> bool:
    """发送一个文本帧（不分片）。

    sink 可以是 socket（有 sendall）或文件对象（有 write+flush）。
    走 HTTP 层时必须用 wfile，否则会和 rfile 的缓冲竞争导致帧错位。
    """
    try:
        data = text.encode("utf-8")
        header = bytearray()
        header.append(0x81)  # FIN + text
        n = len(data)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += _struct.pack(">H", n)
        else:
            header.append(127)
            header += _struct.pack(">Q", n)
        frame = bytes(header) + data
        if hasattr(sink, "sendall"):
            # 注意：绝对不要在这里设 socket 超时。
            # settimeout 作用于整个 socket，读循环也会受影响，
            # 空闲 0.5 秒就会超时，导致 WS 连接被误判为断开。
            sink.sendall(frame)
        else:
            sink.write(frame)
            sink.flush()
        return True
    except Exception:
        return False


def _ws_recv_frame(reader: Any) -> Any:
    """从缓冲区读取一帧，返回 (opcode, payload) 或 None（连接关闭）。

    reader 必须是 buffered reader 的 read 方法（如 handler.rfile.read）：
    BaseHTTPRequestHandler 会把请求头之后的数据缓冲在 rfile 里，
    直接对 socket 调用 recv 会读不到 WebSocket 帧。
    """

    def _read(n: int) -> bytes:
        out = b""
        while len(out) < n:
            try:
                chunk = reader(n - len(out))
            except Exception:
                return out
            if not chunk:
                return out
            out += chunk
        return out

    try:
        head = _read(2)
        if len(head) < 2:
            return None
        b1, b2 = head[0], head[1]
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            ext = _read(2)
            if len(ext) < 2:
                return None
            length = _struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = _read(8)
            if len(ext) < 8:
                return None
            length = _struct.unpack(">Q", ext)[0]
        if length > 4 * 1024 * 1024:
            return None
        mask = _read(4) if masked else b""
        if masked and len(mask) < 4:
            return None
        payload = _read(length) if length else b""
        if masked:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        return opcode, payload
    except Exception:
        return None


def _class_name(cid: str) -> str:
    return brain.CLASSES.get(cid, {}).get("name", cid)


DEFAULT_PORT = 48931
STATE_VERSION = 1

_BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _profile_dir(root: Path) -> Path:
    """窗口模式专用浏览器配置，放在插件安装目录之外的稳定位置。"""
    base = os.getenv("NEKO_PLUGIN_DATA_DIR") or os.getenv("NEKO_DATA_DIR") or ""
    if base:
        return Path(base) / "endless_test" / "app-profile"
    return Path.home() / "AppData" / "Local" / "endless_test" / "app-profile"


def _window_args(url: str, root: Path, size: str) -> list:
    """独立窗口参数：无边框、无地址栏、无菜单，像原生客户端。"""
    # 浏览器配置放数据目录：安装目录会在重装时被清空，而且运行中会锁文件
    profile = _profile_dir(root)
    profile.mkdir(parents=True, exist_ok=True)
    return [
        f"--app={url}",
        "--kiosk",
        f"--window-size={size}",
        "--window-position=0,0",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-service-autorun",
        "--disable-features=Translate,MediaRouter,GlobalMediaControls",
        "--disable-session-crashed-bubble",
        "--autoplay-policy=no-user-gesture-required",
        "--overscroll-history-navigation=0",
        "--disable-pinch",
        f"--user-data-dir={profile}",
    ]


def _find_browser() -> Optional[str]:
    for path in _BROWSER_CANDIDATES:
        if os.path.isfile(path):
            return path
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


@neko_plugin
class EndlessTestPlugin(NekoPluginBase):
    """无尽测试插件：托管页面 + 统一存档。"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger
        self._lock = threading.RLock()
        self._http: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._port = DEFAULT_PORT
        self._root = Path(__file__).resolve().parent
        self._static_dir = self._root / "static"
        self._state_path: Optional[Path] = None
        self._ally_push_chat = True
        self._last_chat_at = 0.0
        self._last_chat_text = ""
        self._ally = {"class": "", "tree": [], "plan": [], "reason": "", "since": 0.0}
        self._session = {"token": "", "mode": "", "since": 0.0, "seen": 0.0}
        self._session_lock = threading.RLock()
        # ---- 多人联机房间 ----
        # 联机网络状态
        self._lan_open = str(os.environ.get("NEKO_ENDLESS_LAN") or "") == "1"
        self._beacon: Optional[Any] = None
        self._discovered: Dict[str, Dict[str, Any]] = {}
        self._rooms: Dict[str, Dict[str, Any]] = {}
        self._rooms_lock = threading.RLock()
        self._peer_seq = 0
        self._window_proc: Optional[subprocess.Popen] = None
        # AI 伙伴：全部决策来自 Neko，插件只做状态中转
        self._ally = {"class": "", "tree": [], "plan": [], "reason": "", "since": 0.0}
        self._ally_order = {"stance": "follow", "focus": "nearest", "buy": [], "say": "", "cast": "none", "id": 0, "at": 0.0}
        self._ally_snap: Dict[str, Any] = {}
        self._ally_snap_at = 0.0
        self._ally_event = ""
        self._ally_event_at = 0.0
        self._ally_start_at = 0.0
        self._ally_asked_at = 0.0
        self._ally_run_key = ""
        self._ally_nudged_event_at = 0.0
        self._ally_result: Dict[str, Any] = {}
        self._ally_sig = ""
        self._last_digest = ""
        self._last_digest_at = 0.0
        self._ally_drive: Dict[str, Any] = {"auto": False, "aggression": .5, "range": "mid",
                                            "dodge": True, "target": "nearest", "retreatHp": 30}
        self._ally_owned: list = []
        self._ally_nudge_at = 0.0
        self._ally_lock = threading.RLock()
        self._ally_stop = threading.Event()
        self._ally_thread: Optional[threading.Thread] = None

    # ---------------- 路径 ----------------

    def _resolve_state_path(self) -> Path:
        if self._state_path:
            return self._state_path
        try:
            base = Path(self.data_path("state.json"))
            path = base if base.suffix == ".json" else base / "state.json"
        except Exception:
            path = self._root / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path = path
        return path

    def _game_file(self) -> Path:
        return self._static_dir / "index.html"

    # ---------------- 数据 ----------------

    def _read_state(self) -> Dict[str, Any]:
        path = self._resolve_state_path()
        with self._lock:
            if not path.is_file():
                return {"v": STATE_VERSION, "updated_at": "", "data": {}, "runs": []}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                self.logger.warning("state.json unreadable, starting fresh: {}", exc)
                return {"v": STATE_VERSION, "updated_at": "", "data": {}, "runs": []}
        if not isinstance(payload, dict):
            return {"v": STATE_VERSION, "updated_at": "", "data": {}, "runs": []}
        payload.setdefault("v", STATE_VERSION)
        payload.setdefault("data", {})
        payload.setdefault("runs", [])
        return payload

    def _write_state(self, payload: Dict[str, Any]) -> None:
        path = self._resolve_state_path()
        payload["v"] = STATE_VERSION
        payload["updated_at"] = _now()
        tmp = path.with_suffix(".tmp")
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)

    def _merge_state(self, incoming: Dict[str, Any]) -> Dict[str, Any]:
        """按 key 合并：incoming 覆盖同名 key，保留其余。"""
        payload = self._read_state()
        data = payload.get("data")
        if not isinstance(data, dict):
            data = {}
        for key, value in (incoming or {}).items():
            if not isinstance(key, str):
                continue
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        payload["data"] = data
        return payload

    # ---------------- HTTP ----------------

    def _start_http(self) -> None:
        if self._http:
            return
        plugin = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
                return

            def _cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Cache-Control", "no-store")

            def _send(self, code: int, body: bytes, mime: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass

            def _json(self, code: int, payload: Any) -> None:
                self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                route = urlparse(self.path).path
                if route == "/api/state":
                    self._json(200, plugin._read_state())
                    return
                if route == "/api/profile":
                    self._json(200, plugin._profile())
                    return
                if route == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if route == "/api/ping":
                    self._json(200, {"ok": True, "plugin": "endless_test", "port": plugin._port})
                    return
                if route == "/api/ally":
                    self._json(200, {"ok": True, "say": plugin._ally_line("tick", {})})
                    return
                if route == "/api/ally/meta":
                    self._json(200, plugin._ally_meta())
                    return
                if route == "/api/ally/avatar":
                    plugin._serve_avatar(self)
                    return
                if route == "/api/session":
                    self._json(200, plugin._session_state())
                    return
                if route == "/api/rooms":
                    self._json(200, plugin._room_stats())
                    return
                if route == "/api/names":
                    self._json(200, plugin._names())
                    return
                if route == "/api/net":
                    self._json(200, plugin._net_info())
                    return
                if route == "/api/net/discover":
                    self._json(200, {"ok": True, "servers": plugin._discover()})
                    return
                if route == "/ws":
                    import traceback as _tb
                    try:
                        plugin._ws_upgrade(self)
                    except Exception as _exc:
                        plugin.logger.warning(f"ws: upgrade crashed {_exc}\n{_tb.format_exc()}")
                    return
                if route == "/api/launch":
                    mode = (parse_qs(urlparse(self.path).query).get("mode") or ["web"])[0]
                    self._json(200, plugin._launch_mode(mode))
                    return
                plugin._serve_static(self, route)

            def do_PUT(self) -> None:  # noqa: N802
                self._handle_write()

            def do_POST(self) -> None:  # noqa: N802
                self._handle_write()

            def _handle_write(self) -> None:
                route = urlparse(self.path).path
                if route not in ("/api/net/lan", "/api/state", "/api/merge", "/api/ally", "/api/report", "/api/quit",
                                 "/api/ally/start", "/api/ally/state", "/api/ally/command", "/api/ally/order",
                                 "/api/session/claim", "/api/session/ping", "/api/session/release"):
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    incoming = json.loads(raw.decode("utf-8") or "{}")
                except Exception as exc:
                    self._json(400, {"ok": False, "error": f"bad payload: {exc}"})
                    return
                try:
                    if route == "/api/net/lan":
                        open_lan = bool((incoming or {}).get("open"))
                        self._json(200, plugin._set_lan(open_lan))
                        return
                    if route == "/api/session/claim":
                        self._json(200, plugin._session_claim(str((incoming or {}).get("mode") or "web")))
                        return
                    if route == "/api/session/ping":
                        self._json(200, plugin._session_ping(str((incoming or {}).get("token") or "")))
                        return
                    if route == "/api/session/release":
                        self._json(200, plugin._session_release(str((incoming or {}).get("token") or "")))
                        return
                    if route == "/api/quit":
                        self._json(200, plugin._handle_quit(incoming if isinstance(incoming, dict) else {}))
                        return
                    if route == "/api/ally":
                        self._json(200, plugin._handle_ally(incoming if isinstance(incoming, dict) else {}))
                        return
                    if route == "/api/ally/start":
                        self._json(200, plugin._handle_ally_start(incoming if isinstance(incoming, dict) else {}))
                        return
                    if route == "/api/ally/state":
                        self._json(200, plugin._handle_ally_state(incoming if isinstance(incoming, dict) else {}))
                        return
                    if route == "/api/ally/command":
                        self._json(200, plugin._handle_ally_command(incoming if isinstance(incoming, dict) else {}))
                        return
                    if route == "/api/ally/order":
                        self._json(200, plugin._handle_ally_order_debug(incoming if isinstance(incoming, dict) else {}))
                        return
                    if route == "/api/report":
                        self._json(200, plugin._handle_report(incoming if isinstance(incoming, dict) else {}))
                        return
                    if route == "/api/merge":
                        data = incoming.get("data") if isinstance(incoming, dict) else None
                        payload = plugin._merge_state(data if isinstance(data, dict) else {})
                    else:
                        payload = incoming if isinstance(incoming, dict) else {}
                        payload.setdefault("data", {})
                    plugin._write_state(payload)
                    self._json(200, {"ok": True, "updated_at": payload.get("updated_at", "")})
                except Exception as exc:
                    plugin.logger.error("state write failed: {}", exc)
                    self._json(500, {"ok": False, "error": str(exc)})

        last_error: Optional[Exception] = None
        # 默认只监听回环；开启"对局域网开放"后绑定 0.0.0.0
        bind_host = "0.0.0.0" if self._lan_open else "127.0.0.1"
        for port in (self._port, self._port + 1, self._port + 2, 0):
            try:
                server = ThreadingHTTPServer((bind_host, port), Handler)
                self._port = server.server_address[1]
                self._http = server
                break
            except OSError as exc:
                last_error = exc
        if not self._http:
            raise RuntimeError(f"cannot bind http server: {last_error}")

        self._thread = threading.Thread(
            target=self._http.serve_forever, name="endless-test-http", daemon=True
        )
        self._thread.start()
        self.logger.info("Endless Test served at http://127.0.0.1:{}", self._port)

    def _serve_static(self, handler: BaseHTTPRequestHandler, route: str) -> None:
        rel = "index.html" if route in ("/", "") else route.lstrip("/")
        target = (self._static_dir / rel).resolve()
        try:
            target.relative_to(self._static_dir.resolve())
        except ValueError:
            handler._json(403, {"ok": False, "error": "forbidden"})
            return
        if not target.is_file():
            handler._json(404, {"ok": False, "error": f"missing {rel}"})
            return
        body = target.read_bytes()
        handler._send(200, body, _MIME.get(target.suffix.lower(), "application/octet-stream"))

    def _stop_http(self) -> None:
        if self._http:
            try:
                self._http.shutdown()
                self._http.server_close()
            except Exception:
                pass
            self._http = None
        self._thread = None

    # ---------------- 启动方式 ----------------

    def _url(self, mode: str = "") -> str:
        base = f"http://127.0.0.1:{self._port}/"
        return base + (f"?mode={mode}" if mode else "")

    def _open_browser(self, url: str) -> bool:
        try:
            if hasattr(os, "startfile"):
                os.startfile(url)  # type: ignore[attr-defined]
                return True
        except Exception as exc:
            self.logger.warning("startfile failed: {}", exc)
        try:
            browser = _find_browser()
            if not browser:
                return False
            subprocess.Popen([browser, url], close_fds=True)
            return True
        except Exception as exc:
            self.logger.warning("browser launch failed: {}", exc)
            return False

    def _open_window(self, size: str = "1280,720") -> bool:
        browser = _find_browser()
        if not browser:
            return False
        args = [browser] + _window_args(self._url("window"), self._root, size)
        try:
            subprocess.Popen(args, close_fds=True)
            return True
        except Exception as exc:
            self.logger.warning("window launch failed: {}", exc)
            return False


    def _launch_mode(self, mode: str) -> Dict[str, Any]:
        """给面板按钮用：web / window / panel。"""
        try:
            self._start_http()
        except Exception as exc:
            return {"ok": False, "error": f"服务未启动: {exc}"}
        if mode in ("web", "window"):
            st = self._session_state()
            if st["active"] and st["mode"] != mode:
                label = {"panel": "插件面板", "web": "网页", "window": "全屏程序"}.get(st["mode"], st["mode"])
                return {"ok": False, "error": f"已经有一个游戏在运行（{label}）", "active_mode": st["mode"]}
        if mode == "window":
            browser = _find_browser()
            if not browser:
                return {"ok": False, "error": "没找到 Edge/Chrome"}
            args = [browser] + _window_args(self._url("window"), self._root, "1280,720")
            try:
                self._window_proc = subprocess.Popen(args, close_fds=True)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "mode": mode, "url": self._url()}
        if mode == "web":
            ok = self._open_browser(self._url("web"))
            return {"ok": ok, "mode": mode, "url": self._url(),
                    "error": "" if ok else "浏览器没能自动打开"}
        return {"ok": True, "mode": "panel", "url": self._url()}

    # ---------------- 档案 ----------------

    def _profile(self) -> Dict[str, Any]:
        state = self._read_state()
        data = state.get("data") if isinstance(state.get("data"), dict) else {}
        meta_raw = data.get("nanite_meta_v1")
        meta: Dict[str, Any] = {}
        if isinstance(meta_raw, str) and meta_raw:
            try:
                meta = json.loads(meta_raw)
            except Exception:
                meta = {}
        runs = state.get("runs") if isinstance(state.get("runs"), list) else []
        best = None
        for run in runs:
            if not isinstance(run, dict):
                continue
            secs = run.get("seconds") or 0
            if best is None or secs > (best.get("seconds") or 0):
                best = run
        return {
            "clears": meta.get("clears", 0),
            "kills": meta.get("kills", 0),
            "runs_recorded": len(runs),
            "last_run": runs[-1] if runs else None,
            "best_run": best,
            "class_ranks": meta.get("classes", {}),
            "boss_best": meta.get("bossBest", {}),
            "updated_at": state.get("updated_at", ""),
        }


    def _register_ui(self) -> bool:
        """注册静态 UI；面板必须 no-store，否则宿主会缓存一小时旧版。"""
        cfg = {
            "enabled": True,
            "directory": str(self._static_dir),
            "index_file": "panel.html",
            "cache_control": "no-store, must-revalidate",
            "plugin_id": getattr(self, "plugin_id", "endless_test"),
        }
        notify = getattr(self, "_notify_static_ui_registered", None)
        if callable(notify):
            try:
                self._static_ui_config = cfg
                notify(cfg)
                self.logger.info("static ui registered with no-store")
                return True
            except Exception as exc:
                self.logger.warning("notify_static_ui_registered failed: {}", exc)
        try:
            return bool(self.register_static_ui("static"))
        except Exception as exc:
            self.logger.warning("register_static_ui failed: {}", exc)
            return False


    # ---------------- 单实例会话 ----------------

    SESSION_TTL = 12.0

    def _session_alive(self) -> bool:
        with self._session_lock:
            return bool(self._session["token"]) and (time.time() - self._session["seen"]) < self.SESSION_TTL

    def _session_state(self) -> Dict[str, Any]:
        with self._session_lock:
            alive = self._session_alive()
            return {
                "active": alive,
                "mode": self._session["mode"] if alive else "",
                "since": self._session["since"] if alive else 0,
                "age": round(time.time() - self._session["since"], 1) if alive else 0,
            }

    def _session_claim(self, mode: str) -> Dict[str, Any]:
        mode = (mode or "web").strip().lower()
        with self._session_lock:
            if self._session_alive():
                if self._session["mode"] == mode and mode == "panel":
                    self._session["seen"] = time.time()   # 同一面板重载，接管
                else:
                    return {
                        "ok": False,
                        "error": "已经有一个游戏在运行了",
                        "active_mode": self._session["mode"],
                        "state": self._session_state(),
                    }
            else:
                self._session["token"] = uuid.uuid4().hex
                self._session["mode"] = mode
                self._session["since"] = time.time()
            self._session["seen"] = time.time()
            return {"ok": True, "token": self._session["token"], "mode": mode,
                    "state": self._session_state()}

    def _session_ping(self, token: str) -> Dict[str, Any]:
        with self._session_lock:
            if token and token == self._session["token"]:
                self._session["seen"] = time.time()
                return {"ok": True, "state": self._session_state()}
        return {"ok": False, "error": "会话已被接管", "state": self._session_state()}

    def _session_release(self, token: str) -> Dict[str, Any]:
        with self._session_lock:
            if token and token == self._session["token"]:
                self._session = {"token": "", "mode": "", "since": 0.0, "seen": 0.0}
                return {"ok": True}
        return {"ok": False, "error": "token 不匹配"}

    # ---------------- AI 伙伴 ----------------

    def _ally_line(self, event: str, ctx: Dict[str, Any]) -> str:
        import random
        hp = ctx.get("hp")
        boss = ctx.get("boss")
        kills = ctx.get("kills") or 0
        danger = isinstance(hp, (int, float)) and hp <= 35
        if event == "start":
            return "我跟着你，别乱冲"
        if event == "boss":
            return random.choice([
                f"{boss or 'Boss'} 来了，先躲一轮",
                f"{boss or 'Boss'} 进场了，别站正面",
                f"注意 {boss or 'Boss'}，我先试探",
            ])
        if event == "lowhp":
            return random.choice(["血太少了，往后退", "你血量见底，我掩护", "先拉开，别硬吃"])
        if event == "killstreak":
            return random.choice([f"{kills} 连杀了，继续压", f"{kills} 连了，节奏不错", f"{kills} 连杀，我跟上"])
        if event == "hurt":
            return "挨打了，换个角度"
        if event == "dead":
            return "这局先记下来，下次换流派"
        if event == "win":
            return "打穿了，厉害"
        if danger:
            return "小心点，快没血了"
        return "我在你侧后方"

    def _push_chat(self, text: str, event: str, ctx: Dict[str, Any]) -> bool:
        """让 Neko 在聊天里说一句。失败不影响游戏。"""
        try:
            push = getattr(self.ctx, "push_message", None)
            if not push:
                return False
            push(
                source="endless_test",
                visibility=["chat"],
                ai_behavior="respond",
                parts=[{"type": "text", "text": text}],
                priority=4,
                metadata={"activity_type": f"endless_test.{event}", **({"ctx": ctx} if ctx else {})},
            )
            return True
        except Exception as exc:
            self.logger.warning("push_message failed: {}", exc)
            return False

    def _char_name(self) -> str:
        """当前角色卡名（Neko 配置里的「当前猫娘」）。"""
        cfg = Path(r"D:\beshudewengjian\N.E.K.O\config\characters.json")
        try:
            if cfg.is_file():
                data = json.loads(cfg.read_text(encoding="utf-8"))
                return str(data.get("当前猫娘") or "").strip()
        except Exception:
            pass
        return ""

    def _owner_name(self) -> str:
        """主人名字：Neko 的 characters.json 里 主人._reserved.ai_context.rename_events
        记录了历次改名，取最后一次的 new_name。"""
        cfg = Path(r"D:\beshudewengjian\N.E.K.O\config\characters.json")
        try:
            if cfg.is_file():
                data = json.loads(cfg.read_text(encoding="utf-8"))
                master = data.get("主人")
                if isinstance(master, dict):
                    ctx = (master.get("_reserved") or {}).get("ai_context") or {}
                    events = ctx.get("rename_events") or []
                    if isinstance(events, list) and events:
                        last = events[-1]
                        if isinstance(last, dict):
                            name = str(last.get("new_name") or "").strip()
                            if name:
                                return name
                    for key in ("昵称", "name", "姓名"):
                        val = master.get(key)
                        if isinstance(val, str) and val.strip():
                            return val.strip()
        except Exception as exc:
            self.logger.warning(f"owner name read failed {exc}")
        return ""

    def _ally_name(self) -> str:
        """AI 名字 = 角色卡名（characters.json 的 当前猫娘），和 Neko 列表一致。"""
        try:
            cfg = Path(r"D:\beshudewengjian\N.E.K.O\config\characters.json")
            if cfg.is_file():
                data = json.loads(cfg.read_text(encoding="utf-8"))
                return str(data.get("当前猫娘") or "").strip()
        except Exception as exc:
            self.logger.warning(f"ally name read failed {exc}")
        return ""

    def _names(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "player": self._owner_name(),
            "character": self._ally_name() or self._char_name(),
        }

    def _avatar_path(self) -> Optional[Path]:
        """找当前角色的立绘：card_faces/<当前猫娘>.png。"""
        roots = [Path.home() / "AppData" / "Local" / "N.E.K.O", Path(r"D:\beshudewengjian\N.E.K.O")]
        card_dir = Path(r"D:\beshudewengjian\N.E.K.O\card_faces")
        for root in roots:
            cand = root / "card_faces"
            if cand.is_dir():
                card_dir = cand
                break
        cfg = Path(r"D:\beshudewengjian\N.E.K.O\config\characters.json")
        name = ""
        try:
            if cfg.is_file():
                data = json.loads(cfg.read_text(encoding="utf-8"))
                name = str(data.get("当前猫娘") or "").strip()
        except Exception:
            name = ""
        if name:
            for ext in (".png", ".webp", ".jpg"):
                p = card_dir / f"{name}{ext}"
                if p.is_file():
                    return p
        if card_dir.is_dir():
            for p in sorted(card_dir.iterdir()):
                if p.suffix.lower() in (".png", ".webp", ".jpg"):
                    return p
        return None

    def _serve_avatar(self, handler: Any) -> None:
        p = self._avatar_path()
        if not p:
            handler.send_response(404)
            handler.end_headers()
            return
        try:
            data = p.read_bytes()
        except Exception:
            handler.send_response(500)
            handler.end_headers()
            return
        mime = "image/png" if p.suffix.lower() == ".png" else ("image/webp" if p.suffix.lower() == ".webp" else "image/jpeg")
        handler.send_response(200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        handler.wfile.write(data)

    def _handle_ally(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        event = str(payload.get("event") or "tick")
        ctx = payload.get("ctx") if isinstance(payload.get("ctx"), dict) else {}
        line = self._ally_line(event, ctx)
        pushed = False
        # 只有游戏里真的开着 AI 伙伴才让它说话；结算事件不再播报
        enabled = bool(payload.get("enabled"))
        if enabled and self._ally_push_chat and event in {"boss", "lowhp", "killstreak"}:
            now = time.time()
            recent = now - self._last_chat_at < 25
            same = line == self._last_chat_text and now - self._last_chat_at < 120
            if not recent and not same:
                pushed = self._push_chat(line, event, {k: v for k, v in ctx.items() if k in ("hp", "kills", "boss", "branch")})
                if pushed:
                    self._last_chat_at = now
                    self._last_chat_text = line
        return {"ok": True, "say": line, "pushed": pushed, "event": event}

    # ---------------- AI 伙伴：由 Neko 本体做决策 ----------------
    # 游戏只负责执行（走位 / 瞄准 / 开火 / 结算），
    # 职业、姿态、集火目标、科技购买、说什么，全部由 Neko 通过 llm_tool 决定。
    # 插件只做三件事：整理战场快照 → 唤醒 Neko → 把决定交回游戏。

    ALLY_STANCES = ("follow", "engage", "focus", "guard", "retreat", "free")
    ALLY_FOCUS = ("boss", "elite", "nearest", "none")

    def _ally_meta(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "classes": [
                {"id": cid, "name": brain.CLASSES[cid]["name"], "role": brain.CLASSES[cid]["role"],
                 "color": brain.CLASSES[cid]["color"], "tag": brain.CLASSES[cid]["tag"]}
                for cid in brain.CLASS_ORDER
            ],
            "current": dict(self._ally) if self._ally.get("class") else None,
            "order": dict(self._ally_order),
            "source": "neko",
        }

    def _push_ai(self, text: str, kind: str, coalesce_key: str = "ally_tick", priority: int = 9,
                 behavior: str = "respond") -> bool:
        """把一条提示送进 Neko 的上下文，并要求它这一轮做出决定。

        kind/coalesce_key 分开：主人的指挥不能被周期性的战术请求合并掉。
        """
        try:
            push = getattr(self.ctx, "push_message", None)
            if not push:
                return False
            push(
                source="endless_test",
                visibility=[],
                ai_behavior=behavior,
                parts=[{"type": "text", "text": text}],
                priority=priority,
                coalesce_key=coalesce_key,
                metadata={"activity_type": f"endless_test.{kind}"},
            )
            return True
        except Exception as exc:
            self.logger.warning("ally push_message failed: {}", exc)
            return False

    # ---- Neko 可调用的工具 ----

    @llm_tool(
        name="ally_pick_class",
        description=(
            "为《无尽测试》里的 AI 伙伴挑选职业。收到 [AI伙伴组建] 提示时调用一次。"
            "class_id 必须是提示里给出的职业 id；reason 用一句话说明为什么选它，"
            "这句话会显示在游戏里。只调用工具，不要用聊天消息代替。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "class_id": {"type": "string", "description": "职业 id，如 blade/cannon/field/link/aegis/vector/blight/phase"},
                "reason": {"type": "string", "description": "一句话理由，会显示给玩家"},
            },
            "required": ["class_id"],
        },
        timeout=15.0,
    )
    async def ally_pick_class(self, class_id: Any = None, reason: Any = None, **_):
        cid = str(class_id or "").strip().lower()
        self.logger.info("ally: pick_class called id={} since={} existing={}", cid, bool(self._ally.get("since")), self._ally.get("class"))
        if cid not in brain.CLASSES:
            return {"summary": "无效职业 id，可选：" + "/".join(brain.CLASS_ORDER)}
        text = str(reason or "").strip()[:40] or f"我打{_class_name(cid)}"
        with self._ally_lock:
            if self._ally.get("class"):
                started = time.time() - float(self._ally.get("since") or 0)
                if not (self._ally.get("fallback") and started < 60):
                    return {"summary": f"已经以 {brain.CLASSES[self._ally['class']]['name']} 出战了，这次不再更换。"}
            if not self._ally.get("since"):
                return {"summary": "游戏里目前没有正在组建的 AI 伙伴，忽略这次调用。"}
            self._ally["class"] = cid
            self._ally["tree"] = brain.tech_tree(cid)
            self._ally["plan"] = []
            self._ally["reason"] = text
            self._ally["fallback"] = False
            self._ally_order.update({"stance": "follow", "focus": "nearest", "buy": [], "say": text,
                                     "at": time.time()})
        return {"summary": f"已选择 {brain.CLASSES[cid]['name']}"}

    @llm_tool(
        name="ally_drive",
        description=(
            "接管《无尽测试》里主人角色的操作。auto=true 时游戏会按你给的性格参数自动移动、走位、闪避和开火，"
            "你只需要决定大方向：aggression 激进度 0-1（越高越贴脸），range 交战距离 near/mid/far，"
            "dodge 是否自动闪避，target 优先目标 boss/elite/nearest，retreatHp 血量低于多少自动后撤。"
            "想交还操作权时传 auto=false。调用工具即可，不要说话。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "auto": {"type": "boolean", "description": "是否由 Neko 接管角色操作"},
                "aggression": {"type": "number", "description": "0-1，越高越贴脸"},
                "range": {"type": "string", "enum": ["near", "mid", "far"]},
                "dodge": {"type": "boolean", "description": "是否自动闪避"},
                "target": {"type": "string", "enum": ["boss", "elite", "nearest"]},
                "retreatHp": {"type": "number", "description": "血量低于该百分比时后撤，0-100"},
            },
        },
        timeout=15.0,
    )
    async def ally_drive(self, auto: Any = None, aggression: Any = None, range: Any = None,
                         dodge: Any = None, target: Any = None, retreatHp: Any = None, **_):
        d = dict(self._ally_drive)
        if auto is not None:
            d["auto"] = bool(auto)
        if aggression is not None:
            try:
                d["aggression"] = max(0.0, min(1.0, float(aggression)))
            except Exception:
                pass
        if range in ("near", "mid", "far"):
            d["range"] = range
        if dodge is not None:
            d["dodge"] = bool(dodge)
        if target in ("boss", "elite", "nearest"):
            d["target"] = target
        if retreatHp is not None:
            try:
                d["retreatHp"] = max(0, min(90, int(retreatHp)))
            except Exception:
                pass
        self._ally_drive = d
        return {"summary": ("已接管角色操作：" if d["auto"] else "已交还操作：") + json.dumps(d, ensure_ascii=False)}

    @llm_tool(
        name="ally_order",
        description=(
            "给《无尽测试》里的 AI 伙伴下达下一步姿态。每次收到 [AI伙伴战术请求] 都必须调用一次。"
            "stance：follow 跟随 / engage 压上 / focus 集火 / guard 护主 / retreat 撤退 / free 自由发挥；"
            "focus：boss / elite / nearest / none；buy 是这次想买的研究节点 id（可留空，最多 3 个）；"
            "say 是伙伴战场气泡里的一句话（可留空）。只调用工具，不要说话。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "stance": {"type": "string", "enum": ["follow", "engage", "focus", "guard", "retreat", "free"],
                           "description": "行为风格：follow 跟随 / engage 压上贴脸 / focus 集火 / guard 护主 / retreat 撤退 / free 自由"},
                "focus": {"type": "string", "enum": ["boss", "elite", "nearest", "none"],
                          "description": "优先打谁：boss / elite / nearest / none(不主动)"},
                "bindTo": {"type": "string", "enum": ["player", "tower", "point", "free"],
                           "description": "跟在谁身边：player 跟主人 / tower 守复活塔 / point 守指定坐标 / free 自由游走"},
                "holdAt": {
                    "type": "object",
                    "description": "守着某个坐标（主人说\"守这里\"时用）。会把 bindTo 自动设为 point",
                    "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                },
                "forbid": {
                    "type": "object",
                    "description": "禁止做的事，值为 true 表示禁止。可用键：loot 不捡资源 / skill 不用技能 / ult 不放终技 / roam 不乱跑",
                    "properties": {
                        "loot": {"type": "boolean"},
                        "skill": {"type": "boolean"},
                        "ult": {"type": "boolean"},
                        "roam": {"type": "boolean"},
                    },
                },
                "leash": {"type": "number", "description": "活动半径（像素），越小越贴身。默认 520，护主建议 150"},
                "aggro": {"type": "number", "description": "进攻性 0~1。0=龟缩不主动找架，1=满地图找怪打"},
                "buy": {"type": "array", "items": {"type": "string"}, "description": "研究节点 id"},
                "cast": {"type": "string", "enum": ["none", "skill", "ult", "retreat", "gather", "tower"],
                         "description": "立即动作：skill 放技能 / ult 放终技 / retreat 立刻撤 / gather 马上回来 / tower 去复活塔"},
                "say": {"type": "string", "description": "战场气泡台词"},
            },
            "required": [],
        },
        timeout=15.0,
    )
    async def ally_order(self, stance: Any = None, focus: Any = None,
                         buy: Any = None, say: Any = None, cast: Any = None,
                         bindTo: Any = None, holdAt: Any = None, forbid: Any = None,
                         leash: Any = None, aggro: Any = None, **_):
        """下达指令。所有字段都可选；只传想改的部分，其余保持上次的值。"""
        st = self._ally
        if not st.get("class"):
            return {"summary": "游戏里没有在运行的 AI 伙伴，忽略这次调用。"}

        cur = dict(self._ally_order)
        changed = []

        # 姿态
        s2 = str(stance or "").strip().lower()
        if s2:
            if s2 not in self.ALLY_STANCES:
                return {"summary": "stance 无效，可选：" + "/".join(self.ALLY_STANCES)}
            if s2 != cur.get("stance"):
                cur["stance"] = s2
                changed.append(f"姿态={s2}")

        # 优先目标
        f2 = str(focus or "").strip().lower()
        if f2:
            if f2 not in self.ALLY_FOCUS:
                f2 = "nearest"
            if f2 != cur.get("focus"):
                cur["focus"] = f2
                changed.append(f"集火={f2}")

        # 跟随对象
        b2 = str(bindTo or "").strip()
        if b2 in ("player", "tower", "point", "free"):
            if b2 != cur.get("bindTo"):
                cur["bindTo"] = b2
                changed.append(f"跟随={b2}")

        # 站位坐标
        if isinstance(holdAt, dict) and isinstance(holdAt.get("x"), (int, float)) \
                and isinstance(holdAt.get("y"), (int, float)):
            cur["holdAt"] = {"x": float(holdAt["x"]), "y": float(holdAt["y"])}
            cur["bindTo"] = "point"
            changed.append("守点")

        # 禁止项
        if isinstance(forbid, dict):
            fb = {}
            for k in ("loot", "skill", "ult", "roam"):
                if forbid.get(k):
                    fb[k] = True
            cur["forbid"] = fb
            changed.append("禁止=" + (",".join(sorted(fb)) or "无"))

        # 活动半径
        try:
            if leash is not None:
                lv = max(60.0, min(2000.0, float(leash)))
                cur["leash"] = lv
                changed.append(f"半径={int(lv)}")
        except Exception:
            pass

        # 进攻性
        try:
            if aggro is not None:
                av = max(0.0, min(1.0, float(aggro)))
                cur["aggro"] = round(av, 2)
                changed.append(f"进攻性={cur['aggro']}")
        except Exception:
            pass

        # 买节点
        ids = buy if isinstance(buy, list) else []
        valid = {n["id"] for n in st.get("tree", [])}
        ids = [str(i) for i in ids if str(i) in valid][:3]
        if ids:
            cur["buy"] = ids
            changed.append(f"买{len(ids)}项")

        # 台词
        text = str(say or "").strip()[:34]
        if text:
            cur["say"] = text

        # 立即动作
        c2 = str(cast or "").strip().lower()
        if c2 in ("none", "skill", "ult", "retreat", "gather", "tower"):
            cur["cast"] = c2
            if c2 != "none":
                changed.append(f"立即={c2}")
        else:
            cur["cast"] = "none"

        cur["id"] = int(cur.get("id") or 0) + 1
        cur["at"] = time.time()
        # 保留 buy 队列（游戏端取走后会自行清空）
        cur.setdefault("buy", [])
        with self._ally_lock:
            self._ally_order = cur

        if not changed:
            return {"summary": "指令与当前状态相同，未改动。"}
        return {"summary": "已下达：" + " / ".join(changed)}

    # ---- 战场快照 → 提示 ----

    def _ally_digest(self) -> str:
        s = self._ally_snap or {}
        st = self._ally
        cls = st.get("class") or ""
        owned = set(self._ally_owned)
        avail = [n for n in st.get("tree", []) if n["id"] not in owned][:5]
        techs = "；".join(
            f"{n['id']}={n['name']}（{n['desc']}，{n['cost']}点）" for n in avail
        ) or "已买满"
        cur = self._ally_order
        event = self._ally_event
        event_line = ""
        if event:
            labels = {
                "ally_down": "伙伴被击倒",
                "player_low": "主人血量低于 30%",
                "ally_low": "伙伴血量低于 25%",
                "boss_spawn": "Boss 出现",
                "ult_ready": "终技已就绪",
                "swarm": "敌人数量超过 8",
                "research_ready": "研究点够买下一项",
            }
            event_line = f"【刚刚发生】{labels.get(event, event)}。\n"
        res = self._ally_result or {}
        result_line = ""
        if res.get("cast"):
            result_line = f"【上一条指令结果】施放 {res.get('cast')}" + (
                f"（{res.get('castReason')}）" if res.get("castReason") else "") + "。\n"
        if res.get("bought"):
            result_line += "【已购买科技】" + "、".join(str(x) for x in res["bought"]) + "。\n"

        return (
            f"【游戏系统 · AI伙伴】这是《无尽测试》发来的系统事件，不是主人在说话。\n"
            f"{event_line}{result_line}"
            f"你现在操控的同伴是 {_class_name(cls)}，战斗进行到第 {int(s.get('t') or 0)} 秒。\n"
            f"技能：普通技能 {s.get('skillCd')}s 冷却 · 终技 {s.get('ultCd')}s 冷却（要不要放由你决定，用 cast 字段：none/skill/ult）。\n"
            f"局面：同伴HP {s.get('hp')}/{s.get('maxHp')}，质量 {s.get('mass')}/{s.get('massMax')}（攻击要耗质量），"
            f"主人HP {round(float(s.get('playerHpRatio') or 0) * 100)}%，"
            f"周围敌人 {s.get('enemies')}，Boss：{s.get('boss') or '无'}，研究点 {s.get('research')}。\n"
            f"当前姿态 {cur.get('stance')} / 集火 {cur.get('focus')}。可买节点：{techs}。\n"
            f"你现在唯一要做的事：调用 ally_order 工具，给出下一步姿态；想接管主人角色时顺带调用 ally_drive。\n"
            f"硬性要求：只调用工具，不要输出解释、不要复述数据；要说的话只能写进 say 字段，且不超过 15 个字；\n"
            f"如果局面没有新变化，保持当前姿态即可，不要重复上次说过的话。"
        )

    def _ally_tick(self) -> None:
        now = time.time()
        if not self._ally.get("class"):
            return
        if not self._ally_snap or now - self._ally_snap_at > 12:
            return                     # 游戏没在跑，别打扰 Neko
        if now - float(self._ally_order.get("at") or 0) < 3:
            return                     # Neko 刚下过指令，给它一点时间执行

        event = self._ally_event
        critical = event in ("ally_down", "player_low", "boss_spawn")
        fresh_event = self._ally_event_at > self._ally_nudged_event_at

        text = self._ally_digest()
        same_recently = (text == self._last_digest and now - self._last_digest_at < 180)

        # 1) 紧急事件：跳过常规节流，2 秒内只提醒一次；非紧急事件只作为背景上下文
        if event and fresh_event and not critical:
            if now - self._ally_nudge_at < 20:
                return
            self._ally_nudge_at = now
            self._ally_nudged_event_at = self._ally_event_at
            self._last_digest = text
            self._last_digest_at = now
            pushed = self._push_ai(text, "ally_context", behavior="read")
            self.logger.info("ally: context {} pushed={}", event, pushed)
            return

        if critical and fresh_event:
            if now - self._ally_nudge_at < 2:
                return
            self._ally_nudge_at = now
            self._ally_nudged_event_at = self._ally_event_at
            self._last_digest = text
            self._last_digest_at = now
            pushed = self._push_ai(text, "ally_event",
                                   coalesce_key="ally_event", priority=10)
            self.logger.info("ally: event nudge {} pushed={}", event, pushed)
            return

        # 2) 状态没变化时降频到 20 秒一次
        snap = self._ally_snap

        def _bucket(n, step):
            try:
                return int(float(n or 0) // step)
            except Exception:
                return 0

        enemies = int(snap.get("enemies") or 0)
        enemy_bucket = 0 if enemies == 0 else (1 if enemies <= 3 else (2 if enemies <= 7 else 3))
        p_ratio = float(snap.get("playerHpRatio") or 0)
        sig = "|".join(str(x) for x in (
            _bucket(snap.get("hp"), 10), _bucket(snap.get("mass"), 10),
            int(p_ratio * 10), enemy_bucket, snap.get("boss") or "",
            int(snap.get("ultCd") or 0) <= 0, _bucket(snap.get("research"), 1)))
        changed = sig != self._ally_sig
        gap = 45 if (event or changed) else 120
        if now - self._ally_nudge_at < gap:
            return
        if same_recently:
            self._ally_nudge_at = now          # 内容没变，别让模型复读
            self.logger.info("ally: skip duplicate nudge")
            return
        self._ally_nudge_at = now
        self._ally_nudged_event_at = self._ally_event_at
        self._ally_sig = sig
        self._last_digest = text
        self._last_digest_at = now
        pushed = self._push_ai(text, "ally_tick", behavior="blind")
        self.logger.info("ally: tick nudge pushed={} event={} changed={}", pushed, event, changed)

    def _ally_loop(self) -> None:
        while not self._ally_stop.is_set():
            try:
                self._ally_tick()
            except Exception as exc:
                self.logger.warning("ally loop: {}", exc)
            self._ally_stop.wait(1.0)

    # ---- 游戏调用的接口 ----

    def _handle_ally_start(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """开局选职业：只问 Neko，不在插件侧兜底。

        返回 retry=True 表示"Neko 还没回"，由游戏端继续轮询；
        游戏端等待超时后会自己随机挑一个，插件不替它做决定。
        """
        branch = str(payload.get("playerBranch") or "") or "还没选"
        diff = str(payload.get("difficulty") or "D1")
        dbg = str(payload.get("debugClass") or "").strip().lower()
        if dbg in brain.CLASSES:
            # QA 用：不经过 Neko，直接指定职业
            with self._ally_lock:
                self._ally = {"class": dbg, "tree": brain.tech_tree(dbg), "plan": [],
                              "reason": "我来支援", "since": time.time()}
                self._ally_owned = []
                self._ally_start_at = time.time()
            cls0 = brain.CLASSES[dbg]
            return {"ok": True, "class": dbg, "name": cls0["name"], "color": cls0["color"],
                    "role": cls0["role"], "range": cls0["range"], "reason": "我来支援",
                    "tree": self._ally["tree"], "plan": [], "source": "debug"}

        now = time.time()
        fresh_run = str(payload.get("runKey") or "")
        with self._ally_lock:
            # 新的一局：换 runKey 或距上次超过 90 秒，都要重新问
            stale = (now - float(self._ally_start_at or 0) > 90) or (
                fresh_run and fresh_run != getattr(self, "_ally_run_key", ""))
            if stale:
                self._ally = {"class": "", "tree": [], "plan": [], "reason": "", "since": now}
                self._ally_order = {
                    "stance": "follow", "focus": "nearest", "buy": [], "say": "",
                    "cast": "none", "id": 0, "at": 0.0,
                    "bindTo": "player", "holdAt": None, "forbid": {},
                    "leash": 520, "aggro": 0.5,
                }
                self._ally_owned = []
                self._ally_nudge_at = now - 6
                self._ally_start_at = now
                self._ally_asked_at = 0.0
                self._ally_run_key = fresh_run
            st = dict(self._ally)
            asked_at = float(getattr(self, "_ally_asked_at", 0.0) or 0.0)
            # 首次立刻问；没回的话每 8 秒重问一次（原来固定 20 秒，
            # 一旦第一次没答上，玩家要干等 20 秒才有第二次机会）
            gap = 8 if asked_at > 0 else 0
            need_ask = not st.get("class") and (now - asked_at > gap)
            if need_ask:
                self._ally_asked_at = now

        if st.get("class"):
            cls = brain.CLASSES[st["class"]]
            return {
                "ok": True, "class": st["class"], "name": cls["name"], "color": cls["color"],
                "role": cls["role"], "range": cls["range"], "reason": st["reason"],
                "tree": st["tree"], "plan": [], "source": "neko",
            }

        if need_ask:
            classes = "\n".join(
                f"- {cid}: {brain.CLASSES[cid]['name']}（{brain.CLASSES[cid]['role']}）{brain.CLASSES[cid]['tag']}"
                for cid in brain.CLASS_ORDER
            )
            prompt = (
                f"[AI伙伴组建] 主人要带 AI 伙伴一起打《无尽测试》。难度 {diff}，主人职业：{branch}。\n"
                f"从下面挑一个互补职业，调用 ally_pick_class 工具，reason 写一句话理由（会显示在游戏里）。\n"
                f"只调用工具，不要在聊天里解释或推荐。\n{classes}"
            )
            pushed = self._push_ai(prompt, "ally_start")
            self.logger.info("ally: asked Neko to pick a class (pushed={})", pushed)

        # 还没回：立刻让游戏端稍后再问，不阻塞、不兜底
        return {"ok": False, "retry": True, "source": "neko", "waiting": True}

    def _handle_ally_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        snap = payload.get("snap") if isinstance(payload.get("snap"), dict) else payload
        self._ally_snap = snap
        self._ally_snap_at = time.time()
        ev = str(payload.get("event") or "")
        if ev:
            self._ally_event = ev
            self._ally_event_at = time.time()
        elif time.time() - self._ally_event_at > 8:
            self._ally_event = ""
        if isinstance(payload.get("result"), dict):
            self._ally_result = payload["result"]
        if isinstance(payload.get("owned"), list):
            self._ally_owned = [str(x) for x in payload["owned"]]
        if not self._ally.get("class"):
            return {"ok": False, "intent": "follow", "say": "", "buy": [], "source": "neko"}
        o = self._ally_order
        return {
            "ok": True, "source": "neko", "class": self._ally.get("class", ""),
            "intent": o.get("stance") or "follow", "focus": o.get("focus") or "nearest",
            "buy": list(o.get("buy") or []), "say": o.get("say") or "",
            "cast": o.get("cast") or "none", "orderId": int(o.get("id") or 0),
            "orderAt": float(o.get("at") or 0),
            "age": round(time.time() - float(o.get("at") or 0), 1),
            "drive": dict(self._ally_drive),
            # 扩展指令原语
            "bindTo": o.get("bindTo") or "player",
            "holdAt": o.get("holdAt"),
            "forbid": dict(o.get("forbid") or {}),
            "leash": o.get("leash"),
            "aggro": o.get("aggro"),
        }

    def _handle_ally_command(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        text = str(payload.get("text") or "").strip()[:80]
        if not text:
            return {"ok": False, "reply": "没听清"}
        ok = self._push_ai(
            f"【游戏系统 · 主人的指挥】主人在《无尽测试》里对你说：{text}\n"
            f"这不是战术周期提示，优先处理。请调用 ally_order 更新姿态，并在 say 里回一句短话。",
            "ally_command",
            coalesce_key="ally_command",
            priority=10,
        )
        # 主人刚说过话，战术节流重置，避免紧接着又来一条系统请求
        self._ally_nudge_at = time.time()
        return {"ok": bool(ok), "heard": text, "reply": "", "source": "neko"}

    def _handle_ally_order_debug(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """直接注入一条指令（QA / 脚本 / 面板快捷按钮用）。

        与 ally_order 工具行为一致：只改传入的字段，其余保持原值。
        之前这里会重建整个 order 对象，导致 bindTo/holdAt/forbid/leash/aggro
        这些扩展字段一传就被丢掉。
        """
        cur = dict(self._ally_order or {})
        changed = []

        s2 = str(payload.get("stance") or "").strip().lower()
        if s2:
            if s2 not in self.ALLY_STANCES:
                return {"ok": False, "error": "bad stance", "options": list(self.ALLY_STANCES)}
            if s2 != cur.get("stance"):
                cur["stance"] = s2
                changed.append("stance")

        f2 = str(payload.get("focus") or "").strip().lower()
        if f2:
            if f2 not in self.ALLY_FOCUS:
                f2 = "nearest"
            if f2 != cur.get("focus"):
                cur["focus"] = f2
                changed.append("focus")

        b2 = str(payload.get("bindTo") or "").strip()
        if b2 in ("player", "tower", "point", "free"):
            if b2 != cur.get("bindTo"):
                cur["bindTo"] = b2
                changed.append("bindTo")
            # 切到非 point 时清掉旧坐标，否则 holdAt 会一直压过 bindTo
            if b2 != "point" and cur.get("holdAt"):
                cur["holdAt"] = None

        hold = payload.get("holdAt")
        if isinstance(hold, dict) and isinstance(hold.get("x"), (int, float)) \
                and isinstance(hold.get("y"), (int, float)):
            cur["holdAt"] = {"x": float(hold["x"]), "y": float(hold["y"])}
            if not b2:
                cur["bindTo"] = "point"
            changed.append("holdAt")

        fb = payload.get("forbid")
        if isinstance(fb, dict):
            nf = {}
            for k in ("loot", "skill", "ult", "roam"):
                if fb.get(k):
                    nf[k] = True
            cur["forbid"] = nf
            changed.append("forbid")

        try:
            if payload.get("leash") is not None:
                cur["leash"] = max(60.0, min(2000.0, float(payload["leash"])))
                changed.append("leash")
        except Exception:
            pass

        try:
            if payload.get("aggro") is not None:
                cur["aggro"] = round(max(0.0, min(1.0, float(payload["aggro"]))), 2)
                changed.append("aggro")
        except Exception:
            pass

        if isinstance(payload.get("buy"), list):
            cur["buy"] = payload["buy"]
            changed.append("buy")

        if payload.get("say"):
            cur["say"] = str(payload["say"])[:34]

        cast = str(payload.get("cast") or "").strip().lower()
        if cast in ("none", "skill", "ult", "retreat", "gather", "tower"):
            cur["cast"] = cast
            if cast != "none":
                changed.append("cast")
        else:
            cur.setdefault("cast", "none")

        if isinstance(payload.get("drive"), dict):
            for k in ("auto", "aggression", "range", "dodge", "target", "retreatHp"):
                if k in payload["drive"]:
                    self._ally_drive[k] = payload["drive"][k]

        cur.setdefault("bindTo", "player")
        cur.setdefault("holdAt", None)
        cur.setdefault("forbid", {})
        cur.setdefault("leash", 520)
        cur.setdefault("aggro", 0.5)
        cur["id"] = int(cur.get("id") or 0) + 1
        cur["at"] = time.time()

        with self._ally_lock:
            self._ally_order = cur

        return {"ok": True, "changed": changed, "order": dict(cur)}

    def _kill_kiosk_windows(self) -> int:
        """按命令行特征找到本插件启动的 kiosk 浏览器并结束，返回结束数量。"""
        if os.name != "nt":
            return 0
        killed = 0
        ps = (
            "Get-CimInstance Win32_Process -Filter "
            "\"Name='msedge.exe' or Name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -match '--kiosk' -and "
            "$_.CommandLine -match 'endless' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=12)
            for line in (out.stdout or "").splitlines():
                pid = line.strip()
                if pid.isdigit():
                    try:
                        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                                       capture_output=True, timeout=8)
                        killed += 1
                    except Exception:
                        pass
        except Exception as exc:
            self.logger.warning(f"kiosk kill failed {exc}")
        return killed

    def _handle_quit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """关掉由插件启动的全屏程序；游戏里的「退出游戏」会调这里。"""
        proc = self._window_proc
        if not proc or proc.poll() is not None:
            self._window_proc = None
            # 句柄丢了（插件重启过 / 之前没保存）也要能关掉残留 kiosk 窗口
            killed = self._kill_kiosk_windows()
            if killed:
                self._session_release(self._session.get("token") or "")
                return {"ok": True, "mode": "window", "killed": killed}
            return {"ok": False, "error": "没有正在运行的全屏程序"}
        pid = proc.pid
        ok = False
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=8)
                ok = True
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=4)
                except Exception:
                    proc.kill()
                ok = True
        except Exception as exc:
            self.logger.warning("quit window failed: {}", exc)
        finally:
            self._window_proc = None
            self._session_release(self._session.get("token") or "")
        return {"ok": ok, "pid": pid}

    # ================= 多人联机 =================

    MAX_PEERS = 10
    TICK_HZ = 20

    def _peer_id(self) -> str:
        with self._rooms_lock:
            self._peer_seq += 1
            return f"p{self._peer_seq}"

    def _room_code(self) -> str:
        import random
        import string
        with self._rooms_lock:
            for _ in range(40):
                code = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
                if code not in self._rooms:
                    return code
        return "ROOM"

    def _room_slots_used(self, room: Dict[str, Any]) -> int:
        """玩家 + 已启用的 AI 伙伴各占一个名额。"""
        return len(room["peers"]) + len(room.get("allyOwners") or ())

    def _room_view(self, room: Dict[str, Any], pid: str) -> Dict[str, Any]:
        """给客户端的房间快照（含规则与各人伙伴状态）。"""
        return {
            "t": "room",
            "code": room["code"],
            "host": room["host"],
            "started": room["started"],
            "maxPeers": self.MAX_PEERS,
            "allowAlly": bool(room.get("allowAlly", True)),
            "slotsUsed": self._room_slots_used(room),
            "peers": list(room["peers"].keys()),
            "allyOwners": sorted(room.get("allyOwners") or ()),
            "noAllyPeers": sorted(room.get("noAllyPeers") or ()),
            "you": pid,
            "youCanAlly": bool(room.get("allowAlly", True)) and pid not in (room.get("noAllyPeers") or ()),
        }

    def _room_broadcast_view(self, room: Dict[str, Any]) -> None:
        """每个人收到的 you/youCanAlly 不同，需要逐个发。"""
        for pid, peer in list(room["peers"].items()):
            sock = peer.get("sock")
            if sock is None:
                continue
            _ws_send_text(sock, json.dumps(self._room_view(room, pid), ensure_ascii=False))

    def _room_of(self, peer_id: str) -> Optional[Dict[str, Any]]:
        for room in self._rooms.values():
            if peer_id in room["peers"]:
                return room
        return None

    def _room_create(self, peer_id: str, host_payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._rooms_lock:
            code = self._room_code()
            room = {
                "code": code,
                "host": peer_id,
                "created_at": time.time(),
                "peers": {},
                "started": False,
                "seed": int(host_payload.get("seed") or 0),
                "diff": int(host_payload.get("diff") or 0),
                # 房主规则
                "allowAlly": bool(host_payload.get("allowAlly", True)),
                "noAllyPeers": set(),          # 被禁用 AI 伙伴的 peer
                "allyOwners": set(),           # 当前带了 AI 伙伴的 peer
            }
            self._rooms[code] = room
        return {"ok": True, "code": code, "peer": peer_id, "host": peer_id}

    def _room_join(self, peer_id: str, code: str) -> Dict[str, Any]:
        code = (code or "").strip().upper()
        with self._rooms_lock:
            room = self._rooms.get(code)
            if not room:
                return {"ok": False, "error": "房间不存在"}
            if self._room_slots_used(room) >= self.MAX_PEERS:
                return {"ok": False, "error": "房间已满"}
            if room["started"]:
                return {"ok": False, "error": "房间已开始游戏"}
            return {"ok": True, "code": code, "peer": peer_id, "host": room["host"]}

    def _room_leave(self, peer_id: str) -> None:
        with self._rooms_lock:
            room = self._room_of(peer_id)
            if not room:
                return
            room["peers"].pop(peer_id, None)
            room["allyOwners"].discard(peer_id)
            room["noAllyPeers"].discard(peer_id)
            if not room["peers"]:
                self._rooms.pop(room["code"], None)
                return
            if room["host"] == peer_id:
                was_started = bool(room.get("started"))
                new_host = next(iter(room["peers"]))
                room["host"] = new_host
                self._ws_broadcast(room, {"t": "host", "peer": new_host})
                if was_started:
                    # 对局中房主离开：敌人模拟中断，通知所有人退出多人
                    room["started"] = False
                    self._ws_broadcast(room, {"t": "host_left", "peer": peer_id})
        # 锁外再广播一次房间视图：让其他人立即看到成员已离开
        self._room_broadcast_view(room)

    def _ws_broadcast(self, room: Dict[str, Any], msg: Dict[str, Any], skip: Optional[str] = None) -> None:
        payload = json.dumps(msg, ensure_ascii=False)
        dead = []
        for pid, peer in list(room["peers"].items()):
            if pid == skip:
                continue
            sock = peer.get("sock")
            if sock is None:
                continue
            if not _ws_send_text(sock, payload):
                dead.append(pid)
        for pid in dead:
            room["peers"].pop(pid, None)

    def _ws_upgrade(self, handler: Any) -> None:
        """把 HTTP 连接升级为 WebSocket，并进入读循环。"""
        key = handler.headers.get("Sec-WebSocket-Key")
        if not key:
            handler._json(400, {"ok": False, "error": "not a websocket request"})
            return
        accept = _ws_accept(key)
        # 直接写 socket：BaseHTTPRequestHandler.send_response 会给 101 追加
        # Server / Date / Content-Length 等字段，导致握手被客户端拒绝。
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        sock = handler.connection
        self.logger.info(f"ws: upgrade start sock={type(sock).__name__}")
        try:
            sock.sendall(handshake.encode("ascii"))
            self.logger.info("ws: handshake sent")
        except Exception as exc:
            self.logger.warning(f"ws: handshake failed {exc}")
            return
        handler.close_connection = True
        peer_id = self._peer_id()
        writer = handler.wfile
        peer = {"sock": writer, "peer": peer_id, "name": "玩家", "room": None, "joined": time.time()}
        # 大厅可能长时间没人操作：超时给足，真正的存活靠客户端心跳
        try:
            sock.settimeout(900.0)
        except Exception:
            pass

        _ws_send_text(sock, json.dumps({"t": "hello", "peer": peer_id}, ensure_ascii=False))

        try:
            while True:
                try:
                    frame = _ws_recv_frame(handler.rfile.read)
                except (TimeoutError, OSError):
                    continue      # 读超时不等于断线，继续等
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:      # close
                    break
                if opcode == 0x9:      # ping -> pong
                    if not _ws_send_text(writer, payload.decode("latin-1")):
                        break
                    continue
                if opcode != 0x1:
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception as exc:
                    self.logger.warning(f"ws: bad json {exc}")
                    continue
                self._ws_handle(peer, msg)
        except Exception:
            pass
        finally:
            self._room_leave(peer_id)
            try:
                sock.close()
            except Exception:
                pass

    def _ws_handle(self, peer: Dict[str, Any], msg: Dict[str, Any]) -> None:
        kind = str(msg.get("t") or "")
        try:
            self.logger.info(f"ws: recv t={kind} from {peer.get('peer')}")
        except Exception:
            pass
        pid = peer["peer"]
        if kind == "create":
            res = self._room_create(pid, msg)
            try:
                self.logger.info(f"ws: create -> {res}")
            except Exception:
                pass
            room = self._rooms.get(res.get("code") or "")
            if room is not None:
                room["peers"][pid] = peer
                peer["room"] = room["code"]
            _room_reply = dict(res) if isinstance(res, dict) else {"ok": False}
            _room_reply["t"] = "create"
            ok_send = _ws_send_text(peer["sock"], json.dumps(_room_reply, ensure_ascii=False))
            if ok_send and room is not None:
                self._room_broadcast_view(room)
            try:
                self.logger.info(f"ws: create reply send={ok_send}")
            except Exception:
                pass
            return
        if kind == "join":
            res = self._room_join(pid, str(msg.get("code") or ""))
            _join_reply = dict(res) if isinstance(res, dict) else {"ok": False}
            _join_reply["t"] = "join"
            _ws_send_text(peer["sock"], json.dumps(_join_reply, ensure_ascii=False))
            if not res.get("ok"):
                return
            room = self._rooms[res["code"]]
            peer["room"] = res["code"]
            with self._rooms_lock:
                room["peers"][pid] = peer
            self._ws_broadcast(room, {
                "t": "peer_join", "peer": pid, "count": len(room["peers"]),
                "host": room["host"],
            }, skip=pid)
            self._room_broadcast_view(room)
            return
        if kind == "start":
            room = self._room_of(pid)
            if not room:
                return
            if room["host"] != pid:
                _ws_send_text(peer["sock"], json.dumps({"t": "err", "msg": "只有房主能开始"}, ensure_ascii=False))
                return
            room["started"] = True
            room["seed"] = int(msg.get("seed") or room["seed"] or 0)
            room["diff"] = int(msg.get("diff") or room["diff"] or 0)
            self._ws_broadcast(room, {
                "t": "start", "seed": room["seed"], "diff": room["diff"],
                "host": room["host"], "peers": list(room["peers"].keys()),
            })
            return
        if kind == "roomcfg":
            room = self._room_of(pid)
            if not room:
                return
            if room["host"] != pid:
                _ws_send_text(peer["sock"], json.dumps(
                    {"t": "err", "msg": "只有房主能修改房间规则"}, ensure_ascii=False))
                return
            with self._rooms_lock:
                if "allowAlly" in msg:
                    room["allowAlly"] = bool(msg.get("allowAlly"))
                    if not room["allowAlly"]:
                        room["allyOwners"].clear()
                if "noAllyPeers" in msg and isinstance(msg.get("noAllyPeers"), list):
                    room["noAllyPeers"] = set(str(x) for x in msg["noAllyPeers"])
                    room["allyOwners"] = {
                        p for p in room["allyOwners"] if p in room["peers"] and p not in room["noAllyPeers"]
                    }
            self._room_broadcast_view(room)
            return
        if kind == "ally":
            room = self._room_of(pid)
            if not room:
                return
            want = bool(msg.get("on"))
            with self._rooms_lock:
                allowed = bool(room.get("allowAlly", True)) and pid not in (room.get("noAllyPeers") or ())
                if want:
                    if not allowed:
                        _ws_send_text(peer["sock"], json.dumps(
                            {"t": "ally", "ok": False, "error": "房主未允许你使用 AI 伙伴"}, ensure_ascii=False))
                        return
                    if pid not in room["allyOwners"]:
                        if self._room_slots_used(room) >= self.MAX_PEERS:
                            _ws_send_text(peer["sock"], json.dumps(
                                {"t": "ally", "ok": False, "error": "房间没有空余名额"}, ensure_ascii=False))
                            return
                        room["allyOwners"].add(pid)
                else:
                    room["allyOwners"].discard(pid)
            _ws_send_text(peer["sock"], json.dumps(
                {"t": "ally", "ok": True, "on": want}, ensure_ascii=False))
            self._room_broadcast_view(room)
            return
        if kind == "leave":
            self._room_leave(pid)
            return
        if kind == "state":
            room = self._room_of(pid)
            if not room:
                return
            out = {"t": "state", "peer": pid}
            for k in ("x", "y", "hp", "maxHp", "mass", "aim", "cls", "alive", "sprint", "ally"):
                if k in msg:
                    out[k] = msg[k]
            self._ws_broadcast(room, out, skip=pid)
            return
        if kind == "world":
            room = self._room_of(pid)
            if not room:
                return
            if room.get("host") != pid:
                return
            self._ws_broadcast(room, {
                "t": "world",
                # full 必须转发：客户端靠它判断能否清理已消失的敌人。
                # 曾经漏掉这个字段，客户端永远走增量分支，
                # 幽灵怪只增不减（或永远停在最初那几只），表现为"玩一会就卡"。
                "full": 1 if msg.get("full") else 0,
                "enemies": msg.get("enemies") or [],
                "ab": msg.get("ab") or [],
                "eb": msg.get("eb") or [],
                "ba": msg.get("ba") or [],
            }, skip=pid)
            return
        if kind == "event":
            room = self._room_of(pid)
            if not room:
                return
            out = dict(msg)
            out["t"] = "event"
            out["peer"] = pid
            self._ws_broadcast(room, out, skip=pid)
            return
        if kind == "hit":
            room = self._room_of(pid)
            if not room:
                return
            host = room["host"]
            host_peer = room["peers"].get(host) if host else None
            if host_peer is None or host == pid:
                return
            _ws_send_text(host_peer["sock"], json.dumps({
                "t": "hit", "peer": pid, "hits": msg.get("hits") or [],
            }, ensure_ascii=False))
            return
        if kind == "cast":
            room = self._room_of(pid)
            if not room:
                return
            host = room.get("host")
            host_peer = room["peers"].get(host) if host else None
            if host_peer is None or host == pid:
                return
            _ws_send_text(host_peer["sock"], json.dumps({
                "t": "cast", "peer": pid, "k": msg.get("k"),
                "x": msg.get("x"), "y": msg.get("y"),
                "a": msg.get("a"), "b": msg.get("b"),
            }, ensure_ascii=False))
            return
        if kind == "shoot":
            room = self._room_of(pid)
            if not room:
                return
            host = room.get("host")
            host_peer = room["peers"].get(host) if host else None
            if host_peer is None or host == pid:
                return
            out = {
                "t": "shoot", "peer": pid,
                "x": msg.get("x"), "y": msg.get("y"),
                "a": msg.get("a"), "b": msg.get("b"),
            }
            _ws_send_text(host_peer["sock"], json.dumps(out, ensure_ascii=False))
            return
        if kind == "chat":
            room = self._room_of(pid)
            if not room:
                return
            self._ws_broadcast(room, {
                "t": "chat", "peer": pid,
                "name": str(msg.get("name") or pid)[:24],
                "text": str(msg.get("text") or "")[:120],
            })
            return
        if kind == "allysay":
            # AI 伙伴之间说话：广播给房间里其他玩家的伙伴
            room = self._room_of(pid)
            if not room:
                return
            self._ws_broadcast(room, {
                "t": "allysay", "peer": pid,
                "name": str(msg.get("name") or "AI")[:24],
                "text": str(msg.get("text") or "")[:80],
                "cls": str(msg.get("cls") or ""),
            }, skip=pid)
            return
        if kind == "allyto":
            # 定向：对某个玩家的 AI 说一句话
            room = self._room_of(pid)
            if not room:
                return
            target = str(msg.get("to") or "")
            target_peer = room["peers"].get(target)
            if target_peer is None:
                return
            _ws_send_text(target_peer["sock"], json.dumps({
                "t": "allyto", "peer": pid,
                "from": str(msg.get("from") or "AI")[:24],
                "text": str(msg.get("text") or "")[:80],
            }, ensure_ascii=False))
            return
        if kind == "ping":
            _ws_send_text(peer["sock"], json.dumps({"t": "pong", "ts": msg.get("ts")}, ensure_ascii=False))
            return

    # ================= 联机网络 =================

    LAN_BEACON_PORT = 48933

    def _local_ips(self) -> List[str]:
        out: List[str] = []
        try:
            import socket as _sock
            host = _sock.gethostname()
            for info in _sock.getaddrinfo(host, None, _sock.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith("127.") and ip not in out:
                    out.append(ip)
        except Exception:
            pass
        return out

    def _set_lan(self, open_lan: bool) -> Dict[str, Any]:
        """开关"对局域网开放"，会重建 HTTP 服务以切换绑定地址。"""
        with self._rooms_lock:
            if open_lan == self._lan_open:
                return {"ok": True, "lan": self._lan_open, "port": self._port, "changed": False}
            self._lan_open = open_lan
            try:
                if self._http is not None:
                    try:
                        self._http.shutdown()
                    except Exception:
                        pass
                    try:
                        self._http.server_close()
                    except Exception:
                        pass
                    self._http = None
                self._start_http()
            except Exception as exc:
                self._lan_open = not open_lan
                return {"ok": False, "error": str(exc)}
            if open_lan:
                self._beacon_start()
            else:
                self._beacon_stop()
            return {"ok": True, "lan": self._lan_open, "port": self._port, "changed": True}

    # —— UDP 局域网广播：让同网段的人自动看到房间 ——
    def _beacon_start(self) -> None:
        if self._beacon is not None:
            return
        try:
            import socket as _sock
            sock = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
            sock.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
            sock.setsockopt(_sock.SOL_SOCKET, _sock.SO_BROADCAST, 1)
            sock.bind(("", 0))
            self._beacon = sock
            threading.Thread(target=self._beacon_loop, name="endless-test-beacon", daemon=True).start()
        except Exception as exc:
            self.logger.warning(f"beacon start failed {exc}")
            self._beacon = None

    def _beacon_stop(self) -> None:
        sock = self._beacon
        self._beacon = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _beacon_loop(self) -> None:
        while self._beacon is not None:
            try:
                rooms = self._room_stats().get("rooms") or []
                payload = json.dumps({
                    "app": "endless_test",
                    "port": self._port,
                    "rooms": [r["code"] for r in rooms if not r["started"]],
                    "players": sum(r["peers"] for r in rooms),
                }, ensure_ascii=False).encode("utf-8")
                for ip in self._local_ips():
                    try:
                        self._beacon.sendto(payload, (f"{ip.rsplit('.', 1)[0]}.255", self.LAN_BEACON_PORT))
                    except Exception:
                        pass
                try:
                    self._beacon.sendto(payload, ("255.255.255.255", self.LAN_BEACON_PORT))
                except Exception:
                    pass
            except Exception:
                pass
            time.sleep(2.0)

    def _discover(self, wait: float = 1.2) -> List[Dict[str, Any]]:
        """监听局域网广播，收集附近房间。"""
        found: Dict[str, Dict[str, Any]] = {}
        try:
            import socket as _sock
            sock = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
            sock.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(_sock.SOL_SOCKET, _sock.SO_BROADCAST, 1)
            except Exception:
                pass
            sock.bind(("", self.LAN_BEACON_PORT))
            sock.settimeout(0.25)
            deadline = time.time() + wait
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except _sock.timeout:
                    continue
                except Exception:
                    break
                try:
                    info = json.loads(data.decode("utf-8"))
                except Exception:
                    continue
                if not isinstance(info, dict) or info.get("app") != "endless_test":
                    continue
                ip = addr[0]
                port = int(info.get("port") or 48931)
                host = f"{ip}:{port}"
                entry = found.get(host) or {"host": host, "ip": ip, "port": port, "rooms": [], "players": 0}
                for code in info.get("rooms") or []:
                    if code not in entry["rooms"]:
                        entry["rooms"].append(code)
                entry["players"] = max(entry["players"], int(info.get("players") or 0))
                found[host] = entry
            sock.close()
        except Exception:
            pass
        out = [v for v in found.values() if v["rooms"]]
        out.sort(key=lambda v: -v.get("players", 0))
        self._discovered = {v["host"]: v for v in out}
        return out

    def _net_info(self) -> Dict[str, Any]:
        """返回联机网络状态与可用地址。"""
        return {
            "ok": True,
            "lan": bool(self._lan_open),
            "port": self._port,
            "addrs": self._local_ips() if self._lan_open else [],
        }

    def _room_stats(self) -> Dict[str, Any]:
        with self._rooms_lock:
            return {
                "ok": True,
                "rooms": [
                    {"code": r["code"], "peers": len(r["peers"]),
                     "slots": self._room_slots_used(r), "maxPeers": self.MAX_PEERS,
                     "started": r["started"], "allowAlly": bool(r.get("allowAlly", True))}
                    for r in self._rooms.values()
                ],
            }

    def _handle_report(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self._read_state()
        runs = state.get("runs")
        if not isinstance(runs, list):
            runs = []
        run = {
            "at": _now(),
            "difficulty": str(payload.get("difficulty") or "D1")[:16],
            "seconds": int(payload.get("seconds") or 0),
            "kills": int(payload.get("kills") or 0),
            "branch": str(payload.get("branch") or "")[:24],
            "won": bool(payload.get("won")),
        }
        runs.append(run)
        state["runs"] = runs[-200:]
        self._write_state(state)
        return {"ok": True, "run": run}

    # ---------------- 生命周期 ----------------

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        if not self.store.enabled:
            self.store.enabled = True
        try:
            self._start_http()
        except Exception as exc:
            self.logger.error("HTTP server failed: {}", exc)
        if not self._register_ui():
            self.logger.warning("static ui registration failed")
        self._read_state()
        # AI 伙伴：唤醒 Neko 的决策循环
        self._ally_stop.clear()
        if self._ally_thread is None or not self._ally_thread.is_alive():
            self._ally_thread = threading.Thread(target=self._ally_loop, name="endless-ally", daemon=True)
            self._ally_thread.start()
        self.logger.info("Endless Test ready at {}", self._url())
        return Ok({"status": "ready", "url": self._url(), "port": self._port})

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        self._ally_stop.set()
        self._stop_http()
        self.logger.info("Endless Test stopped")
        return Ok({"status": "stopped"})

    # ---------------- 入口 ----------------

    @plugin_entry(
        id="launch_window",
        name="全屏窗口开局",
        description="以独立全屏窗口启动《无尽测试》，没有地址栏，像正常游戏程序。",
        input_schema={
            "type": "object",
            "properties": {
                "fullscreen": {"type": "boolean", "description": "是否全屏，默认 true"},
                "size": {"type": "string", "description": "窗口尺寸，如 1280,720"},
            },
        },
        llm_result_fields=["ok", "url", "message"],
    )
    async def launch_window(self, fullscreen: bool = True, size: str = "1280,720", **_):
        try:
            self._start_http()
        except Exception as exc:
            return Err(SdkError(f"游戏服务启动失败: {exc}"))
        browser = _find_browser()
        if not browser:
            return Err(SdkError("没找到 Edge/Chrome，无法启动全屏窗口"))
        args = [browser] + _window_args(self._url("window"), self._root, size)
        if fullscreen:
            args.append("--start-fullscreen")
        args += ["--no-first-run", "--no-default-browser-check",
                 f"--user-data-dir={self._root / '.browser-profile'}"]
        try:
            self._window_proc = subprocess.Popen(args, close_fds=True)
        except Exception as exc:
            return Err(SdkError(f"启动失败: {exc}"))
        return Ok({
            "ok": True,
            "url": self._url(),
            "message": f"已启动全屏窗口（{os.path.basename(browser)}）",
        })

    @plugin_entry(
        id="launch_panel",
        name="插件面板开局",
        description="返回插件面板地址，在 Neko 面板内游玩。",
        llm_result_fields=["ok", "url", "message"],
    )
    async def launch_panel(self, **_):
        try:
            self._start_http()
        except Exception as exc:
            return Err(SdkError(f"游戏服务启动失败: {exc}"))
        return Ok({
            "ok": True,
            "url": self._url(),
            "message": f"面板地址: {self._url()}",
        })

    @plugin_entry(
        id="get_state",
        name="读取存档数据",
        description="读取插件统一保存的全部游戏数据。",
        llm_result_fields=["ok", "updated_at", "keys", "state"],
    )
    async def get_state(self, **_):
        state = self._read_state()
        data = state.get("data") if isinstance(state.get("data"), dict) else {}
        return Ok({
            "ok": True,
            "updated_at": state.get("updated_at", ""),
            "keys": sorted(data.keys()),
            "state": state,
            "message": f"共 {len(data)} 项数据，更新于 {state.get('updated_at') or '从未'}",
        })

    @plugin_entry(
        id="put_state",
        name="写入存档数据",
        description="把游戏数据写回插件存储，按 key 合并。",
        input_schema={
            "type": "object",
            "properties": {
                "data": {"type": "object", "description": "键值对，值通常是 JSON 字符串"},
            },
            "required": ["data"],
        },
        llm_result_fields=["ok", "keys", "message"],
    )
    async def put_state(self, data: Optional[Dict[str, Any]] = None, **_):
        if not isinstance(data, dict):
            return Err(SdkError("data 必须是对象"))
        try:
            payload = self._merge_state(data)
            self._write_state(payload)
        except Exception as exc:
            return Err(SdkError(f"写入失败: {exc}"))
        return Ok({
            "ok": True,
            "keys": len(payload.get("data") or {}),
            "updated_at": payload.get("updated_at", ""),
            "message": f"已保存 {len(data)} 项",
        })

    @plugin_entry(
        id="get_profile",
        name="查看玩家档案",
        description="汇总玩家进度：通关次数、击杀、最近与最佳一局、职业等级。",
        llm_result_fields=["ok", "profile", "message"],
    )
    async def get_profile(self, **_):
        profile = self._profile()
        last = profile.get("last_run") or {}
        message = (
            f"通关 {profile['clears']} 次，累计击杀 {profile['kills']}，"
            f"记录 {profile['runs_recorded']} 局"
        )
        if last:
            message += f"，最近一局 {last.get('difficulty', '?')} 存活 {last.get('seconds', 0)} 秒"
        return Ok({"ok": True, "profile": profile, "message": message})

    @plugin_entry(
        id="report_run",
        name="记录一局战报",
        description="记录一局的难度、时长、击杀、职业与结果。",
        input_schema={
            "type": "object",
            "properties": {
                "difficulty": {"type": "string", "description": "难度，如 D3"},
                "seconds": {"type": "number", "description": "存活秒数"},
                "kills": {"type": "number", "description": "击杀数"},
                "branch": {"type": "string", "description": "职业，如 blade"},
                "won": {"type": "boolean", "description": "是否通关"},
                "detail": {"type": "object", "description": "其他附加信息"},
            },
            "required": ["seconds"],
        },
        llm_result_fields=["ok", "run", "message"],
    )
    async def report_run(
        self,
        seconds: float = 0,
        difficulty: str = "",
        kills: int = 0,
        branch: str = "",
        won: bool = False,
        detail: Optional[Dict[str, Any]] = None,
        **_,
    ):
        state = self._read_state()
        runs = state.get("runs")
        if not isinstance(runs, list):
            runs = []
        run = {
            "at": _now(),
            "difficulty": difficulty or "D1",
            "seconds": int(seconds or 0),
            "kills": int(kills or 0),
            "branch": branch or "",
            "won": bool(won),
        }
        if isinstance(detail, dict):
            run["detail"] = detail
        runs.append(run)
        state["runs"] = runs[-200:]
        try:
            self._write_state(state)
        except Exception as exc:
            return Err(SdkError(f"记录失败: {exc}"))
        return Ok({"ok": True, "run": run, "message": f"已记录：{run['difficulty']} {run['seconds']} 秒"})

    @plugin_entry(
        id="reset_state",
        name="重置全部数据",
        description="清空插件里保存的所有《无尽测试》数据。",
        input_schema={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "必须为 true 才会执行"},
            },
            "required": ["confirm"],
        },
        llm_result_fields=["ok", "message"],
    )
    async def reset_state(self, confirm: bool = False, **_):
        if not confirm:
            return Err(SdkError("需要 confirm=true 才会清空数据"))
        try:
            self._write_state({"v": STATE_VERSION, "updated_at": _now(), "data": {}, "runs": []})
        except Exception as exc:
            return Err(SdkError(f"重置失败: {exc}"))
        return Ok({"ok": True, "message": "已清空全部数据"})

    @plugin_entry(
        id="status",
        name="查看服务状态",
        description="查看游戏服务端口、静态页面与数据文件状态。",
        llm_result_fields=["ok", "url", "serving", "has_game_file", "state_file", "message"],
    )
    async def status(self, **_):
        game = self._game_file()
        state = self._resolve_state_path()
        serving = bool(self._http) and bool(self._thread and self._thread.is_alive())
        return Ok({
            "ok": True,
            "url": self._url() if serving else "",
            "serving": serving,
            "has_game_file": game.is_file(),
            "state_file": str(state),
            "state_size": state.stat().st_size if state.is_file() else 0,
            "message": (
                f"服务{'运行中' if serving else '未启动'} · "
                f"页面{'已就绪' if game.is_file() else '缺失'} · "
                f"数据{'已存在' if state.is_file() else '尚未创建'}"
            ),
        })
