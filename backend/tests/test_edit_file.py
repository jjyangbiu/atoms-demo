"""edit_file 分层定位的单元测试：在 FileSandbox 接缝层固化全部失配症状。

覆盖 6 个症状：精确唯一命中、精确多处歧义、缩进漂移模糊匹配、
跨行折叠模糊匹配、空 old_text、完全未命中（含恢复指引）。
"""

import pytest

from app.agent.tools import FileSandbox, SandboxViolation


@pytest.fixture
def sandbox(tmp_path):
    return FileSandbox(tmp_path)


def _seed(sandbox: FileSandbox, path: str, content: str) -> None:
    """直接落盘并模拟模型已读取（满足 read-before-write 约束）。"""
    target = sandbox.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    sandbox.read_file(path)  # 模拟模型已 read_file，_read_this_round 纳入该路径


class TestEditFileLayeredLocalization:
    """分层定位：精确唯一 → 精确歧义 → 空白模糊 → 失败指引。"""

    def test_exact_unique_match_replaces(self, sandbox):
        """old_text 精确命中且唯一 → 直接替换，行为与旧版一致。"""
        _seed(sandbox, "index.html", "<h1>Hello</h1><p>World</p>")
        result = sandbox.edit_file("index.html", "<h1>Hello</h1>", "<h1>Hi</h1>")
        assert "已写入" in result
        assert sandbox.read_file("index.html") == "<h1>Hi</h1><p>World</p>"

    def test_exact_multiple_match_raises_ambiguity(self, sandbox):
        """old_text 精确命中多处 → 报歧义并返回命中次数，不静默替换第一处。"""
        _seed(sandbox, "index.html", "<p>a</p><p>a</p><p>a</p>")
        with pytest.raises(SandboxViolation, match="出现 3 次"):
            sandbox.edit_file("index.html", "<p>a</p>", "<p>b</p>")
        # 文件未被修改
        assert sandbox.read_file("index.html") == "<p>a</p><p>a</p><p>a</p>"

    def test_whitespace_drift_fuzzy_match(self, sandbox):
        """模型记忆的 old_text 缩进与文件不同 → 模糊匹配命中，未涉及区域不变。"""
        _seed(sandbox, "index.html", "    <h1>Hello</h1>\n    <p>World</p>")
        # 模型用 2 空格缩进，文件是 4 空格
        result = sandbox.edit_file("index.html", "  <h1>Hello</h1>", "  <h1>Hi</h1>")
        assert "已写入" in result
        content = sandbox.read_file("index.html")
        assert "<h1>Hi</h1>" in content
        assert "<p>World</p>" in content  # 未涉及的部分保持不变

    def test_multiline_fold_fuzzy_match(self, sandbox):
        """模型把跨行片段折叠成单行 → 模糊匹配命中，功能正确。"""
        _seed(sandbox, "index.html", "<h1>Hello</h1>\n<p>World</p>")
        # 模型记忆为单行（空格分隔），文件是跨行（换行分隔）
        result = sandbox.edit_file(
            "index.html",
            "<h1>Hello</h1> <p>World</p>",
            "<h1>Hi</h1> <p>Earth</p>",
        )
        assert "已写入" in result
        content = sandbox.read_file("index.html")
        assert "<h1>Hi</h1>" in content
        assert "<p>Earth</p>" in content

    def test_empty_old_text_raises(self, sandbox):
        """old_text 为空 → 拒绝（旧版会把 new_text 插到文件开头）。"""
        _seed(sandbox, "index.html", "<h1>Hello</h1>")
        with pytest.raises(SandboxViolation, match="不能为空"):
            sandbox.edit_file("index.html", "", "<h1>Hi</h1>")
        # 文件未被修改
        assert sandbox.read_file("index.html") == "<h1>Hello</h1>"

    def test_no_match_raises_with_recovery_guidance(self, sandbox):
        """old_text 完全不存在 → 抛错并含可执行恢复指引（read_file）。"""
        _seed(sandbox, "index.html", "<h1>Hello</h1>")
        with pytest.raises(SandboxViolation, match="read_file"):
            sandbox.edit_file("index.html", "<h2>NotExist</h2>", "<h2>Hi</h2>")
        # 文件未被修改
        assert sandbox.read_file("index.html") == "<h1>Hello</h1>"

    def test_edit_without_read_raises(self, sandbox):
        """未先 read_file 就 edit_file → 拒绝（read-before-write 强制）。"""
        # 直接落盘，不经过 read_file（_read_this_round 为空）
        target = sandbox.root / "index.html"
        target.write_text("<h1>Hello</h1>", encoding="utf-8")
        with pytest.raises(SandboxViolation, match="read_file"):
            sandbox.edit_file("index.html", "<h1>Hello</h1>", "<h1>Hi</h1>")
        # 文件未被修改
        assert target.read_text(encoding="utf-8") == "<h1>Hello</h1>"

    def test_noop_edit_raises(self, sandbox):
        """old_text == new_text 的零 diff 空操作 → 拒绝，不得计为成功修改。

        诊断修复 H3：空操作若返回成功，touched_files 非空 → 无 warning、
        迭代日志记为有改动、快照照建，但磁盘零变化——与“口头完成”同症状。
        """
        _seed(sandbox, "index.html", "<h1>工作日历</h1>")
        with pytest.raises(SandboxViolation, match="空操作"):
            sandbox.edit_file("index.html", "<h1>工作日历</h1>", "<h1>工作日历</h1>")
        assert sandbox.read_file("index.html") == "<h1>工作日历</h1>"

    def test_fuzzy_noop_edit_raises(self, sandbox):
        """模糊匹配命中但替换结果与原文一致 → 同样拒绝（零 diff 不分定位路径）。

        old_text 是跨行片段折叠成的单行（精确匹配落空、模糊命中），
        new_text 恰好还原文件原样的跨行内容 → 替换后内容不变。
        """
        _seed(sandbox, "index.html", "    <h1>Hello</h1>\n    <p>World</p>")
        with pytest.raises(SandboxViolation, match="空操作"):
            sandbox.edit_file(
                "index.html",
                "<h1>Hello</h1> <p>World</p>",
                "<h1>Hello</h1>\n    <p>World</p>",
            )
        assert sandbox.read_file("index.html") == "    <h1>Hello</h1>\n    <p>World</p>"
