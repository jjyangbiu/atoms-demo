"""沙箱文件工具：智能体只能通过这三个工具触碰项目目录内的文件。

安全约束（规格 0001）：路径穿越防护 + 扩展名白名单。
"""

import inspect
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


class FileSandbox:
    """绑定到单个项目目录的文件工具实现。"""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def read_file(self, path: str) -> str:
        target = resolve_sandboxed(self.root, path)
        if not target.is_file():
            raise SandboxViolation(f"文件不存在: {path}")
        return target.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        target = resolve_sandboxed(self.root, path)
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise SandboxViolation("文件内容超过 512KB 上限")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"已写入 {path}（{len(data)} 字节）"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        current = self.read_file(path)
        if old_text not in current:
            raise SandboxViolation(f"在 {path} 中未找到要替换的内容，请先 read_file 确认")
        return self.write_file(path, current.replace(old_text, new_text, 1))


def build_tools(sandbox: FileSandbox) -> list:
    """以闭包绑定沙箱，产出可供模型 bind_tools 的 LangChain 工具。"""

    @tool
    def read_file(path: str) -> str:
        """读取项目内文件内容。参数: path — 相对项目根目录的文件路径，如 index.html。"""
        return sandbox.read_file(path)

    @tool
    def write_file(path: str, content: str) -> str:
        """创建新文件或整体覆盖已有文件。参数: path — 相对路径；content — 完整文件内容。"""
        return sandbox.write_file(path, content)

    @tool
    def edit_file(path: str, old_text: str, new_text: str) -> str:
        """对已有文件做局部替换（替换第一处出现的 old_text）。修改前请先 read_file。"""
        return sandbox.edit_file(path, old_text, new_text)

    return [read_file, write_file, edit_file]


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
