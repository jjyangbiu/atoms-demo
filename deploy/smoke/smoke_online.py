"""线上验收冒烟脚本（工单 0014）：一条命令走完评审演示全链路。

链路：注册 → 建项目（工程师模式）→ "做一个番茄钟" → 预览可用 →
迭代"加一个统计区" → 发布 → 匿名打开 /p/{slug} → 另一账号克隆 → 继续迭代。

仅依赖标准库，可在任何装有 Python 3.10+ 的机器上运行：

    python deploy/smoke/smoke_online.py http://<ECS-IP>

任一步失败即以非零码退出并指明步骤；全部通过输出 SMOKE PASS。
"""

import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

GENERATE_TIMEOUT = 900  # 单次流式生成（含澄清/共识/工程师流）的最长等待秒数
PLAIN_TIMEOUT = 30
MAX_CLARIFY_ROUNDS = 4


class Client:
    """带 Cookie 与 Bearer 令牌的极简 HTTP 客户端：
    API 鉴权只认 Authorization 头（后端 get_current_user），
    预览链路由后端按登录 Cookie 鉴权，两者都自动携带。"""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.token: str | None = None
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _authed(self, req: urllib.request.Request) -> urllib.request.Request:
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        return req

    def json(self, path: str, body: dict | None = None, method: str | None = None, timeout=PLAIN_TIMEOUT):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = self._authed(
            urllib.request.Request(self.url(path), data=data, method=method or ("POST" if data else "GET"))
        )
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with self.opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)

    def drain_sse(self, path: str, body: dict) -> list[str]:
        """消费一次 SSE 流直到结束；返回 error 事件的 detail 列表（空即成功）。"""
        req = self._authed(
            urllib.request.Request(self.url(path), data=json.dumps(body).encode("utf-8"))
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream")
        errors: list[str] = []
        with self.opener.open(req, timeout=GENERATE_TIMEOUT) as resp:
            for line in resp:
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line.removeprefix("data: "))
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "error":
                    errors.append(str(event.get("detail")))
        return errors

    def get_raw(self, path: str):
        with self.opener.open(self._authed(urllib.request.Request(self.url(path))), timeout=PLAIN_TIMEOUT) as resp:
            return resp.status, resp.read(), dict(resp.headers)


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def check(cond: bool, message: str) -> None:
    if not cond:
        print(f"FAIL: {message}", flush=True)
        sys.exit(1)


def last_messages(client: Client, project_id: int) -> list[dict]:
    _, messages, _ = client.json(f"/api/projects/{project_id}/messages")
    return messages or []


def files_exist(client: Client, project_id: int) -> bool:
    _, files, _ = client.json(f"/api/projects/{project_id}/files")
    return bool(files)


def pending_action(client: Client, project_id: int) -> str | None:
    """从对话历史推导当前待办：clarify（待回答）/ consensus（待确认）/ None。"""
    messages = last_messages(client, project_id)
    for m in reversed(messages):
        if m["role"] == "user":
            return None  # 最后发言者是用户：等待服务端动作已在流内完成
        if m["role"] == "clarifier" and m["kind"] == "clarify":
            return "clarify"
        if m["role"] == "clarifier" and m["kind"] == "text":
            return "clarify"  # 文本形态的澄清提问同样需要回答推进
        if m["kind"] == "consensus":
            return "consensus"
        if m["kind"] == "consensus_confirm":
            return None
    return None


def drive_to_files(client: Client, project_id: int, first_prompt: str) -> None:
    """发送首条诉求并把项目驱动到"有文件"：应答澄清、确认共识，直到生成完成。"""
    errors = client.drain_sse(f"/api/projects/{project_id}/messages", {"content": first_prompt})
    check(not errors, f"首条消息生成流出错: {errors}")

    answer = "都按你的建议与默认值来，没有额外补充，请尽快生成可用版本。"
    for _ in range(MAX_CLARIFY_ROUNDS + 2):
        if files_exist(client, project_id):
            return
        action = pending_action(client, project_id)
        if action == "clarify":
            errors = client.drain_sse(
                f"/api/projects/{project_id}/messages",
                {"content": answer, "clarify_answer": True},
            )
            check(not errors, f"澄清应答流出错: {errors}")
        elif action == "consensus":
            errors = client.drain_sse(f"/api/projects/{project_id}/consensus/confirm", {"feedback": ""})
            check(not errors, f"共识确认生成流出错: {errors}")
        else:
            # 无待办但仍无文件：等待落盘后复查一次
            time.sleep(2)
            if files_exist(client, project_id):
                return
            check(False, "生成流结束但未产出文件，也无待确认动作")
    check(False, f"澄清/确认轮数超过上限（{MAX_CLARIFY_ROUNDS + 2}）仍未生成文件")


def iterate(client: Client, project_id: int, prompt: str) -> None:
    """已有文件的项目直接走工程师迭代。"""
    errors = client.drain_sse(f"/api/projects/{project_id}/messages", {"content": prompt})
    check(not errors, f"迭代生成流出错: {errors}")


def register_and_login(base: str, tag: str) -> Client:
    client = Client(base)
    username = f"smoke_{tag}_{secrets.token_hex(3)}"
    password = f"Smoke-{secrets.token_hex(8)}"
    status, _, _ = client.json("/api/auth/register", {"username": username, "password": password})
    check(status == 201, f"注册失败（{status}）")
    status, body, _ = client.json("/api/auth/login", {"username": username, "password": password})
    check(status == 200 and body.get("access_token"), f"登录失败（{status}）")
    client.token = body["access_token"]
    print(f"  用户 {username} 注册并登录成功")
    return client


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    base = sys.argv[1]
    started = time.time()

    step("健康检查")
    status, body, _ = Client(base).json("/api/health")
    check(status == 200 and body.get("ok"), f"/api/health 异常（{status}）")
    print("  /api/health OK")

    step("注册账号 A 并创建工程师模式项目")
    alice = register_and_login(base, "a")
    status, project, _ = alice.json("/api/projects", {"name": "番茄钟冒烟", "mode": "engineer"})
    check(status == 201 and project.get("id"), f"建项目失败（{status}）")
    pid = project["id"]
    print(f"  项目 #{pid} 创建成功")

    step("首次生成：做一个番茄钟")
    drive_to_files(alice, pid, "做一个番茄钟：25 分钟工作、5 分钟休息，支持开始/暂停/重置")
    print("  生成完成，项目已有文件")

    step("预览（登录态鉴权代理）")
    status, html, headers = alice.get_raw(f"/api/projects/{pid}/preview")
    check(status == 200 and b"<html" in html.lower(), f"预览不可用（{status}）")
    check(headers.get("Content-Type", "").startswith("text/html"), "预览 Content-Type 异常")
    print("  预览 200，HTML 正常返回")

    step("迭代：加一个统计区")
    iterate(alice, pid, "加一个统计区：记录今日已完成的番茄数")
    status, _, _ = alice.get_raw(f"/api/projects/{pid}/preview")
    check(status == 200, f"迭代后预览不可用（{status}）")
    print("  迭代完成，预览仍可用")

    step("发布并匿名访问公开链接")
    status, pub, _ = alice.json(f"/api/projects/{pid}/publish", {}, method="POST")
    check(status in (200, 201) and pub.get("slug"), f"发布失败（{status}）")
    slug = pub["slug"]
    anonymous = Client(base)  # 全新会话、无 Cookie，等价隐身窗口
    status, html, headers = anonymous.get_raw(f"/p/{slug}")
    check(status == 200 and b"<html" in html.lower(), f"公开链接不可用（{status}）")
    check("sandbox" in headers.get("Content-Security-Policy", ""), "公开链接缺少 CSP sandbox")
    print(f"  {base}/p/{slug} 匿名可访问，CSP sandbox 就位")

    step("账号 B 克隆并继续迭代")
    bob = register_and_login(base, "b")
    status, clone, _ = bob.json(f"/api/world/{slug}/clone", {}, method="POST")
    check(status == 201 and clone.get("id"), f"克隆失败（{status}）")
    clone_id = clone["id"]
    iterate(bob, clone_id, "把页面标题加上「克隆版」字样")
    check(files_exist(bob, clone_id), "克隆项目迭代后无文件")
    status, _, _ = bob.get_raw(f"/api/projects/{clone_id}/preview")
    check(status == 200, f"克隆项目预览不可用（{status}）")
    print(f"  克隆项目 #{clone_id} 迭代并预览成功")

    print(f"\nSMOKE PASS（用时 {int(time.time() - started)}s）：{base}")


if __name__ == "__main__":
    main()
