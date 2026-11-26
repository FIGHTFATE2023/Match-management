"""比赛管理系统 · 公网入口（参考 Where to charge 的单后端 + 隧道模式）。

整套系统只跑一个 Flask 后端进程，公网流量经 Cloudflare / ngrok 隧道转发回
本机 127.0.0.1，本机、局域网和公网访问的是同一个进程、同一份 .local/match.db，
因此名单、队长改动和变更记录天然完全一致，不需要任何数据同步。

用法（从项目根目录运行）：
    python backend/public_tunnel.py                     # 默认 Cloudflare 临时隧道，零配置
    python backend/public_tunnel.py --tunnel ngrok     # ngrok 固定域名，重启后地址不变
    python backend/public_tunnel.py --port 8768        # 换端口

按 Ctrl+C 会同时停止本脚本启动的隧道与本地服务；若复用的是已经运行的
本地服务，则只停止隧道，不干扰原有窗口。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

# backend/ 的上一级即项目根目录；所有运行数据、工具都放在根目录的 .local/ 下
ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / ".local"
DEFAULT_PORT = 8768
URL_FILE = LOCAL / "public-url.txt"
TUNNEL_SETTINGS = LOCAL / "tunnel.json"

CF_URL = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com\b")
NGROK_ERROR = re.compile(r"\bERR_NGROK_\d+\b")
NGROK_HEADERS = {"ngrok-skip-browser-warning": "true", "User-Agent": "Mozilla/5.0"}


def http_alive(port: int) -> bool:
    """端口上是否已经跑着本项目的后端。

    必须显式绕过系统代理：不少电脑开着代理软件时会设置 HTTP_PROXY，
    且 NO_PROXY 未必包含 127.0.0.1。若跟着代理走，本地探测会被代理拦截，
    误判成「服务没启动」，进而重复启动后端并撞上端口占用，公网入口起不来。
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://127.0.0.1:{port}/login")
        with opener.open(req, timeout=3) as response:
            body = response.read(6000).decode("utf-8", "replace")
        return response.status == 200 and (
            "请选择你的身份" in body or "比赛管理系统" in body
        )
    except Exception:  # noqa: BLE001 - 端口未监听 / 非本项目 / 超时均视为不可用
        return False


def port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def ensure_app(port: int):
    """保证只有一个后端：已运行则复用，否则启动一个；返回本脚本启动的进程。"""
    if http_alive(port):
        print(f"已复用正在运行的本地服务 127.0.0.1:{port}（不启动第二份后端）", flush=True)
        return None
    if port_in_use(port):
        # 端口上可能正是本项目的服务，只是启动慢/首次请求超时，再探测一次避免误判。
        time.sleep(1)
        if http_alive(port):
            print(f"已复用正在运行的本地服务 127.0.0.1:{port}（不启动第二份后端）", flush=True)
            return None
        raise RuntimeError(
            f"端口 {port} 已被其他程序占用，请关闭它或用 --port 指定其他端口"
        )
    print("正在启动本地服务 ...", flush=True)
    env = dict(os.environ, PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "backend" / "app.py")], cwd=str(ROOT), env=env
    )
    for _ in range(80):
        if http_alive(port):
            print(f"本地服务已就绪 127.0.0.1:{port}", flush=True)
            return proc
        if proc.poll() is not None:
            raise RuntimeError("本地服务启动失败，请单独运行 backend/app.py 查看报错")
        time.sleep(0.25)
    raise RuntimeError("本地服务启动超时")


def stop(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def cloudflared_exe(explicit: str = "") -> str:
    # 只查项目内的 .local/tools 与系统 PATH，不写死任何机器的磁盘路径
    candidates = [
        explicit,
        LOCAL / "tools" / "cloudflared.exe",
        shutil.which("cloudflared"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise RuntimeError("未找到 cloudflared.exe，请放入 .local/tools/ 或用 --cloudflared 指定")


def ngrok_exe(explicit: str = "") -> str:
    candidates = [explicit, LOCAL / "tools" / "ngrok.exe", shutil.which("ngrok")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise RuntimeError("未找到 ngrok，请安装官方客户端或用 --ngrok 指定路径")


def ngrok_config() -> Path:
    path = LOCAL / "ngrok.yml"
    if not path.is_file():
        raise RuntimeError("缺少 .local/ngrok.yml（ngrok Authtoken 配置）")
    return path


def saved_ngrok_url() -> str:
    try:
        data = json.loads(TUNNEL_SETTINGS.read_text(encoding="utf-8"))
        value = data.get("ngrok_url", "").strip()
        if value.startswith("https://"):
            return value.rstrip("/")
    except (OSError, ValueError, AttributeError):
        pass
    return ""


class Tunnel:
    """统一的隧道子进程封装：解析日志拿公网地址，退出时负责清理。"""

    def __init__(self, command, parser):
        self.command = command
        self.parser = parser
        self.proc = None
        self.reader = None
        self.url = ""
        self.connected = False
        self.error = ""
        self.log_lines = []
        self.ready = threading.Event()

    def start(self, timeout: int = 60) -> str:
        self.proc = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        def read_output():
            for line in self.proc.stdout:
                self.log_lines.append(line.rstrip())
                if len(self.log_lines) > 60:
                    self.log_lines.pop(0)
                self.parser(line, self)
                if self.error or (self.url and self.connected):
                    self.ready.set()

        self.reader = threading.Thread(target=read_output, name="tunnel-log", daemon=True)
        self.reader.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.error:
                raise RuntimeError(f"隧道连接失败：{self.error}")
            if self.proc.poll() is not None:
                raise RuntimeError("隧道进程已退出，请检查网络与客户端配置")
            if self.ready.wait(0.2) and self.url and self.connected:
                return self.url
        raise RuntimeError("等待隧道连接超时，请确认电脑可访问 Cloudflare / ngrok")

    def close(self) -> None:
        if self.proc is not None:
            stop(self.proc)
        if self.reader is not None:
            self.reader.join(timeout=1)
        if self.proc is not None and self.proc.stdout:
            try:
                self.proc.stdout.close()
            except OSError:
                pass


def parse_cloudflared(line: str, tunnel: Tunnel) -> None:
    if not tunnel.url:
        match = CF_URL.search(line)
        if match:
            tunnel.url = match.group(0)
    low = line.lower()
    # 必须等边缘节点真正注册连接才算可用；仅拿到 URL 时域名可能尚未生效
    if "registered tunnel connection" in low or "connection registered" in low:
        tunnel.connected = True


def parse_ngrok(line: str, tunnel: Tunnel) -> None:
    match = NGROK_ERROR.search(line)
    if match:
        tunnel.error = match.group(0)
        return
    try:
        entry = json.loads(line)
    except ValueError:
        return
    if not isinstance(entry, dict):
        return
    if entry.get("msg") not in ("started tunnel", "started endpoint"):
        return
    url = entry.get("url")
    if isinstance(url, str) and url.startswith("https://"):
        tunnel.url = url.rstrip("/")
        tunnel.connected = True


def build_tunnel(args, use_fixed_url: bool = True) -> Tunnel:
    origin = f"http://127.0.0.1:{args.port}"
    if args.tunnel == "ngrok":
        command = [
            ngrok_exe(args.ngrok), "http", origin,
            "--config", str(ngrok_config()),
            "--log=stdout", "--log-format=json", "--inspect=false",
        ]
        # 固定域名被其他程序占用时，回退到 ngrok 自动分配的临时域名
        url = (args.url or saved_ngrok_url()).strip() if use_fixed_url else ""
        if url:
            command += ["--url", url]
        return Tunnel(command, parse_ngrok)
    command = [
        cloudflared_exe(args.cloudflared), "tunnel", "--no-autoupdate",
        "--loglevel", "info", "--protocol", "http2", "--url", origin,
    ]
    return Tunnel(command, parse_cloudflared)


def probe_public(url: str) -> bool:
    """确认公网地址真的能打开登录页，且不受本机 DNS / 代理环境干扰。

    部分内网 DNS 对 *.trycloudflare.com 子域直接返回 NXDOMAIN，工具进程还可能
    带 HTTP_PROXY（内部代理返回 502）。因此这里用主域解析出的 Cloudflare 边缘
    IP 建立直连 TLS，再通过 SNI 与 Host 头指向隧道域名，等价于外部访客的请求。
    """
    host = urlsplit(url).hostname or ""
    if not host:
        return False
    edge_ip = host
    if host.endswith(".trycloudflare.com"):
        try:
            edge_ip = socket.gethostbyname("trycloudflare.com")
        except OSError:
            return False
    request = (
        "GET /login HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "User-Agent: Mozilla/5.0\r\n"
        "ngrok-skip-browser-warning: true\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    for _ in range(8):
        try:
            context = ssl.create_default_context()
            raw = socket.create_connection((edge_ip, 443), timeout=4)
            with context.wrap_socket(raw, server_hostname=host) as sock:
                sock.sendall(request)
                chunks = []
                while sum(len(c) for c in chunks) < 12000:
                    part = sock.recv(4096)
                    if not part:
                        break
                    chunks.append(part)
            body = b"".join(chunks).decode("utf-8", "replace")
            if body.startswith("HTTP/1.1 200") and "请选择你的身份" in body:
                return True
        except Exception:  # noqa: BLE001 - 边缘尚未生效，继续重试
            pass
        time.sleep(2)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="比赛管理系统公网入口（唯一后端 + 隧道）")
    ap.add_argument("--tunnel", choices=["cloudflare", "ngrok"], default="cloudflare")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--url", default="", help="ngrok 固定域名，如 https://xxx.ngrok-free.dev")
    ap.add_argument("--cloudflared", default="", help="cloudflared.exe 路径")
    ap.add_argument("--ngrok", default="", help="ngrok.exe 路径")
    args = ap.parse_args()

    LOCAL.mkdir(parents=True, exist_ok=True)
    app_proc = None
    tunnel = None
    try:
        app_proc = ensure_app(args.port)
        kind = "ngrok 入口" if args.tunnel == "ngrok" else "Cloudflare 临时入口"
        address = ""
        fallback = ""
        verified = False
        last_error = "未知原因"
        for attempt in range(1, 4):
            if tunnel is not None:
                tunnel.close()
                tunnel = None
            print(f"正在连接{kind}（第 {attempt}/3 次）...", flush=True)
            tunnel = build_tunnel(args, use_fixed_url=(attempt == 1))
            try:
                candidate = tunnel.start()
                fallback = candidate
                print(f"  候选地址：{candidate}", flush=True)
            except RuntimeError as exc:
                last_error = str(exc)
                print(f"  连接失败：{last_error}", flush=True)
                if args.tunnel == "ngrok" and "9009" in last_error:
                    print("  固定域名已被占用，将改用 ngrok 临时域名重试。", flush=True)
                continue
            if probe_public(candidate):
                address = candidate
                verified = True
                break
            last_error = "公网地址不可达"
            print("  公网连通性检查未通过，正在重建隧道 ...", flush=True)
            if tunnel.log_lines:
                print("  隧道日志末尾：" + " | ".join(tunnel.log_lines[-3:])[:280], flush=True)
        if not address and fallback:
            # 隧道已注册但本机自检不通：常见于本机 DNS 缓存 / 回环访问限制，
            # 手机流量等外部网络通常仍可打开，保留地址并明确提示用户确认。
            address = fallback
            print("  提示：本机自检未打开该地址，但隧道已注册；请用手机流量或另一网络打开确认。", flush=True)
            print("        若外部网络也打不开，请重启本入口重新生成地址。", flush=True)
        elif not address:
            raise RuntimeError(
                f"连续 3 次未能建立可用隧道（{last_error}）；"
                "请检查网络，或改用 --tunnel ngrok / 另一个网络重试"
            )
        URL_FILE.write_text(address, encoding="utf-8")
        print("", flush=True)
        print("=" * 62, flush=True)
        print(f"  公网地址：{address}", flush=True)
        print(f"  本机地址：http://127.0.0.1:{args.port}", flush=True)
        print("=" * 62, flush=True)
        print("  本机、局域网、公网访问的是同一个后端、同一份数据库。", flush=True)
        print(
            "  公网连通性检查：" + ("通过（/login 已可访问）" if verified else "未通过（请用外部网络确认）"),
            flush=True,
        )
        print("  请保持这台电脑开机联网、保持本窗口运行。", flush=True)
        if args.tunnel == "cloudflare":
            print("  临时入口：每次重启网址会变化，请把上面的新地址发给使用者。", flush=True)
        else:
            print("  ngrok 免费版首次访问可能需点一次 Visit Site。", flush=True)
        print("  按 Ctrl+C 停止公网入口。", flush=True)
        print("", flush=True)

        while True:
            if tunnel.proc.poll() is not None:
                print("隧道已断开，公网入口停止。", flush=True)
                break
            if app_proc is not None and app_proc.poll() is not None:
                print("本地服务已退出，公网入口停止。", flush=True)
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止公网入口 ...", flush=True)
    except Exception as exc:  # noqa: BLE001 - 启动阶段的错误统一给出可读提示
        print(f"\n启动失败：{exc}", flush=True)
        return 1
    finally:
        if tunnel is not None:
            tunnel.close()
        stop(app_proc)
        try:
            URL_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
