"""ORM 模型。术语定义见仓库根目录 CONTEXT.md。"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Project(Base):
    """项目：用户创建的应用构建单元（见 CONTEXT.md）。"""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # engineer | team（team 模式在后续工单交付）
    mode: Mapped[str] = mapped_column(String(16), default="engineer", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ProjectFile(Base):
    """生成文件索引：内容以磁盘为准，此表为索引。"""

    __tablename__ = "project_files"
    __table_args__ = (UniqueConstraint("project_id", "path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True, nullable=False)
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    size: Mapped[int] = mapped_column(default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Publication(Base):
    """发布记录：项目当前版本的稳定公开链接（见 CONTEXT.md）。

    project_id 唯一约束保证同一项目至多一条活跃发布记录（工单 0006）。
    """

    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), unique=True, index=True, nullable=False
    )
    slug: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    # 官方示例标记（工单 0012）：由系统自身链路生成并发布的画廊冷启动示例；
    # 存量旧库缺列时由灌入脚本自动补列（见 official_samples._ensure_official_column）
    official: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Snapshot(Base):
    """版本快照：每次成功生成后项目全部文件的完整留档（工单 0007）。

    文件本体以磁盘为准（{storage_root}/projects/{id}/snapshots/{rev}/），
    此表只存索引；rev 在单个项目内从 1 递增。
    """

    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("project_id", "rev"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True, nullable=False)
    rev: Mapped[int] = mapped_column(nullable=False)
    file_count: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Ticket(Base):
    """工单（工单 0017 / ADR 0003）：团队模式规格确认后的纵向切片拆解。

    每个工单是可独立预览的完整路径；blocked_by 为阻塞依赖的工单序号列表（JSON，落库时已换算为续编后的 seq）。
    清单待确认时重新拆解：旧清单整批删除、新清单取代（序号在历史最大值上续编）；
    一经确认进入执行期，不可重新拆单。
    """

    __tablename__ = "tickets"
    __table_args__ = (UniqueConstraint("project_id", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True, nullable=False)
    seq: Mapped[int] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    deliverable: Mapped[str] = mapped_column(Text, nullable=False)
    blocked_by: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    # open（待执行）| running（执行中）| done（已完成）| failed（失败待重试）；
    # 执行状态迁移与串行执行见工单 0018（ADR 0003 检查点执行）
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    # 检查点快照引用（工单 0018）：工单完成时形成的快照，回看/回滚入口；
    # 存量旧库缺列时由 db.ensure_schema 自动补列（同 official 列先例）
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("snapshots.id"), nullable=True)
    # 检查点快照对象；快照行被超限清理删除时置 None（passive_deletes，工单 0018）
    snapshot: Mapped[Snapshot | None] = relationship(passive_deletes=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Message(Base):
    """对话历史：含智能体文本与工具事件（kind=event 时 content 为 JSON）。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True, nullable=False)
    # user | pm | engineer | clarifier | spec_agent | breaker_agent | system
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # text | prd | prd_confirm | consensus | consensus_confirm | spec | spec_confirm
    # | tickets | tickets_confirm | ticket | event | thinking
    # （prd/prd_confirm 为团队模式，工单 0010；consensus/consensus_confirm 为需求澄清，工单 0015；
    # spec/spec_confirm 为需求规格，工单 0016；tickets/tickets_confirm 为工单清单，工单 0017；
    # ticket 为单张工单的执行进度行，工单 0018；
    # clarify_answer 为弹窗式澄清的答案消息标记，工单 0020）
    kind: Mapped[str] = mapped_column(String(16), default="text", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
