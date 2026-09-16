"""工具调用记录折叠走查（E2E，Playwright，工单 0023）：

验证同一轮的工具事件在对话主流里默认折叠为一行轮次摘要、点击展开看完整明细，
且摘要反映本轮真实的读取/修改文件数（规格 0021 / ADR 0005「前端渲染」：工具事件
落库契约不变，变化只在渲染层；前端不做组件级测试，靠端到端走查脚本覆盖折叠与展开）。

覆盖判据：
- 一轮内多条工具事件折叠为「一行摘要」：对话主流里出现的是摘要组，不是平铺的工具行；
- 摘要口径正确：读取/修改文件数按去重路径计，终结出口 submit_turn_result 不计入摘要
  （其结果已由产物卡片呈现，事件行仅作证据链留档于展开明细）；
- 默认折叠、点击展开：展开前明细不可见，展开后逐条工具行可见且条数与落库事件一致；
- 跨轮不串组：两轮各自的工具事件折叠成两个独立摘要组（被 thinking/卡片/用户消息隔开）；
- 刷新一致性：整页刷新后历史回看与刷新前一致——折叠摘要（同口径）+ 可再展开明细；
- 思考过程默认折叠现状不受影响（工单 0023 验收项）。

自带 mock 后端（8001 端口，经 Playwright route.continue_ 重写 /api 请求直连）：
持久化历史直出工具事件行（kind='event'），不依赖真实后端与 LLM，确定性可重放。
运行：python frontend/e2e/tool_event_folding.py
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


def _event(name: str, args: dict, status: str = "done", result: str = "") -> str:
    """构造一条工具事件行的落库 JSON（字段契约同后端 _persist_event）。"""
    return json.dumps(
        {"name": name, "args": args, "status": status, "result": result},
        ensure_ascii=False,
    )


def _card(summary: str, changed: list[str]) -> str:
    """构造一张轮次产物卡片的落库 JSON（字段契约同后端 0024/0025/0028 卡片）。"""
    return json.dumps(
        {
            "intent": "modify_code" if changed else "no_change",
            "summary": summary,
            "changed_files": changed,
            "declared_files": changed,
            "no_change_reason": "",
            "consistency": "consistent",
            "mismatch_kind": None,
            "scope_verdict": "in_scope",
            "out_of_scope_segments": [],
            "snapshot_id": 2,
            "snapshot_rev": 2,
        },
        ensure_ascii=False,
    )


# 两轮历史，消息按落库 id 顺序排列：
#   轮一（改代码）：user → read×2 → edit → submit_turn_result → thinking → turn_result
#     工具事件在循环中即时落库、thinking 与卡片在轮末落库，故工具事件天然连续排在前面；
#     摘要应为「本轮读取 2 个文件、修改 1 个文件」（index.html 读+改算读 1 改 1，
#     style.css 只读；submit_turn_result 不计入），明细含 4 条工具行（含 submit 留档）。
#   轮二（咨询/只读）：user → read → search_templates → thinking → text
#     摘要应为「本轮读取 1 个文件、检索 1 次」，明细含 2 条工具行；咨询轮无产物卡片。
MESSAGES = [
    {"id": 1, "role": "user", "kind": "text", "content": "把标题改大一点"},
    {"id": 2, "role": "engineer", "kind": "event",
     "content": _event("read_file", {"path": "index.html"}, "done", "<h1>…</h1>")},
    {"id": 3, "role": "engineer", "kind": "event",
     "content": _event("read_file", {"path": "style.css"}, "done", "h1 { … }")},
    {"id": 4, "role": "engineer", "kind": "event",
     "content": _event("edit_file", {"path": "index.html"}, "done", "ok")},
    {"id": 5, "role": "engineer", "kind": "event",
     "content": _event("submit_turn_result", {"intent": "modify_code"}, "done", "ok")},
    {"id": 6, "role": "engineer", "kind": "thinking", "content": "先读结构再改字号。"},
    {"id": 7, "role": "engineer", "kind": "turn_result",
     "content": _card("已把标题字号调大。", ["index.html"])},
    {"id": 8, "role": "user", "kind": "text", "content": "按钮样式在哪个文件定义"},
    {"id": 9, "role": "engineer", "kind": "event",
     "content": _event("read_file", {"path": "App.vue"}, "done", "<style>…</style>")},
    {"id": 10, "role": "engineer", "kind": "event",
     "content": _event("search_templates", {"query": "button"}, "done", "命中 3 个")},
    {"id": 11, "role": "engineer", "kind": "thinking", "content": "检索按钮相关模板。"},
    {"id": 12, "role": "engineer", "kind": "text", "content": "按钮样式在 App.vue 的 <style> 段。"},
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

            groups = page.locator(".tool-group")

            # —— 跨轮不串组：两轮各自折叠成一个独立摘要组 ——
            if groups.count() != 2:
                failures.append(f"期望 2 个工具折叠组（每轮一个），实际 {groups.count()}")

            # —— 默认折叠：对话主流里没有平铺的工具行，明细不可见 ——
            # 顶层可见的 .tool-line 数为 0（全部藏在折叠明细里）
            visible_lines = [
                ln for ln in page.locator(".tool-line").all() if ln.is_visible()
            ]
            if visible_lines:
                failures.append(f"默认应折叠，却有 {len(visible_lines)} 条工具行平铺可见")

            g1 = groups.nth(0)
            g2 = groups.nth(1)

            # —— 轮一摘要口径：读取 2 个文件、修改 1 个文件（submit 不计入）——
            s1 = g1.locator('[data-testid="tool-group-summary"]').inner_text().strip()
            if s1 != "本轮读取 2 个文件、修改 1 个文件":
                failures.append(f"轮一摘要口径错误：「{s1}」")

            # —— 轮二摘要口径：读取 1 个文件、检索 1 次（咨询轮无产物卡片）——
            s2 = g2.locator('[data-testid="tool-group-summary"]').inner_text().strip()
            if s2 != "本轮读取 1 个文件、检索 1 次":
                failures.append(f"轮二摘要口径错误：「{s2}」")

            # —— 默认折叠：明细容器不可见 ——
            d1 = g1.locator('[data-testid="tool-group-detail"]')
            if d1.is_visible():
                failures.append("轮一明细默认不应可见（应折叠）")

            # —— 点击展开：明细可见，逐条工具行齐全（含 submit_turn_result 留档）——
            g1.locator('[data-testid="tool-group-toggle"]').click()
            page.wait_for_timeout(150)
            if not d1.is_visible():
                failures.append("点击摘要后轮一明细仍未展开")
            else:
                lines1 = g1.locator(".tool-line")
                if lines1.count() != 4:
                    failures.append(f"轮一展开后应有 4 条工具行（read×2+edit+submit），实际 {lines1.count()}")
                detail_text = d1.inner_text()
                for expected in ("读取 index.html", "读取 style.css", "修改 index.html", "提交轮次产物"):
                    if expected not in detail_text:
                        failures.append(f"轮一明细缺内容：{expected}")
                # 展开后轮一的工具行可见
                if not g1.locator(".tool-line").first.is_visible():
                    failures.append("轮一展开后工具行仍不可见")

            # —— 展开轮一不影响轮二：轮二仍折叠 ——
            d2 = g2.locator('[data-testid="tool-group-detail"]')
            if d2.is_visible():
                failures.append("展开轮一不应连带展开轮二")

            # —— 再次点击收起 ——
            g1.locator('[data-testid="tool-group-toggle"]').click()
            page.wait_for_timeout(150)
            if d1.is_visible():
                failures.append("再次点击摘要后轮一明细应收起")

            # —— 思考过程默认折叠现状不受影响（工单 0023 验收项）——
            thinking_bodies = page.locator(".thinking-body")
            if thinking_bodies.count() != 2:
                failures.append(f"期望 2 个思考块，实际 {thinking_bodies.count()}")
            else:
                if thinking_bodies.nth(0).is_visible() or thinking_bodies.nth(1).is_visible():
                    failures.append("思考过程应默认折叠（正文不可见）")

            # —— 刷新一致性（工单 0023 验收项）：整页刷新后历史回看与刷新前一致 ——
            # 仍是折叠摘要（同口径）+ 明细默认收起、可再展开
            page.reload()
            page.wait_for_timeout(800)
            if groups.count() != 2:
                failures.append(f"刷新后应有 2 个工具折叠组，实际 {groups.count()}")
            r1 = g1.locator('[data-testid="tool-group-summary"]').inner_text().strip()
            r2 = g2.locator('[data-testid="tool-group-summary"]').inner_text().strip()
            if r1 != s1 or r2 != s2:
                failures.append(f"刷新后摘要口径与刷新前不一致：「{r1}」/「{r2}」")
            if d1.is_visible():
                failures.append("刷新后明细应回到默认折叠")
            g1.locator('[data-testid="tool-group-toggle"]').click()
            page.wait_for_timeout(150)
            if not d1.is_visible():
                failures.append("刷新后点击摘要仍应能展开明细")
            elif g1.locator(".tool-line").count() != 4:
                failures.append(
                    f"刷新后展开明细应有 4 条工具行，实际 {g1.locator('.tool-line').count()}"
                )
            g1.locator('[data-testid="tool-group-toggle"]').click()  # 收起，供下方截图流程复用
            page.wait_for_timeout(150)

            page.screenshot(path=str(SHOTS_DIR / "tool_folding_full.png"), full_page=True)
            # 展开态截图，留档明细可见性
            g1.locator('[data-testid="tool-group-toggle"]').click()
            page.wait_for_timeout(150)
            page.screenshot(path=str(SHOTS_DIR / "tool_folding_expanded.png"), full_page=True)
            browser.close()
    finally:
        mock.shutdown()
        if dev_proc is not None:
            subprocess.run(
                ["taskkill", "/pid", str(dev_proc.pid), "/t", "/f"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    if failures:
        print("FAIL —— 工具调用记录折叠走查未过：")
        for f in failures:
            print(" -", f)
        print(f"截图：{SHOTS_DIR}")
        return 1
    print("PASS —— 工具事件按轮折叠为一行摘要、口径正确、默认折叠可展开、跨轮不串组、思考折叠不受影响")
    return 0


if __name__ == "__main__":
    sys.exit(main())
