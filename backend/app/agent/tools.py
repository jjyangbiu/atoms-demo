"""沙箱文件工具：智能体只能通过这三个工具触碰项目目录内的文件。

安全约束（规格 0001）：路径穿越防护 + 扩展名白名单。
"""

import inspect
import json
import re
from pathlib import Path

from langchain_core.tools import tool

# 生成物限定纯前端（ADR 0001）：仅允许文本类资源扩展名
ALLOWED_EXTENSIONS = {".html", ".css", ".js", ".mjs", ".json", ".svg", ".md", ".txt"}
MAX_FILE_BYTES = 512 * 1024
# 项目目录内的系统保留目录（快照留档，工单 0007）：智能体与预览均不得触碰
RESERVED_DIRS = {"snapshots"}


class SandboxViolation(ValueError):
    """路径越界或扩展名不在白名单。"""


def resolve_sandboxed(root: Path, rel_path: str) -> Path:
    """把相对路径解析进沙箱；越界或扩展名非法即抛 SandboxViolation。"""
    if not rel_path or "\x00" in rel_path:
        raise SandboxViolation(f"非法路径: {rel_path!r}")
    first = rel_path.replace("\\", "/").strip("/").split("/", 1)[0]
    if first in RESERVED_DIRS:
        raise SandboxViolation(f"禁止访问系统保留目录: {first}")
    candidate = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise SandboxViolation(f"禁止访问项目目录之外的路径: {rel_path}")
    if candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise SandboxViolation(
            f"不支持的文件类型: {candidate.suffix or '(无扩展名)'}，"
            f"仅允许 {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
    return candidate


def _locate_ignoring_whitespace(current: str, old_text: str) -> re.Match | None:
    """在 current 中定位 old_text，忽略空白/缩进差异。

    把 old_text 按空白拆成 token，用 \\s+ 连接成正则，在 current 中搜索。
    仅在唯一命中时返回 Match（含原始空白区间），0 或多处命中返回 None。
    """
    tokens = old_text.split()
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    matches = list(re.finditer(pattern, current))
    if len(matches) == 1:
        return matches[0]
    return None


class FileSandbox:
    """绑定到单个项目目录的文件工具实现。"""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._read_this_round: set[str] = set()
        # 本轮真实改动的文件集（工单 0022）：与「本轮已读文件集」同构的辅助记录，
        # 路径统一为相对项目根的 posix 形式（已读集存的是模型传入的原始 path）。
        # 只在 _write_to_disk 成功落盘后登记——编辑失败、被闸门拒绝、零 diff
        # 空操作都在落盘前抛 SandboxViolation，天然不计入。
        self._modified_this_round: set[str] = set()

    @property
    def modified_this_round(self) -> set[str]:
        """本轮真实改动的文件路径集（相对项目根的 posix 形式）。"""
        return set(self._modified_this_round)

    def read_file(self, path: str) -> str:
        target = resolve_sandboxed(self.root, path)
        if not target.is_file():
            raise SandboxViolation(f"文件不存在: {path}")
        content = target.read_text(encoding="utf-8")
        self._read_this_round.add(path)
        return content

    def _write_to_disk(self, path: str, content: str) -> str:
        """实际落盘（内部方法，不做存在性检查；edit_file 也经此持久化）。"""
        target = resolve_sandboxed(self.root, path)
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise SandboxViolation("文件内容超过 512KB 上限")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._modified_this_round.add(target.relative_to(self.root.resolve()).as_posix())
        return f"已写入 {path}（{len(data)} 字节）"

    def write_file(self, path: str, content: str) -> str:
        target = resolve_sandboxed(self.root, path)
        if target.is_file():
            raise SandboxViolation(
                f"文件 {path} 已存在；修改已有文件必须走 edit_file（先 read_file 再 edit_file），"
                f"write_file 只用于新建文件"
            )
        result = self._write_to_disk(path, content)
        self._read_this_round.add(path)  # 模型刚创建的文件，内容已知，无需再 read
        return result

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        """分层定位替换：精确唯一 → 精确歧义 → 空白模糊 → 失败指引。"""
        if path not in self._read_this_round:
            raise SandboxViolation(
                f"修改 {path} 前必须先 read_file 确认当前内容；不得凭记忆修改"
            )
        current = self.read_file(path)
        if not old_text:
            raise SandboxViolation(
                "old_text 不能为空；请先 read_file 确认要修改的内容，再提供精确的 old_text"
            )
        count = current.count(old_text)
        if count == 1:
            updated = current.replace(old_text, new_text, 1)
        elif count > 1:
            raise SandboxViolation(
                f"old_text 在 {path} 中出现 {count} 次，无法确定修改哪一处；"
                f"请提供更多上下文使 old_text 唯一"
            )
        else:
            # 精确匹配落空：尝试忽略空白/缩进差异的模糊匹配
            match = _locate_ignoring_whitespace(current, old_text)
            if match is None:
                raise SandboxViolation(
                    f"在 {path} 中未找到要替换的内容（含忽略空白差异的模糊匹配）；"
                    f"请先 read_file 确认当前内容，逐字复制 old_text"
                )
            updated = current[: match.start()] + new_text + current[match.end() :]
        if updated == current:
            # 零 diff 空操作（诊断修复 H3）：old_text 与 new_text 相同（或替换结果
            # 与原文一致）不得计为成功修改——否则 touched_files 非空、无 warning、
            # 迭代日志记为有改动、快照照建，但文件内容零变化，与“口头完成”同症状。
            raise SandboxViolation(
                f"本次替换是空操作：{path} 替换前后内容完全相同，文件未发生任何改动。"
                "请核实目标状态是否已存在于磁盘：若已存在，向用户说明现状"
                "（不要使用『已修改/已生效』之类完成断言措辞）；"
                "若不存在，提供真正产生差异的 new_text。"
            )
        return self._write_to_disk(path, updated)


def build_tools(sandbox: FileSandbox, knowledge_store=None) -> list:
    """以闭包绑定沙箱，产出可供模型 bind_tools 的 LangChain 工具。

    knowledge_store 可用时（工单 0009）额外提供 search_templates 模板检索工具；
    知识库不可用时不注册该工具，生成主链路不受影响。
    """

    @tool
    def read_file(path: str) -> str:
        """读取项目内文件内容。参数: path — 相对项目根目录的文件路径，如 index.html。"""
        return sandbox.read_file(path)

    @tool
    def write_file(path: str, content: str) -> str:
        """创建新文件。不能用于修改已有文件（修改请用 edit_file）。参数: path — 相对路径；content — 完整文件内容。"""
        return sandbox.write_file(path, content)

    @tool
    def edit_file(path: str, old_text: str, new_text: str) -> str:
        """对已有文件做局部替换。old_text 必须从 read_file 结果逐字复制且在文件中唯一；空白/缩进有差异时尝试模糊匹配。修改前必须先 read_file。"""
        return sandbox.edit_file(path, old_text, new_text)

    tools = [read_file, write_file, edit_file]
    if knowledge_store is not None:

        @tool
        def search_templates(query: str) -> str:
            """检索模板知识库，获取相关应用模板与技术片段作为参考。参数: query — 想构建的应用或功能描述。"""
            hits = knowledge_store.search(query, top_k=5)
            if not hits:
                return "未找到相关模板，可直接开始生成。"
            return "\n\n".join(f"【{h['title']}】\n{h['text']}" for h in hits)

        tools.append(search_templates)
    return tools


def build_clarify_tools() -> list:
    """澄清智能体的工具集（工单 0015 / ADR 0003）。

    澄清阶段不绑定任何文件工具：模型物理上无法提前写代码；
    提问出口是携结构化选项的 ask_options（前端渲染为可点选卡片），
    收敛出口是携带需求共识摘要的 start_build。
    """

    @tool
    def ask_options(questions: str) -> str:
        """提出澄清问题时调用，问题以可点选的选项卡片呈现。参数: questions — 问题清单 JSON 数组字符串，每项含 question（问题）、options（候选项字符串数组，2–4 个）、recommend（推荐项下标，从 0 起，无推荐可省）。"""
        return "已发出澄清问题，等待用户选择或输入回答。"

    @tool
    def start_build(requirements_summary: str) -> str:
        """澄清完成（或用户要求跳过澄清）时调用，产出需求共识。参数: requirements_summary — 澄清后达成的需求共识摘要（中文 Markdown）。"""
        return "已记录需求共识，等待用户确认。"

    return [ask_options, start_build]


def parse_clarify_payload(raw: str) -> tuple[list[dict] | None, str]:
    """校验澄清智能体提交的选项式问题清单；非法时返回 (None, 错误文案) 交还模型修正。

    约束：问题数 1–6；每题候选项 2–4 个且非空；recommend 为可选的合法下标。
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None, "questions 不是合法 JSON，请提交 JSON 数组字符串。"
    if not isinstance(data, list) or not data:
        return None, "问题清单必须是非空 JSON 数组。"
    if len(data) > 6:
        return None, f"一次最多提 6 个问题，当前为 {len(data)} 个，请合并或减少。"
    questions: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return None, f"第 {i + 1} 个问题必须是 JSON 对象。"
        question = str(item.get("question") or "").strip()
        if not question:
            return None, f"第 {i + 1} 个问题缺少 question。"
        options_raw = item.get("options")
        if not isinstance(options_raw, list) or not (2 <= len(options_raw) <= 4):
            return None, f"第 {i + 1} 个问题的 options 必须是 2–4 个候选项的数组。"
        options = [str(o or "").strip() for o in options_raw]
        if not all(options):
            return None, f"第 {i + 1} 个问题的候选项不能为空。"
        recommend = item.get("recommend")
        if recommend is not None:
            try:
                recommend = int(recommend)
            except (TypeError, ValueError):
                return None, f"第 {i + 1} 个问题的 recommend 必须是候选项下标。"
            if not 0 <= recommend < len(options):
                return None, f"第 {i + 1} 个问题的 recommend 下标越界。"
        questions.append({"question": question, "options": options, "recommend": recommend})
    return questions, ""


def recover_clarify_payload(raw: str) -> list[dict] | None:
    """模型未调 ask_options 而把选项式问题 JSON 写进 content 时，尽力恢复出问题清单。

    部分推理模型会把 JSON 开头漏进 think 块、或尾部被截断缺 `]`；
    故从后往前扫描每个 `[{` 候选起点，取到文末尝试解析，解析失败再补 `]` 重试，
    返回首个通过 parse_clarify_payload 校验的清单；自由文本命中不了合法清单形状，天然不会误恢复。
    """
    starts = [m.start() for m in re.finditer(r"\[\s*\{", raw)]
    for start in reversed(starts):
        candidate = raw[start:]
        for fixed in (candidate, candidate + "]"):
            questions, _reason = parse_clarify_payload(fixed)
            if questions is not None:
                return questions
    return None


def build_breaker_tools() -> list:
    """拆单智能体的唯一工具（工单 0017 / ADR 0003）。

    拆解阶段不绑定任何文件工具：模型物理上无法提前写代码，
    唯一出口是携带工单清单 JSON 的 submit_tickets。
    """

    @tool
    def submit_tickets(tickets: str) -> str:
        """拆解完成时调用，提交工单清单。参数: tickets — 工单清单 JSON 数组字符串，每项含 title（标题）、deliverable（交付内容）、blocked_by（阻塞它的工单序号列表，无依赖为 []）。"""
        return "已提交工单清单。"

    return [submit_tickets]


def parse_ticket_payload(raw: str) -> tuple[list[dict] | None, str]:
    """校验拆单智能体提交的工单清单；非法时返回 (None, 错误文案) 交还模型修正。

    约束（工单 0017）：数量个位数（1–9）；序号按提交顺序从 1 起编；
    blocked_by 只引用序号更小的工单（首单无依赖），天然不成环。
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None, "tickets 不是合法 JSON，请提交 JSON 数组字符串。"
    if not isinstance(data, list) or not data:
        return None, "工单清单必须是非空 JSON 数组。"
    if len(data) > 9:
        return None, f"工单数量应控制在个位数，当前为 {len(data)} 个，请合并粒度。"
    tickets: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return None, f"第 {i + 1} 个工单必须是 JSON 对象。"
        title = str(item.get("title") or "").strip()
        deliverable = str(item.get("deliverable") or "").strip()
        if not title or not deliverable:
            return None, f"第 {i + 1} 个工单缺少 title 或 deliverable。"
        blocked_raw = item.get("blocked_by") or []
        if not isinstance(blocked_raw, list):
            return None, f"第 {i + 1} 个工单的 blocked_by 必须是序号数组。"
        try:
            blocked = sorted({int(x) for x in blocked_raw})
        except (TypeError, ValueError):
            return None, f"第 {i + 1} 个工单的 blocked_by 含非整数序号。"
        seq = i + 1
        if any(b >= seq for b in blocked):
            return None, f"工单 {seq} 的 blocked_by 只能引用序号更小的工单。"
        tickets.append(
            {"seq": seq, "title": title, "deliverable": deliverable, "blocked_by": blocked}
        )
    return tickets, ""


def execute_tool(tools: list, name: str, args: dict) -> tuple[bool, str]:
    """按名称执行工具，返回 (是否成功, 结果文本)；异常转成失败结果交还模型。"""
    for t in tools:
        if t.name != name:
            continue
        try:
            params = inspect.signature(t.func).parameters
            return True, str(t.func(**{k: v for k, v in (args or {}).items() if k in params}))
        except Exception as e:  # noqa: BLE001 — 工具错误须回传模型而非中断循环
            return False, f"工具执行失败: {e}"
    return False, f"工具执行失败: 未知工具 {name}"
