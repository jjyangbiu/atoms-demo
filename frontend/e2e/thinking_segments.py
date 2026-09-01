"""多轮思考流式归位回归（E2E，Playwright）：

一条 SSE 流内出现多轮生成（智能体多步工具循环 / 工单串行执行）时，
后续轮次的 thinking/text 增量必须各自开新条目、排在对应工具/进度行之后，
而不是全部并入第一轮创建的思考块/文本气泡。

- 场景 A（工程师多步）：thinking1 → tool → thinking2，流式期间应有两张思考卡，
  且顺序为 思考 / 工具 / 思考；
- 场景 B（工单串行）：进度1 → thinking/文本（工单一）→ 进度2 → thinking/文本（工单二），
  流式期间思考卡与文本气泡各两份且按轮归位。

全量 mock /api，SSE 由页内 fetch 桩按测试节奏逐事件推送，确定性可重放。
运行：pip install playwright && playwright install chromium，然后
  python frontend/e2e/thinking_segments.py
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

# 模拟服务端持久化状态：随流推进，流结束后前端 loadHistory 据此重建
messages_state: list[dict] = []
_msg_id = 0


def next_msg(role: str, kind: str, content: str) -> None:
    global _msg_id
    _msg_id += 1
    messages_state.append(
        {"id": _msg_id, "role": role, "kind": kind, "content": content,
         "created_at": "2026-01-01T00:00:00"}
    )


# 页内 fetch 桩：POST /messages 返回可控 SSE 流，事件由测试逐个推入
FETCH_STUB = """
(() => {
  const origFetch = window.fetch;
  window.__sseQueue = [];
  window.__sseWake = [];
  window.__sseClosed = false;
  window.__pushSse = (ev) => { window.__sseQueue.push(ev); window.__sseWake.splice(0).forEach((r) => r()); };
  window.__endSse = () => { window.__sseClosed = true; window.__sseWake.splice(0).forEach((r) => r()); };
  window.__resetSse = () => { window.__sseQueue = []; window.__sseClosed = false; };
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    const method = ((init && init.method) || 'GET').toUpperCase();
    if (url.includes('/api/projects/1/messages') && method === 'POST') {
      const enc = new TextEncoder();
      const stream = new ReadableStream({
        async pull(controller) {
          for (;;) {
            if (window.__sseQueue.length) {
              const ev = window.__sseQueue.shift();
              controller.enqueue(enc.encode('data: ' + JSON.stringify(ev) + '\\n\\n'));
              return;
            }
            if (window.__sseClosed) { controller.close(); return; }
            await new Promise((r) => window.__sseWake.push(r));
          }
        },
      });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    }
    return origFetch(input, init);
  };
})();
"""


def handle_api(route):
    req = route.request
    url = req.url
    if url.endswith("/api/auth/me"):
        return route.fulfill(json={"id": 1, "username": "tester", "created_at": "2026-01-01T00:00:00"})
    if re.search(r"/api/projects/1$", url):
        return route.fulfill(json={"name": "demo", "mode": "engineer", "published_slug": None})
    if url.endswith("/api/projects/1/files") or url.endswith("/api/projects/1/snapshots"):
        return route.fulfill(json=[])
    if url.endswith("/api/projects/1/tickets"):
        return route.fulfill(json=[])
    if url.endswith("/api/projects/1/messages"):
        return route.fulfill(json=messages_state)
    return route.abort()


def vite_running() -> bool:
    try:
        with socket.create_connection(("localhost", 5173), timeout=1):
            return True
    except OSError:
        return False


def chat_seq(page) -> list[str]:
    """聊天区条目按文档序的类型串：thinking / tool / text / user / progress"""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('.chat-body > *'))
            .map((el) => {
              if (el.querySelector('.thinking-card')) return 'thinking';
              if (el.querySelector('.agent-bubble')) return 'text';
              if (el.querySelector('.user-bubble')) return 'user';
              if (el.classList.contains('tool-line')) {
                return el.textContent.includes('工单') ? 'progress' : 'tool';
              }
              return 'other';
            })
            .filter((t) => t !== 'other')"""
    )


def push(page, event: dict, settle_ms: int = 250) -> None:
    page.evaluate("(ev) => window.__pushSse(ev)", event)
    page.wait_for_timeout(settle_ms)


def main() -> int:
    failures: list[str] = []
    dev_proc = None

    if not vite_running():
        dev_proc = subprocess.Popen(
            ["npm.cmd", "run", "dev"], cwd=FRONTEND_DIR,
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
            ctx.add_init_script(FETCH_STUB)
            ctx.route(lambda url: url.startswith(f"{BASE}/api/"), handle_api)
            page = ctx.new_page()
            page.goto(f"{BASE}/projects/1")
            expect(page.locator('[data-testid="chat-input"]')).to_be_visible(timeout=10000)

            # --- 场景 A：工程师多步（thinking → tool → thinking） ---
            page.locator('[data-testid="chat-input"]').fill("做一个待办应用")
            page.locator('[data-testid="chat-input"]').press("Enter")

            push(page, {"type": "thinking", "content": "第一步的思考"})
            push(page, {"type": "tool", "name": "write_file",
                        "args": {"path": "index.html"}, "status": "start"})
            push(page, {"type": "tool", "name": "write_file",
                        "args": {"path": "index.html"}, "status": "done", "result": "ok"})
            push(page, {"type": "thinking", "content": "第二步的思考"})

            cards = page.locator(".thinking-card")
            try:
                expect(cards).to_have_count(2, timeout=4000)
            except AssertionError:
                failures.append(
                    f"场景A：流式期间思考卡应为 2 张（每轮一张），实际 {cards.count()} 张"
                )
            first_body = page.locator(".thinking-card .thinking-body").first
            try:
                expect(first_body).not_to_contain_text("第二步的思考", timeout=3000)
            except AssertionError:
                failures.append("场景A：第二轮思考被并入了第一张思考卡")
            seq = chat_seq(page)
            want_prefix = ["user", "thinking", "tool", "thinking"]
            if seq[:4] != want_prefix:
                failures.append(f"场景A：流式条目顺序应为 {want_prefix}，实际 {seq}")

            # 收尾：done 后前端以持久化历史重建（后端把整轮思考合并落一行）
            next_msg("user", "text", "做一个待办应用")
            next_msg("engineer", "event",
                     json.dumps({"name": "write_file", "args": {"path": "index.html"},
                                 "status": "done", "result": "ok"}, ensure_ascii=False))
            next_msg("engineer", "thinking", "第一步的思考第二步的思考")
            next_msg("engineer", "text", "已完成初版。")
            push(page, {"type": "text", "content": "已完成初版。"}, settle_ms=100)
            push(page, {"type": "done", "text": "已完成初版。"}, settle_ms=0)
            page.evaluate("() => window.__endSse()")
            expect(page.locator(".agent-bubble")).to_have_count(1, timeout=8000)
            expect(page.locator(".thinking-card")).to_have_count(1, timeout=8000)

            # --- 场景 B：工单串行（一个流内多张工单各自产出 thinking/text） ---
            page.evaluate("() => window.__resetSse()")
            page.locator('[data-testid="chat-input"]').fill("继续完善")
            page.locator('[data-testid="chat-input"]').press("Enter")

            push(page, {"type": "ticket_progress", "seq": 1, "title": "页面骨架", "status": "running"})
            push(page, {"type": "thinking", "content": "工单一的思考"})
            push(page, {"type": "text", "content": "工单一完成。"}, settle_ms=400)
            push(page, {"type": "ticket_progress", "seq": 1, "title": "页面骨架",
                        "status": "done", "snapshot_rev": 1})
            push(page, {"type": "ticket_progress", "seq": 2, "title": "交互逻辑", "status": "running"})
            push(page, {"type": "thinking", "content": "工单二的思考"})
            push(page, {"type": "text", "content": "工单二完成。"}, settle_ms=400)

            # 场景 A 重建后留有 1 张思考卡与 1 个文本气泡，本轮两张工单各加一张/一个
            try:
                expect(page.locator(".thinking-card")).to_have_count(3, timeout=4000)
            except AssertionError:
                failures.append(
                    f"场景B：流式期间思考卡应为 3 张（历史 1 + 每单一张），实际 {page.locator('.thinking-card').count()} 张"
                )
            try:
                expect(page.locator(".agent-bubble")).to_have_count(3, timeout=4000)
            except AssertionError:
                failures.append(
                    f"场景B：流式期间文本气泡应为 3 个（历史 1 + 每单一个），实际 {page.locator('.agent-bubble').count()} 个"
                )
            seq = chat_seq(page)
            want = ["user", "progress", "thinking", "text", "progress", "progress", "thinking", "text"]
            tail = seq[-len(want):]
            if tail != want:
                failures.append(f"场景B：流式条目顺序应为 …{want}，实际 {seq}")

            browser.close()
    finally:
        if dev_proc is not None:
            subprocess.run(
                ["taskkill", "/pid", str(dev_proc.pid), "/t", "/f"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    if failures:
        print("FAIL —— 多轮思考流式归位回归未过：")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS —— 多轮 thinking/text 流式期间各自开新条目且按轮归位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
