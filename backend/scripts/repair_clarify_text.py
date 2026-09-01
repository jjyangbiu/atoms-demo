"""一次性数据修复：把被裸文本落库的选项式澄清 JSON 残段升级为 clarify 卡片。

背景：部分推理模型把问题 JSON 写进 content（开头漏进 think 块、尾部截断），
旧版本后端按旧形态文本落库，前端渲染为裸 JSON 气泡。代码侧已修复（澄清流
恢复路径），本脚本修复历史存量数据：对每条 clarifier text 消息，尝试用
recover_clarify_payload 从「前一条 thinking 尾部 + 文本」恢复合法清单；
成功则改写为 kind=clarify，并裁掉 thinking 尾部漏出的 JSON 前缀。

用法：在 backend/ 目录下执行 `python scripts/repair_clarify_text.py`。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text as sql_text

from app.agent.tools import recover_clarify_payload
from app.config import get_settings

LEAK_MARKERS = ('[{"', "[{")


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    with engine.begin() as con:
        rows = con.execute(
            sql_text(
                "select id, project_id, content from messages "
                "where role = 'clarifier' and kind = 'text' order by id"
            )
        ).fetchall()
        fixed = 0
        for row in rows:
            prev = con.execute(
                sql_text(
                    "select id, content from messages "
                    "where project_id = :pid and id < :mid and kind = 'thinking' "
                    "order by id desc limit 1"
                ),
                {"pid": row.project_id, "mid": row.id},
            ).fetchone()
            thinking_tail = prev.content if prev else ""
            # 安全闸：thinking 自身已含完整清单草稿时不动（避免误改旧形态自由文本提问）
            if recover_clarify_payload(thinking_tail) is not None:
                continue
            recovered = recover_clarify_payload(thinking_tail + row.content)
            if recovered is None:
                continue
            payload = json.dumps(recovered, ensure_ascii=False)
            con.execute(
                sql_text("update messages set kind = 'clarify', content = :c where id = :id"),
                {"c": payload, "id": row.id},
            )
            # 裁掉 thinking 尾部漏出的 JSON 前缀，思考回看更干净
            if prev:
                cut = max(prev.content.rfind(m) for m in LEAK_MARKERS)
                if cut > 0:
                    con.execute(
                        sql_text("update messages set content = :c where id = :id"),
                        {"c": prev.content[:cut].rstrip(), "id": prev.id},
                    )
            fixed += 1
            print(f"repaired message {row.id} (project {row.project_id})")
    print(f"done, {fixed} message(s) repaired")


if __name__ == "__main__":
    main()
