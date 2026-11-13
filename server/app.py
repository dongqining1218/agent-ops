"""HTTP 服务：事件接入、长轮询派单、结论回写、观察页。

接口（P0）：
  POST /events            接入一条事件（统一格式 + 去重 + 匹配规则 + 建派工）
  POST /lease             worker 长轮询领活（默认挂 50s）
  POST /conclude          worker 回写结论（幂等）
  GET  /health            健康快照
  GET  /api/runs          最近的运行
  GET  /api/inbox         待我决定
  GET  /api/events        最近的事件
  GET  /                  仪表盘（HTML）
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, Form, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import views
from .config import Config, load_config
from . import memory
from .db import ConflictError, Store, iso, utc_now
from .rules import Rule, load_rules, match_rule

log = logging.getLogger("agent-ops")

SWEEP_INTERVAL_SECONDS = 5
LEASE_POLL_INTERVAL_SECONDS = 0.3


# ---------- 请求模型 ----------


class EventIn(BaseModel):
    source: str = Field(..., min_length=1, description="谁发来的，如 ticker / github / feishu")
    type: str = Field(..., min_length=1, description="什么类型，如 repo.daily_check")
    subject: str = ""
    summary: str = ""
    link: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = Field(
        None, description="不传则由 source|type|subject|summary 推导"
    )


class LeaseIn(BaseModel):
    agent: str = Field(..., min_length=1)
    ttl: Optional[int] = Field(None, ge=0, le=600, description="长轮询挂起秒数")


class DecisionIn(BaseModel):
    choice: str = Field(..., min_length=1, description="人做了什么决定，如 approve / reject / 具体选项")
    note: str = ""
    decided_by: str = "me"


class ConcludeIn(BaseModel):
    run_id: int
    status: Literal["completed", "failed", "need_decision"]
    summary: str = ""
    session_ref: Optional[str] = None
    artifacts: Optional[List[str]] = None
    report: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ---------- 工具 ----------


def _derive_key(source: str, event_type: str, subject: str, summary: str) -> str:
    raw = "|".join([source, event_type, subject, summary])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _now() -> str:
    return iso(utc_now())


def _health(store: Store, cfg: Config) -> Dict[str, Any]:
    counts = store.counts()
    expired = store.expired_lease_count()
    return {
        "ok": expired == 0,
        "now": _now(),
        "db": str(cfg.db_path),
        "counts": counts,
        "expired_leases": expired,
    }


# ---------- 应用 ----------


def create_app(cfg: Optional[Config] = None) -> FastAPI:
    cfg = cfg or load_config()
    store = Store(
        cfg.db_path,
        max_attempts=cfg.max_attempts,
        retry_base_seconds=cfg.retry_base_seconds,
        retry_max_seconds=cfg.retry_max_seconds,
    )
    rules_holder: Dict[str, List[Rule]] = {"rules": []}

    def reload_rules() -> List[Rule]:
        rules_holder["rules"] = load_rules(cfg.rules_file)
        return rules_holder["rules"]

    async def _sweeper() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                n = store.sweep_expired_leases()
                if n:
                    log.warning("回收了 %d 个过期租约", n)
            except Exception:  # noqa: BLE001 - 后台任务不能死
                log.exception("回收过期租约失败")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        store.init()
        reload_rules()
        log.info("Agent Ops 启动：db=%s rules=%d", cfg.db_path, len(rules_holder["rules"]))
        task = asyncio.create_task(_sweeper())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Agent Ops", version="0.1.0", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.store = store
    app.state.rules_holder = rules_holder

    # ---------- 事件接入 ----------

    def ingest(
        source: str,
        event_type: str,
        subject: str,
        summary: str,
        link: Optional[str],
        payload: Optional[Dict[str, Any]],
        idem: Optional[str] = None,
    ) -> Dict[str, Any]:
        """一条事件进中台：去重 → 匹配规则 → 建派工。事件源和「人做了决定」都走这里。"""
        idem = idem or _derive_key(source, event_type, subject, summary)
        rule = match_rule(rules_holder["rules"], source, event_type)

        event_id, created = store.add_event(
            idem_key=idem,
            source=source,
            event_type=event_type,
            subject=subject,
            summary=summary,
            link=link,
            payload=payload,
            status="queued" if rule else "recorded",
            rule=rule.name if rule else None,
        )
        if not created:
            event = store.get_event(event_id)
            log.info("事件重复，忽略：idem=%s event=%s", idem, event_id)
            return {
                "event_id": event_id,
                "duplicate": True,
                "status": event["status"] if event else "unknown",
                "run_id": None,
                "rule": event.get("rule") if event else None,
            }

        if rule is None:
            log.info("事件无匹配规则，只记录：#%s %s/%s", event_id, source, event_type)
            return {
                "event_id": event_id,
                "duplicate": False,
                "status": "recorded",
                "run_id": None,
                "rule": None,
            }

        run_id = store.create_run(event_id, rule)
        log.info("已入队：event=#%s run=#%s rule=%s", event_id, run_id, rule.name)
        return {
            "event_id": event_id,
            "duplicate": False,
            "status": "queued",
            "run_id": run_id,
            "rule": rule.name,
        }

    @app.post("/events")
    async def post_event(body: EventIn) -> Dict[str, Any]:
        return ingest(
            source=body.source,
            event_type=body.type,
            subject=body.subject,
            summary=body.summary,
            link=body.link,
            payload=body.payload,
            idem=body.idempotency_key,
        )

    # ---------- 批奏折：人做决定 ----------

    def decide_run(run_id: int, choice: str, note: str, decided_by: str) -> Dict[str, Any]:
        decision = store.decide(run_id, choice, note=note, decided_by=decided_by)
        memory_file = memory.append_decision(
            cfg.memory_dir,
            question=decision["question"],
            choice=choice,
            note=note,
            run_id=run_id,
            source=decision["source"],
        )
        question = decision["question"]
        followup = ingest(
            source=decision["source"],
            event_type=decision["type"],
            subject=decision["subject"],
            summary="已决定：%s（原问题：%s）" % (choice, question[:120]),
            link=None,
            payload={
                "decision": {
                    "choice": choice,
                    "note": note,
                    "question": question,
                    "decided_at": decision["decided_at"],
                },
                "prior_run": run_id,
            },
            idem="decision:%s" % run_id,
        )
        log.info(
            "决策：run=#%s choice=%s → 后续事件 #%s", run_id, choice, followup.get("event_id")
        )
        return {"decision": decision, "memory_file": memory_file, "followup": followup}

    @app.post("/api/runs/{run_id}/decide")
    async def api_decide(run_id: int, body: DecisionIn) -> Dict[str, Any]:
        try:
            return decide_run(run_id, body.choice, body.note, body.decided_by)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/ui/decide")
    async def ui_decide(
        run_id: int = Form(...),
        choice: str = Form(...),
        note: str = Form(""),
    ) -> Response:
        try:
            decide_run(run_id, choice, note, "me")
        except (KeyError, ConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return RedirectResponse("/ui/inbox", status_code=303)

    @app.get("/api/decisions")
    async def api_decisions(limit: int = Query(50, ge=1, le=500)) -> List[Dict[str, Any]]:
        return store.recent_decisions(limit)

    # ---------- 派单（长轮询） ----------

    @app.post("/lease")
    async def lease(
        body: LeaseIn,
        x_agent_token: Optional[str] = Header(default=None, alias="X-Agent-Token"),
    ) -> Response:
        expected = cfg.tokens.get(body.agent)
        if expected is not None and x_agent_token != expected:
            raise HTTPException(status_code=401, detail="agent token 不匹配")

        ttl = body.ttl if body.ttl is not None else cfg.lease_ttl_seconds
        ttl = min(ttl, cfg.lease_max_ttl_seconds)
        deadline = asyncio.get_event_loop().time() + ttl

        while True:
            run = store.lease(body.agent, cfg.lease_ttl_seconds)
            if run is not None:
                log.info("派单：run=#%s agent=%s attempts=%s", run["id"], body.agent, run["attempts"])
                return JSONResponse(run)
            if asyncio.get_event_loop().time() >= deadline:
                return Response(status_code=204)
            await asyncio.sleep(LEASE_POLL_INTERVAL_SECONDS)

    # ---------- 结论回写 ----------

    @app.post("/conclude")
    async def conclude(
        body: ConcludeIn,
        x_agent_token: Optional[str] = Header(default=None, alias="X-Agent-Token"),
    ) -> Dict[str, Any]:
        cfg_tokens = cfg.tokens
        if cfg_tokens:
            allowed = {v for v in cfg_tokens.values()}
            if x_agent_token not in allowed:
                raise HTTPException(status_code=401, detail="agent token 不匹配")
        try:
            result = store.conclude(
                run_id=body.run_id,
                status=body.status,
                summary=body.summary,
                session_ref=body.session_ref,
                artifacts=body.artifacts,
                report=body.report,
                error=body.error,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        log.info(
            "结论：run=#%s status=%s retried=%s", body.run_id, result["status"], result["retried"]
        )
        return result

    # ---------- 观察 ----------

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return _health(store, cfg)

    @app.get("/api/runs")
    async def api_runs(limit: int = Query(50, ge=1, le=500)) -> List[Dict[str, Any]]:
        return store.recent_runs(limit)

    @app.get("/api/runs/{run_id}")
    async def api_run(run_id: int) -> Dict[str, Any]:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run 不存在")
        return run

    @app.get("/api/events")
    async def api_events(limit: int = Query(50, ge=1, le=500)) -> List[Dict[str, Any]]:
        return store.recent_events(limit)

    @app.get("/api/inbox")
    async def api_inbox() -> List[Dict[str, Any]]:
        return store.inbox()

    @app.get("/api/rules")
    async def api_rules() -> Dict[str, Any]:
        names = [r.name for r in rules_holder["rules"]]
        return {"file": str(cfg.rules_file), "rules": names}

    @app.post("/api/rules/reload")
    async def api_reload_rules() -> Dict[str, Any]:
        names = [r.name for r in reload_rules()]
        return {"reloaded": True, "rules": names}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        counts = store.counts()
        return views.render_dashboard(
            counts=counts,
            runs=store.recent_runs(30),
            inbox=store.inbox(),
            health=_health(store, cfg),
            rules=[r.name for r in rules_holder["rules"]],
        )

    @app.get("/ui/inbox", response_class=HTMLResponse)
    async def inbox_page() -> str:
        return views.render_inbox(store.inbox())

    return app


app = create_app()
