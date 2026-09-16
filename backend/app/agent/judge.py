"""正确性裁判（工单 0028 阶段一 / ADR 0005「第 8 层」正确性裁决）：轮末裁决磁盘真实改动。

只在「改动代码轮且磁盘确有改动」时触发（咨询轮、零改动轮不触发，零调用成本）；
输入是用户原话 + 最近若干轮上下文 + 代码算好的 diff（difflib 带行号，裁判不
自己数行）；输入禁区是任何智能体自述——summary、自报意图、申报改动文件、
分类器蒸馏的 user_goal 一概不进入本调用，history 里的 AI 消息（轮次产物结论
回放）在装配消息时就地滤除。

阶段一只标注不阻断：三种裁决值的处置全在路由层（卡片标注、快照照建），不自动
回滚越界段落、不把裁决回喂返工。裁判失败（网络/超时/非法输出）复用既有重试
次数与退避语义重试，仍失败返回 None，由路由层标注「核验未完成」（unverified）
且快照照建——安全网故障不得扣住用户的劳动成果，且必须显式化而非静默吞掉。

通道选择同分类器（intent.py）：不绑工具，以 JSON Mode 收尾——response_format
与工具是互斥的输出通道。JSON Mode 只保证语法合法，服务端 schema 校验必须做
（parse_scope_verdict_payload）；非法输出回喂错误文案修正一次。

字段名说明：ADR 0005 草案写作 out_of_scope_hunks，工单 0028 细化为「越界段落
清单（文件、行区间、理由）」——裁决产物是改动后文件里的越界区域（segment），
不是原始 diff hunk，实现按细化后的 out_of_scope_segments 命名。
"""

import asyncio
import difflib
import json
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from .loop import RETRY_BACKOFF_SECONDS, strip_think_blocks
from .verdicts import OUT_OF_SCOPE, SCOPE_VERDICT_ENUM

# 非法输出的回喂修正预算：修正一次（房规同分类器/澄清/拆单/终结出口）
_MAX_REFEEDS = 1


def build_diff_text(touched: set[str], pre_contents: dict[str, str], root: Path) -> str:
    """以 difflib 为改动集生成带行号的 unified diff 文本（裁判不自己数行）。

    pre_contents 是轮前磁盘内容快照（缺失/不可解码视为空文件）；轮后内容即时
    重读磁盘（文件已删除则新内容为空）。diff 基准是磁盘而非上一个快照——房规
    同改动集的权威来源（工单 0022），越界轮不建快照也不会使基准脱钩。
    """
    chunks: list[str] = []
    for rel in sorted(touched):
        f = root / rel
        try:
            new_text = f.read_text(encoding="utf-8") if f.is_file() else ""
        except (UnicodeDecodeError, OSError):
            new_text = ""
        old_text = pre_contents.get(rel, "")
        diff = "\n".join(
            difflib.unified_diff(
                old_text.splitlines(),
                new_text.splitlines(),
                fromfile=f"改动前/{rel}",
                tofile=f"改动后/{rel}",
                lineterm="",
            )
        )
        if diff:
            chunks.append(diff)
    return "\n\n".join(chunks)


def _parse_segment(raw, idx: int) -> tuple[dict | None, str]:
    """校验单个越界段落：file 非空路径、行号 1 ≤ start ≤ end、reason 非空。"""
    if not isinstance(raw, dict):
        return None, f"out_of_scope_segments[{idx}] 必须是对象。"
    file_path = str(raw.get("file") or "").strip()
    if not file_path:
        return None, f"out_of_scope_segments[{idx}].file 不能为空。"
    try:
        start_line = int(raw.get("start_line"))
        end_line = int(raw.get("end_line"))
    except (TypeError, ValueError):
        return None, f"out_of_scope_segments[{idx}] 的行号必须是整数。"
    if start_line < 1 or end_line < start_line:
        return None, f"out_of_scope_segments[{idx}] 的行号区间非法（须 1 ≤ start ≤ end）。"
    reason = str(raw.get("reason") or "").strip()
    if not reason:
        return None, f"out_of_scope_segments[{idx}].reason 不能为空。"
    return (
        {
            "file": file_path,
            "start_line": start_line,
            "end_line": end_line,
            "reason": reason,
        },
        "",
    )


def parse_scope_verdict_payload(raw: str) -> tuple[dict | None, str]:
    """校验裁判输出 JSON；非法时返回 (None, 错误文案) 交还模型修正（房规同其他 parse_*）。

    约束：JSON 对象；verdict 限于三值枚举；out_of_scope_segments 可缺省（视为
    空数组），给出则逐项校验；verdict 为 out_of_scope 时清单必须非空（无段落的
    「越界」没有处置价值，属自相矛盾输出），其余两个值时清单一律置空。
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None, "输出不是合法 JSON。"
    if not isinstance(data, dict):
        return None, "输出必须是 JSON 对象。"
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in SCOPE_VERDICT_ENUM:
        return None, f"verdict 必须是 {' 或 '.join(SCOPE_VERDICT_ENUM)} 之一。"
    segments_raw = data.get("out_of_scope_segments")
    if segments_raw is None:
        segments_raw = []
    if not isinstance(segments_raw, list):
        return None, "out_of_scope_segments 必须是段落数组（无越界时为空数组）。"
    segments: list[dict] = []
    for idx, item in enumerate(segments_raw):
        seg, reason = _parse_segment(item, idx)
        if seg is None:
            return None, reason
        segments.append(seg)
    if verdict == OUT_OF_SCOPE and not segments:
        return None, (
            "verdict 为 out_of_scope 时 out_of_scope_segments 不能为空：请给出具体越界段落。"
        )
    if verdict != OUT_OF_SCOPE:
        segments = []
    return {"verdict": verdict, "out_of_scope_segments": segments}, ""


async def judge_correctness(
    model,
    system_prompt: str,
    history: list,
    user_text: str,
    diff_text: str,
    max_retries: int,
) -> dict | None:
    """执行一次正确性裁决；重试耗尽仍失败返回 None（调用方标注「核验未完成」）。

    输入契约（工单 0028 验收）：用户原话 + 最近若干轮上下文 + 代码算好的 diff。
    输入禁区：任何智能体自述——history 只保留用户侧消息（AI 消息承载的是轮次
    产物结论回放，属于自述），summary / intent 申报 / 申报改动文件 / user_goal
    一概不进入本调用的任何消息。

    失败语义（工单 0028 验收）：网络/超时等异常复用既有重试次数与退避语义
    （max_retries 次机会 + 线性退避，公式同 run_generation）；非法输出回喂错误
    文案修正一次；全部失败返回 None——路由层据此标注 unverified，快照照建。
    """
    # JSON Mode：真实 ChatOpenAI 经 bind 强制 json_object；伪模型无 bind 则原样调用
    caller = (
        model.bind(response_format={"type": "json_object"})
        if hasattr(model, "bind")
        else model
    )
    # 裁判不得看见智能体自述：上下文只留用户侧消息
    context = [m for m in history if isinstance(m, HumanMessage)]
    messages = [
        SystemMessage(content=system_prompt),
        *context,
        HumanMessage(content=f"用户最新原话：{user_text}"),
        HumanMessage(
            content=(
                "本轮磁盘真实改动的 diff（行号已由系统算好：hunk 头 @@ -a,b +c,d @@ "
                "即行号定位，直接引用，不要自己数行）：\n"
                f"{diff_text or '（无可解码的文本改动）'}"
            )
        ),
    ]
    refeeds_left = _MAX_REFEEDS
    attempts_left = max_retries + 1  # 首次 + 网络失败重试，语义同 run_generation
    while attempts_left > 0:
        attempts_left -= 1
        try:
            msg = await caller.ainvoke(messages)
        except Exception:  # noqa: BLE001 — 网络/超时：按既有退避语义重试，绝不向上炸流
            if attempts_left <= 0:
                return None
            # 退避公式与 run_generation 逐字同语义（loop.py）：0.5s、1.0s、1.5s…
            # 线性递增——第 n 次失败（0 基）睡 RETRY_BACKOFF_SECONDS * (n + 1)
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * (max_retries - attempts_left + 1))
            continue
        # 推理模型会把思考混进 content：解析前先洗掉行内思考块（房规同循环收尾）
        parsed, reason = parse_scope_verdict_payload(
            strip_think_blocks(getattr(msg, "content", "") or "")
        )
        if parsed is not None:
            return parsed
        if refeeds_left <= 0:
            return None
        refeeds_left -= 1
        messages = [
            *messages,
            msg,
            HumanMessage(
                content=(
                    f"裁决输出不合法：{reason} "
                    "请只输出一个符合约定 schema 的 JSON 对象（verdict / "
                    "out_of_scope_segments），不要输出任何其他文字。"
                )
            ),
        ]
    return None
