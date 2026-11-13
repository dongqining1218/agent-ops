"""最小可用的 HTML 视图：一张仪表盘 + 一个待我决定页。

原则（抄 B 的面板哲学）：先回答「现在有没有事需要我」，健康状态不占地方。
"""
from __future__ import annotations

import html
from typing import Any, Dict, List

CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; font: 14px/1.55 -apple-system, "PingFang SC", system-ui, sans-serif;
       background: #0f1216; color: #e6e9ee; }
h1 { font-size: 18px; margin: 0 0 4px; letter-spacing: .01em; }
h2 { font-size: 13px; margin: 28px 0 10px; color: #9aa4b2; font-weight: 600;
     text-transform: uppercase; letter-spacing: .08em; }
.sub { color: #7d8794; font-size: 12px; margin-bottom: 20px; }
.cards { display: flex; gap: 10px; flex-wrap: wrap; }
.card { background: #171c22; border: 1px solid #232a33; border-radius: 10px; padding: 12px 14px; min-width: 108px; }
.card .n { font-size: 22px; font-variant-numeric: tabular-nums; }
.card .k { color: #7d8794; font-size: 11px; }
table { width: 100%; border-collapse: collapse; background: #171c22; border: 1px solid #232a33;
        border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #232a33; vertical-align: top; }
th { color: #7d8794; font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
tr:last-child td { border-bottom: 0; }
.tag { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px;
       border: 1px solid #2c3540; color: #a8b2be; white-space: nowrap; }
.tag.ok { border-color: #1f5c3a; color: #6fd08c; }
.tag.bad { border-color: #6b2731; color: #f0818f; }
.tag.wait { border-color: #6b5320; color: #e3bb63; }
.tag.run { border-color: #24405e; color: #79b8f5; }
.mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; color: #8f9bab; }
.decide { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
input { background: #0f1216; border: 1px solid #2c3540; color: #e6e9ee; border-radius: 7px;
        padding: 5px 9px; font-size: 12px; min-width: 150px; }
button { background: #1d2530; border: 1px solid #2c3540; color: #cfd8e3; border-radius: 7px;
         padding: 5px 11px; font-size: 12px; cursor: pointer; }
button:hover { border-color: #3d4b5c; color: #fff; }
button.ok { border-color: #1f5c3a; color: #8fdc9f; }
button.no { border-color: #6b2731; color: #f0a3ad; }
.empty { color: #6b7480; padding: 14px 12px; background: #14192000; }
a { color: #79b8f5; text-decoration: none; }
a:hover { text-decoration: underline; }
"""

TAG_CLASS = {
    "queued": "run",
    "leased": "run",
    "completed": "ok",
    "done": "ok",
    "recorded": "",
    "failed": "bad",
    "waiting_human": "wait",
}


def _tag(status: str) -> str:
    return '<span class="tag %s">%s</span>' % (TAG_CLASS.get(status, ""), html.escape(status))


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=zh><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>%s · Agent Ops</title><style>%s</style></head><body>%s</body></html>"
        % (html.escape(title), CSS, body)
    )


def render_dashboard(
    counts: Dict[str, Any],
    runs: List[Dict[str, Any]],
    inbox: List[Dict[str, Any]],
    health: Dict[str, Any],
    rules: List[str],
) -> str:
    waiting = len(inbox)
    run_counts = counts.get("runs", {})
    cards = [
        ("待我决定", waiting),
        ("在跑", run_counts.get("leased", 0)),
        ("已完成", run_counts.get("completed", 0)),
        ("已决定", run_counts.get("decided", 0)),
        ("失败", run_counts.get("failed", 0)),
        ("事件总数", sum(counts.get("events", {}).values())),
    ]
    body = ["<h1>Agent Ops</h1>"]
    body.append(
        "<div class=sub>中台在 %s · 规则 %d 条 · %s</div>"
        % (
            html.escape(health.get("now", "")),
            len(rules),
            "健康" if health.get("ok") else "有问题",
        )
    )
    body.append("<div class=cards>")
    for label, value in cards:
        body.append(
            "<div class=card><div class=n>%s</div><div class=k>%s</div></div>"
            % (value, html.escape(label))
        )
    body.append("</div>")

    if inbox:
        body.append('<h2>需要我 · <a href="/ui/inbox">去批奏折 →</a></h2>')
        body.append(
            "<table><tr><th>run</th><th>规则</th><th>结论</th><th>问题</th></tr>"
        )
        for run in inbox:
            report = run.get("report") or {}
            question = report.get("human_question") or run.get("summary") or ""
            body.append(
                "<tr><td class=mono>#%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (
                    run["id"],
                    html.escape(str(run.get("rule", ""))),
                    html.escape(str(run.get("summary", ""))),
                    html.escape(str(question)),
                )
            )
        body.append("</table>")

    body.append("<h2>最近的运行</h2>")
    if not runs:
        body.append("<div class=empty>还没有运行记录。先发一条事件试试。</div>")
    else:
        body.append(
            "<table><tr><th>#</th><th>事件</th><th>规则</th><th>状态</th>"
            "<th>尝试</th><th>结论</th><th>时间</th></tr>"
        )
        for run in runs:
            body.append(
                "<tr><td class=mono>%s</td><td>%s<br><span class=mono>%s</span></td>"
                "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=mono>%s</td></tr>"
                % (
                    run["id"],
                    html.escape(str(run.get("e_summary", ""))[:80]),
                    html.escape(str(run.get("e_type", ""))),
                    html.escape(str(run.get("rule", ""))),
                    _tag(str(run.get("status", ""))),
                    run.get("attempts", 0),
                    html.escape(str(run.get("summary") or run.get("error") or "")[:120]),
                    html.escape(str(run.get("started_at") or run.get("created_at") or "")),
                )
            )
        body.append("</table>")
    return _page("仪表盘", "".join(body))


def render_inbox(inbox: List[Dict[str, Any]]) -> str:
    body = [
        "<h1>需要我决定</h1>",
        "<div class=sub>点一下就是决策：决定会写进「定了的事」，并让 Agent 继续往下干。</div>",
    ]
    if not inbox:
        body.append("<div class=empty>没有待决定的事。这是好状态。</div>")
    else:
        body.append("<table><tr><th>run</th><th>问题</th><th>它查到的情况</th><th>决定</th></tr>")
        for run in inbox:
            report = run.get("report") or {}
            question = report.get("human_question") or run.get("summary") or ""
            options = report.get("options") or []
            buttons = [
                '<button class=ok name=choice value=approve>同意</button>',
                '<button class=no name=choice value=reject>拒绝</button>',
            ]
            for option in options:
                if str(option) in ("approve", "reject"):
                    continue
                buttons.append(
                    '<button name=choice value="%s">%s</button>'
                    % (html.escape(str(option)), html.escape(str(option)))
                )
            body.append(
                "<tr><td class=mono>#%s</td><td>%s</td><td>%s</td>"
                "<td><form method=post action=/ui/decide class=decide>"
                "<input type=hidden name=run_id value=%s>"
                "<input name=note placeholder='备注（可空）'>%s</form></td></tr>"
                % (
                    run["id"],
                    html.escape(str(question)),
                    html.escape(str(report.get("summary") or run.get("summary") or "")),
                    run["id"],
                    "".join(buttons),
                )
            )
        body.append("</table>")
    return _page("需要我决定", "".join(body))
