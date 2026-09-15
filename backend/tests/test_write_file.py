"""write_file 限制的单元测试：只能新建，不能修改已有文件。"""

import pytest

from app.agent.tools import FileSandbox, SandboxViolation


@pytest.fixture
def sandbox(tmp_path):
    return FileSandbox(tmp_path)


class TestWriteFileRestriction:
    """write_file 对已有文件的拒绝行为。"""

    def test_write_file_new_succeeds(self, sandbox):
        """目标文件不存在 → 正常创建。"""
        result = sandbox.write_file("index.html", "<h1>Hello</h1>")
        assert "已写入" in result
        assert sandbox.read_file("index.html") == "<h1>Hello</h1>"

    def test_write_file_existing_raises(self, sandbox):
        """目标文件已存在 → 拒绝，引导走 edit_file。"""
        sandbox.write_file("index.html", "<h1>v1</h1>")
        with pytest.raises(SandboxViolation, match="已存在.*edit_file"):
            sandbox.write_file("index.html", "<h1>v2</h1>")
        # 文件未被覆写
        assert sandbox.read_file("index.html") == "<h1>v1</h1>"

    def test_write_file_after_edit_still_raises(self, sandbox):
        """write_file 创建 → edit_file 修改 → 再 write_file 仍拒绝。"""
        sandbox.write_file("index.html", "<h1>v1</h1>")
        sandbox.read_file("index.html")  # 满足 read-before-write
        sandbox.edit_file("index.html", "v1", "v2")
        assert sandbox.read_file("index.html") == "<h1>v2</h1>"
        with pytest.raises(SandboxViolation, match="已存在"):
            sandbox.write_file("index.html", "<h1>v3</h1>")
        assert sandbox.read_file("index.html") == "<h1>v2</h1>"
