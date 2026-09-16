"""正确性裁决卡片走查（E2E，Playwright，工单 0028 阶段一）：

验证轮次产物卡片对四种裁决态 + 存量旧卡片的渲染（规格 0021：越界段落标红靠
端到端走查脚本验证，前端不做组件级测试）：
- 越界（out_of_scope）：徽标「越界改动」+ 越界段落清单标红（文件、行区间、理由）
  + 接受/回滚提示（阶段一只标注不阻断，快照照常留档）；
- 未完成（incomplete）：徽标「未完成」，无段落清单；
- 核验未完成（unverified，裁判失败降级）：徽标「核验未完成」；
- 范围内（in_scope）：干净收尾——不出现任何裁决徽标与段落清单；
- 存量旧卡片（无 scope_verdict 字段）：正常渲染、不加徽标（向后兼容）。

自带 mock 后端（8001 端口，经 Playwright route.continue_ 重写 /api 请求直连）：
持久化历史直出 turn_result 卡片消息，不依赖真实后端与 LLM，确定性可重放。
运行：python frontend/e2e/verdict_card.py
（5173 端口无 dev server 时自动拉起 vite，结束自动清理。）
"""

import json
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
FRONTEND_DIR = Path(__file__).resolve().parents[1]
SHOTS_DIR = FRONTEND_DIR / "e2e" / "shots"

RED = "rgb(245, 108, 108)"  # Element Plus danger 色：越界清单「标红」的判据


def _card(summary: str, scope_verdict=None, segments=None, legacy: bool = False) -> str:
    """构造一张轮次产物卡片的落库 JSON（字段契约同后端 0028 卡片）。"""
    data = {
        "intent": "modify_code",
        "summary": summary,
        "changed_files": ["index.html"],
        "declared_files": ["index.html"],
        "no_change_reason": "",
        "consistency": "consistent",
        "mismatch_kind": None,
        "snapshot_id": 5,
        "snapshot_rev": 5,
    }
    if not legacy:
        data["scope_verdict"] = scope_verdict
        data["out_of_scope_segments"] = segments or []
    return json.dumps(data, ensure_ascii=False)


MESSAGES = [
    {"id": 1, "role": "user", "kind": "text", "content": "把标题改一下"},
    {
        "id": 2,
        "role": "engineer",
        "kind": "turn_result",
        "content": _card(
            "越界轮卡片。",
            scope_verdict="out_of_scope",
            segments=[
                {
                    "file": "index.html",
                    "start_line": 3,
                    "end_line": 8,
                    "reason": "顺带重写了页脚，用户未要求。",
                }
            ],
        ),
    },
    {
        "id": 3,
        "role": "engineer",
        "kind": "turn_result",
        "content": _card("未完成轮卡片。", scope_verdict="incomplete"),
    },
    {
        "id": 4,
        "role": "engineer",
        "kind": "turn_result",
        "content": _card("裁判失败轮卡片。", scope_verdict="unverified"),
    },
    {
        "id": 5,
        "role": "engineer",
        "kind": "turn_result",
        "content": _card("范围内轮卡片。", scope_verdict="in_scope"),
    },
    {
        "id": 6,
        "role": "engineer",
        "kind": "turn_result",
        "content": _card("存量旧卡片。", legacy=True),
    },
]


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
        if self.path.endswith("/api/projects/1/files"):
            return self._json([{"path": "index.html", "size": 12}])
        if self.path.endswith("/api/projects/1/snapshots") or self.path.endswith(
            "/api/projects/1/tickets"
        ):
            return self._json([])
        if self.path.endswith("/api/projects/1/messages"):
            return self._json(MESSAGES)
        return self._json({"detail": "not found"}, 404)


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
            ctx = browser.new_context(viewport={"width": 1280, "height": 1200})
            ctx.add_init_script("localStorage.setItem('atoms_token', 'fake-token')")
            ctx.route(
                lambda url: "://localhost:5173/api/" in url,
                lambda route: route.continue_(
                    url=route.request.url.replace("localhost:5173", "127.0.0.1:8001")
                ),
            )
            page = ctx.new_page()
            page.goto(f"{BASE}/projects/1")
            page.wait_for_timeout(800)

            def card_of(summary: str):
                return page.locator(".turn-result-card", has_text=summary)

            # —— 越界轮：徽标 + 标红段落清单 + 接受/回滚提示 ——
            oos = card_of("越界轮卡片。")
            if oos.locator('[data-testid="scope-badge-out-of-scope"]').count() != 1:
                failures.append("越界轮缺少「越界改动」徽标")
            segs = oos.locator('[data-testid="out-of-scope-segments"]')
            if segs.count() != 1:
                failures.append("越界轮缺少越界段落清单")
            else:
                text = segs.inner_text()
                for expected in ("index.html", "第 3–8 行", "顺带重写了页脚，用户未要求。"):
                    if expected not in text:
                        failures.append(f"越界段落清单缺内容：{expected}")
                hint = segs.locator(".turn-result-oos-hint").inner_text()
                if "接受" not in hint or "回滚" not in hint:
                    failures.append("越界提示未告知可接受或回滚")
                # 「标红」的机器判据：清单标题的计算色是 danger 红
                color = segs.locator(".turn-result-oos-title").evaluate(
                    "el => getComputedStyle(el).color"
                )
                if color != RED:
                    failures.append(f"越界清单未标红（title 计算色 {color}，期望 {RED}）")
            oos.screenshot(path=str(SHOTS_DIR / "verdict_out_of_scope.png"))

            # —— 未完成轮：只标注，无段落清单 ——
            inc = card_of("未完成轮卡片。")
            if inc.locator('[data-testid="scope-badge-incomplete"]').count() != 1:
                failures.append("未完成轮缺少「未完成」徽标")
            if inc.locator('[data-testid="out-of-scope-segments"]').count() != 0:
                failures.append("未完成轮不应出现越界段落清单")

            # —— 裁判失败轮：「核验未完成」显式标注，不静默 ——
            unf = card_of("裁判失败轮卡片。")
            if unf.locator('[data-testid="scope-badge-unverified"]').count() != 1:
                failures.append("裁判失败轮缺少「核验未完成」徽标")
            if unf.locator('[data-testid="turn-result-diff-button"]').count() != 1:
                failures.append("裁判失败轮缺少快照 diff 入口（快照应照建）")

            # —— 范围内轮：干净收尾，无任何裁决徽标与清单 ——
            ins = card_of("范围内轮卡片。")
            if ins.locator('[data-testid^="scope-badge-"]').count() != 0:
                failures.append("范围内轮出现多余裁决徽标（应干净收尾）")
            if ins.locator('[data-testid="out-of-scope-segments"]').count() != 0:
                failures.append("范围内轮不应出现越界段落清单")

            # —— 存量旧卡片：无 scope_verdict 字段，正常渲染不加徽标 ——
            legacy = card_of("存量旧卡片。")
            if legacy.count() != 1:
                failures.append("存量旧卡片未渲染")
            elif legacy.locator('[data-testid^="scope-badge-"]').count() != 0:
                failures.append("存量旧卡片出现裁决徽标（向后兼容破坏）")

            page.screenshot(path=str(SHOTS_DIR / "verdict_cards_full.png"), full_page=True)
            browser.close()
    finally:
        mock.shutdown()
        if dev_proc is not None:
            subprocess.run(
                ["taskkill", "/pid", str(dev_proc.pid), "/t", "/f"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    if failures:
        print("FAIL —— 正确性裁决卡片走查未过：")
        for f in failures:
            print(" -", f)
        print(f"截图：{SHOTS_DIR}")
        return 1
    print("PASS —— 越界标红列段落、未完成/核验未完成标注、范围内干净收尾、旧卡片兼容")
    return 0


if __name__ == "__main__":
    sys.exit(main())
