"""澄清弹窗翻页回归（E2E，Playwright）：

- 非末题选中选项后自动切到下一题（1/3 → 2/3 → 3/3）；
- 末题选中后停留原页、不自动提交，「继续」按钮可用，用户主动点击才提交。

全量 mock /api，不依赖后端与 LLM，确定性可重放。
运行：pip install playwright && playwright install chromium，然后
  python frontend/e2e/clarify_pager.py
（5173 端口无 dev server 时自动拉起 vite，结束自动清理。）
"""

import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = "http://localhost:5173"
FRONTEND_DIR = Path(__file__).resolve().parents[1]

QUESTIONS = [
    {"question": f"问题{i + 1}？", "options": [f"选项{i}A", f"选项{i}B"], "recommend": 0}
    for i in range(3)
]
CLARIFY_JSON = json.dumps(QUESTIONS, ensure_ascii=False)

# 模拟服务端持久化状态：随交互推进
messages_state: list[dict] = []
post_bodies: list[dict] = []


def sse(*events: dict) -> str:
    return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)


def handle_api(route):
    req = route.request
    url = req.url
    method = req.method

    if url.endswith("/api/auth/me"):
        return route.fulfill(json={"id": 1, "username": "tester", "created_at": "2026-01-01T00:00:00"})
    if re.search(r"/api/projects/1$", url):
        return route.fulfill(json={"name": "demo", "mode": "engineer", "published_slug": None})
    if url.endswith("/api/projects/1/files") or url.endswith("/api/projects/1/snapshots"):
        return route.fulfill(json=[])
    if url.endswith("/api/projects/1/tickets"):
        return route.fulfill(json=[])
    if url.endswith("/api/projects/1/messages") and method == "GET":
        return route.fulfill(json=messages_state)
    if url.endswith("/api/projects/1/messages") and method == "POST":
        body = json.loads(req.post_data or "{}")
        post_bodies.append(body)
        if body.get("clarify_answer"):
            messages_state.append(
                {"id": 3, "role": "user", "kind": "clarify_answer",
                 "content": body.get("content", ""), "created_at": "2026-01-01T00:00:02"}
            )
            messages_state.append(
                {"id": 4, "role": "engineer", "kind": "text",
                 "content": "收到答案，开始生成。", "created_at": "2026-01-01T00:00:03"}
            )
            return route.fulfill(
                status=200, content_type="text/event-stream",
                body=sse({"type": "text", "content": "收到答案，开始生成。"}, {"type": "done"}),
            )
        messages_state.append(
            {"id": 1, "role": "user", "kind": "text",
             "content": body.get("content", ""), "created_at": "2026-01-01T00:00:00"}
        )
        messages_state.append(
            {"id": 2, "role": "clarifier", "kind": "clarify",
             "content": CLARIFY_JSON, "created_at": "2026-01-01T00:00:01"}
        )
        return route.fulfill(
            status=200, content_type="text/event-stream",
            body=sse({"type": "clarify", "content": CLARIFY_JSON}, {"type": "done"}),
        )
    return route.abort()


def vite_running() -> bool:
    try:
        with socket.create_connection(("localhost", 5173), timeout=1):
            return True
    except OSError:
        return False


def pager_text(page) -> str:
    raw = page.locator(".pending-pager").inner_text()
    m = re.search(r"(\d+/\d+)", raw)
    return m.group(1) if m else raw


def main() -> int:
    failures: list[str] = []
    dev_proc = None

    if not vite_running():
        dev_proc = subprocess.Popen(
            ["npm", "run", "dev"], cwd=FRONTEND_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if vite_running():
                break
            time.sleep(0.5)
        else:
            print("无法拉起 vite dev server（5173）", file=sys.stderr)
            return 2

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context()
            ctx.add_init_script("localStorage.setItem('atoms_token', 'fake-token')")
            ctx.route(lambda url: url.startswith(f"{BASE}/api/"), handle_api)
            page = ctx.new_page()

            page.goto(f"{BASE}/projects/1")
            page.locator('[data-testid="chat-input"]').fill("做一个待办应用")
            page.locator('[data-testid="chat-input"]').press("Enter")

            # 弹窗自动张开，停在第 1 题
            expect(page.locator('[data-testid="panel-option-0-0"]')).to_be_visible(timeout=10000)
            assert pager_text(page) == "1/3", f"初始页码应为 1/3，实际 {pager_text(page)}"

            # 非末题：选中后自动切到下一题
            page.locator('[data-testid="panel-option-0-1"]').click()
            page.wait_for_timeout(400)
            got = pager_text(page)
            if got != "2/3":
                failures.append(f"第 1 题选择后未自动翻页：期望 2/3，实际 {got}")
                page.locator(".pending-pager button").nth(1).click()  # 手动翻页续跑
                page.wait_for_timeout(200)

            page.locator('[data-testid="panel-option-1-0"]').click()
            page.wait_for_timeout(400)
            got = pager_text(page)
            if got != "3/3":
                failures.append(f"第 2 题选择后未自动翻页：期望 3/3，实际 {got}")
                page.locator(".pending-pager button").nth(1).click()  # 手动翻页续跑
                page.wait_for_timeout(200)

            # 末题：停留原页、不自动提交，「继续」可用
            posts_before = len(post_bodies)
            page.locator('[data-testid="panel-option-2-0"]').click()
            page.wait_for_timeout(800)
            got = pager_text(page)
            if got != "3/3":
                failures.append(f"末题选择后页码异常：期望停留 3/3，实际 {got}")
            if len(post_bodies) != posts_before:
                failures.append("末题选择后发生了自动提交（应等待用户点击「继续」）")
            if page.locator('[data-testid="panel-continue"]').is_disabled():
                failures.append("全部作答后「继续」按钮仍被禁用")

            # 主动点击「继续」才提交
            page.locator('[data-testid="panel-continue"]').click()
            page.wait_for_timeout(1500)
            if len(post_bodies) != posts_before + 1 or not post_bodies[-1].get("clarify_answer"):
                failures.append("点击「继续」未发出澄清答案提交请求")

            browser.close()
    finally:
        if dev_proc is not None:
            subprocess.run(
                ["taskkill", "/pid", str(dev_proc.pid), "/t", "/f"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    if failures:
        print("FAIL —— 澄清翻页回归未过：")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS —— 选题自动翻页、末题停留待提交、手动提交均符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
