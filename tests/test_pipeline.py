"""P0 流水线的单元测试：去重、无规则、派单、结论、重试、租约回收、幂等。

跑法：cd agent-ops && python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from server.app import create_app  # noqa: E402
from server.config import load_config  # noqa: E402
from worker import mock_adapter  # noqa: E402
from worker.codex_adapter import _parse_report, build_prompt  # noqa: E402

TOKEN = "test-token"


def make_env(root: Path) -> Path:
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "skills" / "demo" / "SKILL.md").write_text("# demo\n按这个办。", encoding="utf-8")
    (root / "rules.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "name": "演示规则",
                        "when": {"source": ["test"], "types": ["demo"]},
                        "to": {
                            "agent": "t",
                            "cwd": str(root),
                            "skill": "demo",
                            "sandbox": "read-only",
                        },
                        "ask": "演示一下",
                        "group": "g",
                        "parallel": 1,
                        "tell": "needed",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config = {
        "host": "127.0.0.1",
        "port": 0,
        "data_dir": str(root / "data"),
        "rules_file": str(root / "rules.json"),
        "report_schema_file": str(PROJECT_ROOT / "schemas" / "report.schema.json"),
        "max_attempts": 3,
        "retry_base_seconds": 0,
        "retry_max_seconds": 0,
        "lease_ttl_seconds": 50,
        "tokens": {"t": TOKEN},
    }
    path = root / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cfg = load_config(str(make_env(self.root)))
        self.app = create_app(cfg)
        self.client_ctx = TestClient(self.app)
        self.client = self.client_ctx.__enter__()
        self.store = self.app.state.store

    def tearDown(self) -> None:
        self.client_ctx.__exit__(None, None, None)
        self.tmp.cleanup()

    # ---------- helpers ----------

    def post_event(self, **overrides):
        body = {
            "source": "test",
            "type": "demo",
            "subject": "s1",
            "summary": "一条测试事件",
            "idempotency_key": "k1",
        }
        body.update(overrides)
        return self.client.post("/events", json=body)

    def lease(self, agent: str = "t", ttl: int = 0):
        return self.client.post(
            "/lease", json={"agent": agent, "ttl": ttl}, headers={"X-Agent-Token": TOKEN}
        )

    # ---------- 测试 ----------

    def test_happy_path(self) -> None:
        res = self.post_event()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertFalse(body["duplicate"])
        self.assertEqual(body["status"], "queued")
        self.assertIsNotNone(body["run_id"])

        leased = self.lease()
        self.assertEqual(leased.status_code, 200)
        run = leased.json()
        self.assertEqual(run["id"], body["run_id"])
        self.assertEqual(run["status"], "leased")
        self.assertEqual(run["attempts"], 1)
        self.assertEqual(run["e_summary"], "一条测试事件")

        done = self.client.post(
            "/conclude",
            json={
                "run_id": run["id"],
                "status": "completed",
                "summary": "做完了",
                "report": {"status": "done", "title": "t", "summary": "做完了", "needs_human": False},
            },
            headers={"X-Agent-Token": TOKEN},
        )
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()["status"], "completed")

        health = self.client.get("/health").json()
        self.assertTrue(health["ok"])
        self.assertEqual(health["counts"]["events"].get("done"), 1)

    def test_duplicate_event_is_ignored(self) -> None:
        first = self.post_event().json()
        second = self.post_event().json()
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertIsNone(second["run_id"])
        self.assertEqual(self.store.counts()["runs"].get("queued"), 1)

    def test_event_without_rule_is_only_recorded(self) -> None:
        res = self.post_event(type="unknown.type")
        body = res.json()
        self.assertEqual(body["status"], "recorded")
        self.assertIsNone(body["run_id"])
        self.assertEqual(self.client.get("/health").json()["counts"]["events"].get("recorded"), 1)

    def test_lease_unauthorized_without_token(self) -> None:
        self.post_event()
        res = self.client.post("/lease", json={"agent": "t", "ttl": 0})
        self.assertEqual(res.status_code, 401)

    def test_same_subject_is_serialized(self) -> None:
        self.post_event(idempotency_key="a")
        self.post_event(idempotency_key="b")  # 同 subject、同 group，只能有一条在跑
        first = self.lease()
        self.assertEqual(first.status_code, 200)
        second = self.lease()
        self.assertEqual(second.status_code, 204)

    def test_failure_retries_then_fails(self) -> None:
        run_id = self.post_event().json()["run_id"]
        for expected_attempt in (1, 2, 3):
            leased = self.lease()
            self.assertEqual(leased.status_code, 200)
            run = leased.json()
            self.assertEqual(run["attempts"], expected_attempt)
            res = self.client.post(
                "/conclude",
                json={"run_id": run["id"], "status": "failed", "error": "炸了"},
                headers={"X-Agent-Token": TOKEN},
            )
            self.assertEqual(res.status_code, 200)
            body = res.json()
            if expected_attempt < 3:
                self.assertTrue(body["retried"])
                self.assertEqual(body["event_status"], "queued")
            else:
                self.assertFalse(body["retried"])
                self.assertEqual(body["event_status"], "failed")
        self.assertEqual(self.lease().status_code, 204)
        self.assertEqual(run_id, run_id)  # 只是为了留住变量，run 是原地重试

    def test_expired_lease_is_recovered(self) -> None:
        run_id = self.post_event().json()["run_id"]
        self.lease()
        conn = self.store.connect()
        try:
            conn.execute(
                "UPDATE runs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?", (run_id,)
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(self.store.expired_lease_count(), 1)
        self.assertEqual(self.store.sweep_expired_leases(), 1)
        self.assertEqual(self.store.expired_lease_count(), 0)

        again = self.lease()
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["attempts"], 2)

    def test_conclude_is_idempotent(self) -> None:
        run_id = self.post_event().json()["run_id"]
        self.lease()
        first = self.client.post(
            "/conclude",
            json={"run_id": run_id, "status": "completed", "summary": "ok"},
            headers={"X-Agent-Token": TOKEN},
        ).json()
        second = self.client.post(
            "/conclude",
            json={"run_id": run_id, "status": "completed", "summary": "又写一遍"},
            headers={"X-Agent-Token": TOKEN},
        ).json()
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])

    def test_need_decision_goes_to_inbox(self) -> None:
        run_id = self.post_event().json()["run_id"]
        self.lease()
        self.client.post(
            "/conclude",
            json={
                "run_id": run_id,
                "status": "need_decision",
                "summary": "要不要删这个分支？",
                "report": {"needs_human": True, "human_question": "删不删？"},
            },
            headers={"X-Agent-Token": TOKEN},
        )
        inbox = self.client.get("/api/inbox").json()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["report"]["human_question"], "删不删？")
        inbox_html = self.client.get("/ui/inbox")
        self.assertEqual(inbox_html.status_code, 200)
        self.assertIn("action=/ui/decide", inbox_html.text)
        self.assertIn("value=approve", inbox_html.text)
        self.assertEqual(self.client.get("/").status_code, 200)

    def _need_decision(self, question: str = "要不要删掉这个分支？", **kwargs):
        body = {"summary": "这件事需要你定", "payload": {"question": question}}
        body.update(kwargs)
        run_id = self.post_event(**body).json()["run_id"]
        self.lease()
        self.client.post(
            "/conclude",
            json={
                "run_id": run_id,
                "status": "need_decision",
                "summary": question,
                "report": {"needs_human": True, "human_question": question},
            },
            headers={"X-Agent-Token": TOKEN},
        )
        return run_id

    def test_decision_loop(self) -> None:
        run_id = self._need_decision()
        self.assertEqual(len(self.client.get("/api/inbox").json()), 1)

        res = self.client.post(
            "/api/runs/%d/decide" % run_id, json={"choice": "approve", "note": "删"}
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["decision"]["choice"], "approve")
        self.assertEqual(body["followup"]["status"], "queued")

        # 决定写进了记忆库
        memory_file = Path(body["memory_file"])
        self.assertTrue(memory_file.exists())
        text = memory_file.read_text(encoding="utf-8")
        self.assertIn("要不要删掉这个分支？", text)
        self.assertIn("approve", text)
        self.assertIn("删", text)

        # 收件箱清空、run 变 decided、原事件结案
        self.assertEqual(self.client.get("/api/inbox").json(), [])
        self.assertEqual(self.store.get_run(run_id)["status"], "decided")
        decisions = self.client.get("/api/decisions").json()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["run_id"], run_id)

        # 后续事件带着决定，能被领走继续干
        follow = self.lease().json()
        self.assertEqual(follow["id"], body["followup"]["run_id"])
        self.assertEqual(follow["e_payload"]["decision"]["choice"], "approve")

        # 同一条不能决策第二次
        again = self.client.post("/api/runs/%d/decide" % run_id, json={"choice": "reject"})
        self.assertEqual(again.status_code, 409)

    def test_decide_rejected_goes_to_rejected_memory(self) -> None:
        run_id = self._need_decision("要不要公开这个仓库？")
        body = self.client.post(
            "/api/runs/%d/decide" % run_id, json={"choice": "reject", "note": "先私有"}
        ).json()
        self.assertIn("定了不做的事", body["memory_file"])
        self.assertTrue(Path(body["memory_file"]).read_text(encoding="utf-8").count("reject") >= 1)

    def test_cannot_decide_a_running_run(self) -> None:
        run_id = self.post_event().json()["run_id"]
        res = self.client.post("/api/runs/%d/decide" % run_id, json={"choice": "approve"})
        self.assertEqual(res.status_code, 409)

    def test_decide_unknown_run_404(self) -> None:
        res = self.client.post("/api/runs/999/decide", json={"choice": "approve"})
        self.assertEqual(res.status_code, 404)

    def test_unknown_run_conclude_404(self) -> None:
        res = self.client.post(
            "/conclude", json={"run_id": 999, "status": "completed"}, headers={"X-Agent-Token": TOKEN}
        )
        self.assertEqual(res.status_code, 404)


class AdapterTest(unittest.TestCase):
    def test_build_prompt_contains_skill_and_ask(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "skills" / "demo").mkdir(parents=True)
        (root / "skills" / "demo" / "SKILL.md").write_text("# 只读巡检", encoding="utf-8")
        (root / "config.json").write_text("{}", encoding="utf-8")
        cfg = load_config(str(root / "config.json"))
        (root / "memory").mkdir(exist_ok=True)
        (root / "memory" / "定了的事.md").write_text(
            "# 定了的事\n\n- 2026-11-13 · 问：要不要删？ → 定：approve\n", encoding="utf-8"
        )
        prompt = build_prompt(
            {
                "id": 1,
                "ask": "做一次巡检",
                "skill": "demo",
                "cwd": str(root),
                "e_source": "test",
                "e_type": "demo",
                "e_summary": "摘要",
                "e_payload": {"question": "要不要动这个文件？"},
            },
            cfg,
        )
        self.assertIn("做一次巡检", prompt)
        self.assertIn("# 只读巡检", prompt)
        self.assertIn("JSON schema", prompt)
        self.assertIn("要不要动这个文件？", prompt)
        self.assertIn("决策记忆", prompt)
        self.assertIn("定：approve", prompt)
        tmp.cleanup()

    def test_mock_executor_asks_when_payload_has_question(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "config.json").write_text("{}", encoding="utf-8")
        cfg = load_config(str(root / "config.json"))
        result = mock_adapter.execute(
            {
                "id": 8,
                "ask": "巡检",
                "e_source": "test",
                "e_type": "demo",
                "e_payload": {"question": "删不删？"},
            },
            cfg,
        )
        self.assertEqual(result["status"], "need_decision")
        self.assertEqual(result["report"]["human_question"], "删不删？")
        tmp.cleanup()

    def test_mock_executor_continues_after_decision(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "config.json").write_text("{}", encoding="utf-8")
        cfg = load_config(str(root / "config.json"))
        result = mock_adapter.execute(
            {
                "id": 9,
                "ask": "巡检",
                "e_source": "test",
                "e_type": "demo",
                "e_payload": {"decision": {"choice": "approve"}, "prior_run": 8},
            },
            cfg,
        )
        self.assertEqual(result["status"], "completed")
        self.assertIn("approve", result["report"]["summary"])
        tmp.cleanup()

    def test_mock_executor_returns_structured_completion(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "config.json").write_text("{}", encoding="utf-8")
        cfg = load_config(str(root / "config.json"))
        result = mock_adapter.execute(
            {"id": 7, "ask": "做一次巡检", "skill": "demo", "e_source": "test", "e_type": "demo"},
            cfg,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["report"]["status"], "done")
        self.assertTrue((cfg.runs_dir / "run-7" / "report.json").exists())
        tmp.cleanup()

    def test_parse_report_plain_and_fenced(self) -> None:
        plain, err = _parse_report('{"status":"nothing","title":"t","summary":"s","needs_human":false}')
        self.assertIsNone(err)
        self.assertEqual(plain["status"], "nothing")
        fenced, err2 = _parse_report('```json\n{"status":"done"}\n```')
        self.assertIsNone(err2)
        self.assertEqual(fenced["status"], "done")
        bad, err3 = _parse_report("不是 JSON")
        self.assertIsNotNone(err3)
        self.assertIsNone(bad)


if __name__ == "__main__":
    unittest.main()
