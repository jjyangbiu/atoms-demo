"""生成中停止与输入区布局回归（E2E，Playwright）：

诊断回路（diagnosing-bugs）：
- 问题 1：生成中不支持停止——期望生成中出现「停止」按钮，点击后中止 SSE、UI 回到可发送态；
- 问题 2：生成中按钮和对话框的布局不协调——抓取两态截图留证，供人工/视觉核对。

自带 mock 后端（8001 端口，8000 已被真实后端占用不可抢占；经 Playwright route.continue_
重写 /api 请求直连 8001，mock 回 CORS 头）：快接口直出，
messages POST 以真实逐帧慢速 SSE 下发（每帧 0.5s），客户端中止即被服务端感知。
不依赖真实后端与 LLM，确定性可重放。
运行：python frontend/e2e/stop_generation.py
（5173 端口无 dev server 时自动拉起 vite，结束自动清理。）
"""

import json
import re
import socket
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
FRONTEND_DIR = Path(__file__).resolve().parents[1]
SHOTS_DIR = FRONTEND_DIR / "e2e" / "shots"

SLOW_TEXT_FRAMES = [{"type": "text", "content": f"生成片段{ch}。"} for ch in "一二三四五六七八"]

# 服务端感知的断流信号（问题 1 的服务端侧断言）
server_state = {"frames_sent": 0, "client_disconnected": False}
state_lock = threading.Lock()


class MockBackend(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/api/auth/me"):
            return self._json({"id": 1, "username": "tester", "created_at": "2026-01-01T00:00:00"})
        if re.search(r"/api/projects/1$", self.path):
            return self._json({"name": "demo", "mode": "engineer", "published_slug": None})
        # 项目已有文件：绕过分流，消息直接触发工程师生成（后端以有文件判分流）
        if self.path.endswith("/api/projects/1/files"):
            return self._json([{"path": "index.html", "size": 12}])
        if self.path.endswith("/api/projects/1/snapshots") or self.path.endswith("/api/projects/1/tickets"):
            return self._json([])
        if self.path.endswith("/api/projects/1/messages"):
            return self._json([])
        return self._json({"detail": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.endswith("/api/projects/1/messages"):
            return self._json({"detail": "not found"}, 404)
        # 慢速 SSE：逐帧 flush + sleep；客户端中止时写会抛异常
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self._cors()
        self.end_headers()
        try:
            for frame in SLOW_TEXT_FRAMES:
                self.wfile.write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                with state_lock:
                    server_state["frames_sent"] += 1
                time.sleep(0.5)
            self.wfile.write(b"data: {\"type\": \"done\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            with state_lock:
                server_state["client_disconnected"] = True


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except OSError:
        return False


def main() -> int:
    failures: list[str] = []
    dev_proc = None

    if not port_open(5173):
        dev_proc = subprocess.Popen(
            ["npm.cmd", "run", "dev"], cwd=FRONTEND_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if port_open(5173):
                break
            time.sleep(0.5)
        else:
            print("无法拉起 vite dev server（5173）", file=sys.stderr)
            return 2

    mock = ThreadingHTTPServer(("127.0.0.1", 8001), MockBackend)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    SHOTS_DIR.mkdir(exist_ok=True)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            ctx.add_init_script("localStorage.setItem('atoms_token', 'fake-token')")
            # /api 请求改道 8001 mock 后端（保留流式，不破坏 SSE 逐帧消费）；
            # 按路径前缀匹配，避免误吞 /src/api/ 等 vite 模块 URL
            ctx.route(
                lambda url: "://localhost:5173/api/" in url,
                lambda route: route.continue_(
                    url=route.request.url.replace("localhost:5173", "127.0.0.1:8001")
                ),
            )
            page = ctx.new_page()
            page.goto(f"{BASE}/projects/1")
            page.wait_for_timeout(600)

            # —— 两态截图留证（问题 2：布局协调性）——
            page.locator(".chat-bottom").screenshot(path=str(SHOTS_DIR / "composer_idle.png"))
            page.locator('[data-testid="chat-input"]').fill("慢速生成一个页面")
            page.locator('[data-testid="chat-send"]').click()

            # —— 问题 1：生成中应有「停止」入口，点击后中止生成 ——
            stop_btn = page.locator('[data-testid="chat-stop"]')
            try:
                stop_btn.wait_for(timeout=5000)
            except Exception:
                failures.append("生成中没有「停止」按钮（问题 1 复现）")
            else:
                page.locator(".chat-bottom").screenshot(
                    path=str(SHOTS_DIR / "composer_generating.png")
                )
                stop_btn.click()
                page.wait_for_timeout(1500)
                # 停止后回到可发送态：停止按钮消失、发送按钮重现且非 loading
                if stop_btn.count() != 0:
                    failures.append("点击「停止」后停止按钮仍在，未退出生成中")
                send_btn = page.locator('[data-testid="chat-send"]')
                if send_btn.count() == 0:
                    failures.append("点击「停止」后发送按钮未恢复")
                elif send_btn.evaluate("el => el.classList.contains('is-loading')"):
                    failures.append("点击「停止」后发送按钮仍处于 loading")
                with state_lock:
                    sent = server_state["frames_sent"]
                    disconnected = server_state["client_disconnected"]
                if sent >= len(SLOW_TEXT_FRAMES):
                    failures.append("点击「停止」时全部帧已发完，窗口太短无法验证中止")
                if not disconnected:
                    failures.append("点击「停止」后服务端未感知到客户端断开")

            browser.close()
    finally:
        mock.shutdown()
        if dev_proc is not None:
            subprocess.run(
                ["taskkill", "/pid", str(dev_proc.pid), "/t", "/f"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    if failures:
        print("FAIL —— 生成中停止/布局回路未过：")
        for f in failures:
            print(" -", f)
        print(f"截图：{SHOTS_DIR}")
        return 1
    print("PASS —— 生成中可停止，停止后回到可发送态，服务端感知断流")
    return 0


if __name__ == "__main__":
    sys.exit(main())
