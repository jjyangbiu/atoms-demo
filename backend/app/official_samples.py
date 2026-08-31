"""官方示例灌入（工单 0012）：画廊冷启动。

用系统自身链路生成并发布一组官方示例应用，标记"官方示例"进入 App 世界画廊，
供浏览与克隆演示。灌入走与真实用户完全相同的 HTTP 链路（建项目 → 首轮生成 →
一轮迭代 → 发布），因此同时是对全链路（生成 → 迭代 → 发布 → 画廊）的真实回归。

可重复执行：已灌入的示例直接跳过；生成成功但未发布的中断现场会被补齐发布
（不重复迭代）；生成失败的示例记录原因、不影响其余示例。入口见
`scripts/seed_official_samples.py`。
"""

import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, text, update

from .models import Project, Publication, User
from .security import create_access_token, hash_password

# 官方示例的属主账号：只用于署名，随机口令使无人可登录
OFFICIAL_USERNAME = "atoms_official"


@dataclass(frozen=True)
class SampleSpec:
    """一个官方示例：项目名 + 首次诉求（画廊描述即取自这段诉求）+ 迭代诉求。

    迭代诉求让每个示例真实走过对话迭代链路（工单 0012 验收项 3 的全链路回归）。
    """

    name: str
    ask: str
    iterate: str


# 画廊冷启动示例（待办清单 / 数据仪表盘 / 小游戏 / 落地页）。
# 诉求写明功能；统一的自包含约束放在末尾：真实生成实测发现模型会自发引入
# 外部 CDN（tailwind/字体），必须显式禁止才能保证公开链接离线可运行。
_NO_EXTERNAL_DEPS = (
    "严格约束：所有 HTML/CSS/JS 全部内联，不得使用或引用任何外部资源，"
    "包括但不限于 CDN、字体、图标库、图片链接；界面文字用中文。"
)

OFFICIAL_SAMPLE_SPECS = [
    SampleSpec(
        name="待办清单",
        ask=(
            "做一个待办清单单页应用：可以添加待办、勾选完成、删除条目，"
            "顶部显示未完成数量并提供 全部/未完成/已完成 三个筛选；"
            "数据存 localStorage，刷新后自动恢复。现代简洁的界面。"
        )
        + _NO_EXTERNAL_DEPS,
        iterate="微调一下：把主标题改得更醒目，并在输入框下方加一句简短的使用说明。",
    ),
    SampleSpec(
        name="数据仪表盘",
        ask=(
            "做一个数据仪表盘单页应用：顶部一排统计卡片（今日访问量、新增用户、"
            "订单金额、转化率），下方用内联 SVG 或 Canvas 手绘最近 7 天访问量"
            "折线图与渠道占比条形图。深色科技风配色。"
        )
        + _NO_EXTERNAL_DEPS,
        iterate="微调一下：在统计卡片下方加一行小字注明数据更新时间，其余保持不变。",
    ),
    SampleSpec(
        name="贪吃蛇小游戏",
        ask=(
            "做一个贪吃蛇小游戏单页应用：方向键控制移动，吃到食物变长加分，"
            "撞墙或撞到自己则结束；显示当前分与最高分（localStorage 持久化），"
            "提供开始/暂停与重新开始按钮。Canvas 绘制。"
        )
        + _NO_EXTERNAL_DEPS,
        iterate="微调一下：在游戏区下方加一句操作方式说明（方向键控制）。",
    ),
    SampleSpec(
        name="产品落地页",
        ask=(
            "做一个 SaaS 产品落地页单页应用：顶部导航、首屏大标题与行动按钮、"
            "三到四张功能亮点卡片、三档定价方案、页脚版权；导航支持锚点平滑滚动，"
            "带克制的入场动画。现代渐变配色。"
        )
        + _NO_EXTERNAL_DEPS,
        iterate="微调一下：首屏大标题改得更醒目，并配一句简短的产品标语。",
    ),
]


@dataclass
class SeedResult:
    """单个示例的灌入结果：seeded=本轮生成/补齐发布，skipped=已存在，failed=失败。"""

    name: str
    status: str
    slug: str | None = None
    error: str | None = None


def seed_official_samples(app, client) -> list[SeedResult]:
    """按规格逐个灌入官方示例。

    `client` 需是架在 `app` 上的 TestClient：灌入经真实 HTTP 路由走完整链路；
    生产跑真实模型，测试可经 app.state.model_factory 注入伪模型。
    """
    _ensure_official_column(app.state.engine)
    session_factory = app.state.session_factory
    settings = app.state.settings
    user = _ensure_official_user(session_factory)
    headers = {
        "Authorization": f"Bearer {create_access_token(settings, user.id, user.username)}"
    }
    return [
        _seed_one(app, client, session_factory, user, headers, spec)
        for spec in OFFICIAL_SAMPLE_SPECS
    ]


def _ensure_official_column(engine) -> None:
    """旧库的 publications 表缺 official 列（create_all 不改存量表）：自动补列。

    项目没有迁移框架，且只支持 SQLite；新环境建库由 create_all 一步到位。
    """
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(publications)"))}
    if "official" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE publications ADD COLUMN official BOOLEAN NOT NULL DEFAULT 0")
            )


def _ensure_official_user(session_factory) -> User:
    """官方账号只用于署名：随机口令使无人可登录，重跑时复用同一账号。"""
    with session_factory() as session:
        user = session.scalar(select(User).where(User.username == OFFICIAL_USERNAME))
        if user is None:
            user = User(
                username=OFFICIAL_USERNAME, password_hash=hash_password(secrets.token_urlsafe(32))
            )
            session.add(user)
            session.commit()
            session.refresh(user)
        return user


def _generation_ok_events(response) -> tuple[bool, str | None]:
    """解析生成 SSE 流：返回 (是否收到 done, 首个 error 事件的原因)。"""
    done = False
    error: str | None = None
    for line in response.iter_lines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line.removeprefix("data: "))
        if event.get("type") == "error" and error is None:
            error = event.get("detail") or "未知错误"
        elif event.get("type") == "done":
            done = True
    return done, error


def _run_generation(client, headers: dict, project_id: int, content: str) -> str | None:
    """经真实对话链路发一轮生成（SSE）；成功返回 None，失败返回原因。"""
    try:
        with client.stream(
            "POST",
            f"/api/projects/{project_id}/messages",
            json={"content": content},
            headers=headers,
        ) as stream:
            if stream.status_code != 200:
                return f"生成请求失败: HTTP {stream.status_code}"
            done, error = _generation_ok_events(stream)
    except Exception as e:  # noqa: BLE001 — 单个示例失败记录后继续灌入其余
        return f"生成异常: {e}"
    if error is not None:
        return error
    if not done:
        return "生成流结束但没有 done 事件"
    return None


def _seed_one(app, client, session_factory, user: User, headers: dict, spec: SampleSpec) -> SeedResult:
    """灌入单个示例：跳过已发布、补齐未发布、必要时整段重建。"""
    settings = app.state.settings
    with session_factory() as session:
        project = session.scalar(
            select(Project).where(Project.user_id == user.id, Project.name == spec.name)
        )
        publication = (
            session.scalar(select(Publication).where(Publication.project_id == project.id))
            if project is not None
            else None
        )
        if publication is not None:
            if not publication.official:
                publication.official = True
                session.commit()
            return SeedResult(spec.name, "skipped", slug=publication.slug)

    if project is not None:
        # 中断现场：上次没走到发布。有可发布内容就补齐；否则删掉重来，
        # 避免在半成品（如生成失败的空项目）上发布报 400 后永远卡住。
        # 存储布局与 routers/projects.project_dir 一致（后者依赖 Request 无法复用）
        has_index = (
            Path(settings.storage_root) / "projects" / str(project.id) / "index.html"
        ).is_file()
        if not has_index:
            client.delete(f"/api/projects/{project.id}", headers=headers)
            project = None

    if project is None:
        resp = client.post("/api/projects", json={"name": spec.name}, headers=headers)
        if resp.status_code != 201:
            return SeedResult(spec.name, "failed", error=f"创建项目失败: HTTP {resp.status_code}")
        project_id = resp.json()["id"]

        error = _run_generation(client, headers, project_id, spec.ask)
        if error is not None:
            return SeedResult(spec.name, "failed", error=error)
        # 再走一轮对话迭代：全链路回归含迭代环节；失败则不发布，重跑可重建
        error = _run_generation(client, headers, project_id, spec.iterate)
        if error is not None:
            return SeedResult(spec.name, "failed", error=f"迭代失败: {error}")
    else:
        project_id = project.id

    # 发布接口幂等：已发布返回同一链接；随后标记为官方示例
    resp = client.post(f"/api/projects/{project_id}/publish", headers=headers)
    if resp.status_code not in (200, 201):
        return SeedResult(spec.name, "failed", error=f"发布失败: HTTP {resp.status_code}")
    slug = resp.json()["slug"]
    # “可打开运行”验收：发布后立即匿名访问公开页，非 200 计为灌入失败
    page = client.get(f"/p/{slug}")
    if page.status_code != 200:
        return SeedResult(
            spec.name, "failed", slug=slug, error=f"发布页打不开: HTTP {page.status_code}"
        )
    # 直接 UPDATE 标记官方示例，不取行改属性，避免跨会话对象状态干扰
    with session_factory() as session:
        session.execute(
            update(Publication).where(Publication.slug == slug).values(official=True)
        )
        session.commit()
    return SeedResult(spec.name, "seeded", slug=slug)
