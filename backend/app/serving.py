"""项目文件对外提供的共享原语：沙箱路径解析 + MIME 映射。

预览托管（工单 0005，属主登录态）与公开托管（工单 0006，匿名 /p/{slug}）
共用同一套越界防护与内容类型判定，避免两套口径漂移。
"""

from pathlib import Path

from fastapi import HTTPException, Response, status

from .agent.tools import SandboxViolation, resolve_sandboxed

MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


def serve_project_file(root: Path, rel_path: str) -> Response:
    """按真实 MIME 类型提供项目目录内的单个文件。

    越界路径、白名单外扩展名与缺失文件一律 404。
    """
    try:
        target = resolve_sandboxed(root, rel_path or "index.html")
    except SandboxViolation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    if not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
    media_type = MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
    return Response(content=target.read_bytes(), media_type=media_type)
