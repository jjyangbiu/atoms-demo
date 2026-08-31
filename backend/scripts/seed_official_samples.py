"""官方示例灌入 CLI（工单 0012）：重建/补齐 App 世界画廊的冷启动示例。

用法（在 backend/ 目录下）：

    uv run python scripts/seed_official_samples.py

依赖常规环境变量（见 .env）：ATOMS_LLM_API_KEY 等；未配置时模型工厂报错，
对应示例会以失败记录在输出里。脚本可重复执行：已灌入的示例自动跳过。
"""

import sys
from pathlib import Path

# 允许从任意目录执行：把 backend/ 加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import create_app
from app.official_samples import seed_official_samples

_MARKS = {"seeded": "√", "skipped": "-", "failed": "×"}
_LABELS = {"seeded": "已灌入", "skipped": "已存在", "failed": "失败"}


def main() -> int:
    app = create_app()
    with TestClient(app) as client:
        results = seed_official_samples(app, client)

    print("官方示例灌入结果：")
    for r in results:
        detail = f"/p/{r.slug}" if r.slug else r.error
        print(f"  {_MARKS[r.status]} {r.name}（{_LABELS[r.status]}）{detail}")
    failed = [r for r in results if r.status == "failed"]
    if failed:
        print(f"{len(failed)} 个示例失败，可修复后重跑本脚本补齐。")
        return 1
    print("画廊种子就绪；重跑本脚本不会重复生成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
