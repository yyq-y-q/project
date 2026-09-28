"""
统一 CLI：mykb

  mykb rag-rebuild
  mykb rag-smoke
  mykb sqlite-ingest
  mykb sqlite-smoke
  mykb hybrid -q "..." [--sql "..."]
  mykb smoke
  mykb serve
  mykb agent -t "..."
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _dump(out) -> None:
    click.echo(json.dumps(out, ensure_ascii=False, indent=2, default=str))


def _fail(exc: BaseException) -> None:
    """CLI 统一出口：AppError / 未预期 → 同一 JSON 形，exit 1。"""
    from app.errors import AppError, internal_from_exception
    from app.logging_setup import get_logger

    log = get_logger("app.cli")
    if isinstance(exc, AppError):
        log.warning("cli AppError code=%s stage=%s msg=%s", exc.code, exc.stage, exc.message)
        _dump(exc.to_dict())
        sys.exit(1)

    log.exception("cli unhandled: %s", type(exc).__name__)
    wrapped = internal_from_exception(exc, stage="cli")
    _dump(wrapped.to_dict())
    sys.exit(1)


@click.group()
def main():
    """Private Knowledge Base 命令行。"""
    pass


@main.command("rag-rebuild")
def rag_rebuild():
    """全量重建 RAG 索引（data/raw 叙述文档）。"""
    from app.logging_setup import setup_logging
    from app.services import KnowledgeService

    setup_logging()
    try:
        out = KnowledgeService().rebuild()
        _dump(out)
        sys.exit(0)
    except Exception as e:
        _fail(e)


@main.command("rag-smoke")
@click.option("--load-only", is_flag=True)
def rag_smoke(load_only: bool):
    """RAG 冒烟。"""
    from app.logging_setup import setup_logging
    from app.services import KnowledgeService

    setup_logging()
    svc = KnowledgeService()
    try:
        if not load_only:
            built = svc.rebuild()
            _dump(built)
        r = svc.ask("什么是RAG？", session_id="smoke", use_memory=False)
        _dump(r)
        ok = r.get("ok") and r.get("sources") and "引用" in str(r.get("answer"))
        sys.exit(0 if ok else 2)
    except Exception as e:
        _fail(e)


@main.command("sqlite-ingest")
@click.option("--dir", "raw_dir", default=None, help="默认 data/raw")
def sqlite_ingest(raw_dir):
    """一键 ingest 目录下 xlsx/csv。"""
    from app.logging_setup import setup_logging
    from app.services import TableService

    setup_logging()
    try:
        out = TableService().ingest_all(raw_dir)
        _dump(out)
        sys.exit(0)
    except Exception as e:
        _fail(e)


@main.command("sqlite-smoke")
def sqlite_smoke():
    """固定 SELECT 冒烟。"""
    from app.logging_setup import setup_logging
    from app.services import TableService

    setup_logging()
    try:
        out = TableService().query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        _dump(out)
        out2 = TableService().query("SELECT id, name, dept FROM employees LIMIT 5")
        _dump(out2)
        sys.exit(0)
    except Exception as e:
        _fail(e)


@main.command("hybrid")
@click.option("--question", "-q", required=True, help="用户问题")
@click.option("--sql", default=None, help="可选显式 SELECT")
@click.option("--session", default="hybrid")
@click.option("--no-rag", is_flag=True, help="关闭知识库分支")
@click.option("--no-sql", is_flag=True, help="关闭表查询分支")
@click.option("--no-auto-sql", is_flag=True, help="未给 --sql 时也不自动生成")
def hybrid_cmd(question, sql, session, no_rag, no_sql, no_auto_sql):
    """确定性混合问答：RAG + SQL → 一篇答案。"""
    from app.hybrid import HybridService
    from app.logging_setup import setup_logging

    setup_logging()
    try:
        out = HybridService().ask(
            question,
            sql=sql,
            session_id=session,
            use_rag=not no_rag,
            use_sql=not no_sql,
            auto_sql=not no_auto_sql,
            use_memory=False,
        )
        _dump(out)
        sys.exit(0)
    except Exception as e:
        _fail(e)


@main.command("smoke")
def smoke_all():
    """rag-rebuild + sqlite-ingest + 两边 smoke + hybrid 结构检查。"""
    from app.hybrid import HybridService
    from app.logging_setup import setup_logging
    from app.services import KnowledgeService, TableService

    setup_logging()
    try:
        b = KnowledgeService().rebuild()
        click.echo(b)
        ing = TableService().ingest_all()
        click.echo(ing)
        a = KnowledgeService().ask("什么是RAG？", session_id="smoke", use_memory=False)
        click.echo(a.get("answer"))
        q = TableService().query("SELECT COUNT(*) AS n FROM employees")
        click.echo(q)
        h = HybridService().ask(
            "员工表有多少人？顺便说明什么是 RAG",
            sql="SELECT COUNT(*) AS n FROM employees",
            session_id="smoke-hybrid",
            auto_sql=False,
            use_memory=False,
        )
        click.echo(h.get("mode"))
        click.echo(h.get("answer"))
        ok = a.get("ok") and q.get("ok") and h.get("ok")
        click.echo("ALL SMOKE OK" if ok else "SMOKE FAILED")
        sys.exit(0 if ok else 2)
    except Exception as e:
        _fail(e)


@main.command("serve")
@click.option("--host", default=None, help="默认 KB_HOST 或 0.0.0.0")
@click.option("--port", default=None, type=int, help="默认 KB_PORT 或 8000")
@click.option("--workers", default=None, type=int, help="默认 KB_WORKERS 或 1")
def serve(host, port, workers):
    """启动 FastAPI + UI（生产请前置 nginx HTTPS）。"""
    import uvicorn
    from app.logging_setup import setup_logging
    from app.settings import get_settings

    settings = get_settings()
    setup_logging()
    h = host or settings.host
    p = port if port is not None else settings.port
    w = workers if workers is not None else settings.workers
    # 多 worker 必须 import string；包经 uv sync 装成顶层 app
    if w > 1:
        uvicorn.run("app.api:app", host=h, port=p, workers=w, reload=False)
    else:
        from app.api import app as fastapi_app

        uvicorn.run(fastapi_app, host=h, port=p, reload=False)


@main.command("check-deploy")
def check_deploy():
    """部署前自检：鉴权/密钥/目录/索引，不启动服务。"""
    from app.logging_setup import setup_logging
    from app.services import HealthService
    from app.settings import get_settings

    setup_logging()
    st = get_settings()
    out = HealthService().status(deep=False)
    out["settings"] = {
        "env": st.env,
        "require_api_key": st.require_api_key,
        "host": st.host,
        "port": st.port,
        "workers": st.workers,
    }
    _dump(out)
    sys.exit(0 if out.get("ready") else 2)


@main.command("temp-cleanup")
def temp_cleanup():
    """删除到期临时文件并追加系统审计记录。"""
    import uuid
    from app.temp_file_service import cleanup_expired

    try:
        deleted = cleanup_expired(
            request_id=f"cli_{uuid.uuid4().hex}",
            source_ip="local",
        )
        _dump({"ok": True, "deleted": deleted})
        sys.exit(0)
    except Exception as e:
        _fail(e)


@main.command("audit-verify")
def audit_verify():
    """校验不可变审计哈希链。"""
    from app.governance import verify_audit_chain

    try:
        result = verify_audit_chain()
        _dump(result)
        sys.exit(0 if result.get("ok") else 2)
    except Exception as e:
        _fail(e)


@main.command("agent")
@click.option("--task", "-t", required=True)
@click.option("--work-dir", default=None)
@click.option("--session", default="cli")
def agent_cmd(task, work_dir, session):
    from app.logging_setup import setup_logging
    from app.services import AgentService

    setup_logging()
    try:
        out = AgentService().run(task, work_dir=work_dir, session_id=session)
        _dump(out)
        sys.exit(0)
    except Exception as e:
        _fail(e)


@main.command("chat")
@click.option("--message", "-m", default="", help="用户消息；也可管道/粘贴")
@click.option("--session", default="cli-chat")
@click.option("--mode", "force_mode", default=None, help="强制 sql|rag|hybrid|agent|chat")
@click.option("--file", "files", multiple=True, type=click.Path(exists=True, dir_okay=False))
def chat_cmd(message, session, force_mode, files):
    """统一对话：自动分流 + 可选本地附件。"""
    from app.chat_service import ChatService
    from app.logging_setup import setup_logging

    setup_logging()
    try:
        pairs = []
        for fp in files:
            p = Path(fp)
            pairs.append((p.name, p.open("rb")))
        text = message
        if not text and not pairs and not sys.stdin.isatty():
            text = sys.stdin.read()
        out = ChatService().handle(
            text,
            session_id=session,
            force_mode=force_mode,
            files=pairs or None,
        )
        for _, fh in pairs:
            try:
                fh.close()
            except Exception:
                pass
        _dump(out)
        sys.exit(0)
    except Exception as e:
        _fail(e)


if __name__ == "__main__":
    main()
