# Agent Ops —— Agent 自动化调度中台（P0）

> 仓库：<https://github.com/dongqining1218/agent-ops>（私有）

> 把「什么时候开始干活」从人身上拿走的那个调度层。
> 事件进来 → 查规则 → 排队 → 派给 worker → Agent 干活 → 结构化结论落库 → 一个页面看结果。
> 人只在「需要决定」时被打扰。

P0 已完成并跑通端到端（mock 执行器）；真实执行器 `codex` 已接好，但**本机 codex 登录态失效，需要先 `codex login`**。

---

## 1. 现在有什么

```
[定时触发器]──┐
[GitHub]──────┼─▶ POST /events ──▶ 去重 ──▶ 匹配 rules.json ──▶ 入队(run)
[Webhook]─────┘                                              │
                                              POST /lease ◀──┘   （worker 长轮询，默认挂 50s）
                                                    │
                                             worker 领活 ──▶ 执行器（codex / mock）
                                                    │
                                             POST /conclude ◀── 结构化 Report
                                                    │
                                      SQLite（events / runs）+ 仪表盘 + inbox
```

已实现：

| 能力 | 位置 | 说明 |
|---|---|---|
| 统一事件接入 | `POST /events` | 6 字段 + 幂等键去重；无规则的事件只记录不派单 |
| 规则路由 | `rules.json` | 加一种事件 = 加一条规则，不改代码 |
| 长轮询派单 | `POST /lease` | worker 挂 50 秒等活，机器不用暴露公网 |
| 并发与同对象串行 | `server/db.py` | 同 `group` 并发上限；同 `subject` 同时只跑一条 |
| 结论回写 | `POST /conclude` | 幂等；`completed / failed / need_decision` 三态 |
| 失败重试 | `server/db.py` | 最多 3 次，指数退避；超限标记失败 |
| 掉线回收 | 后台 sweeper | 租约过期自动重派（worker 崩了不会把活卡死） |
| 结构化结论 | `schemas/report.schema.json` | codex 用 `--output-schema` 直接吐 JSON |
| 观察页 | `GET /` `/ui/inbox` | 仪表盘 + 待我决定 |
| 执行器可插拔 | `worker/executor.py` | `codex`（真实）/ `mock`（验证链路） |
| 测试 | `tests/` | 13 项：去重/无规则/派单/串行/重试/租约回收/幂等/空闲态 |

## 2. 快速开始

依赖：macOS + Python 3.9+（FastAPI/uvicorn 已装）、`codex` CLI（真实执行时才需要）。

```bash
cd agent-ops

# ① 起中台（前台，默认 127.0.0.1:8765）
scripts/server.sh

# ② 另开两个终端：定时触发器 + worker
scripts/ticker.sh --force        # 无视时间，立刻敲一次门（调试用）
scripts/worker.sh --once         # 领一次活，干完就退出

# ③ 看结果
open http://127.0.0.1:8765        # 仪表盘
scripts/status.sh                 # 命令行版
```

### 一键演示（mock，不花 token）

```bash
scripts/demo.sh
```

它会起一个隔离实例（`data-demo/`、`rules.mock.json`），敲门 → 派单 → 回写 → 打印结果，然后关掉。

### 真实执行

1. 先在终端跑 `codex login`（当前 token 已失效：`refresh token was already used`）。
2. 确认 `rules.json` 里 `to.runtime` 是 `codex`、`to.sandbox` 是 `read-only`。
3. 正常启动 `scripts/server.sh` + `scripts/worker.sh`。
4. `scripts/ticker.sh --force` 敲门，worker 会真跑一次 `codex exec`，产物在 `data/runs/run-<id>/`（`prompt.md` / `stdout.log` / `stderr.log` / `report.json`）。

> 没登录时不会静默失败：worker 会把「codex 登录失效，请先运行 `codex login`」写进结论，并按规则重试。

## 3. 目录

```
agent-ops/
├── config.json              # 端口、超时、重试、agent token
├── rules.json               # 真实规则（runtime=codex）
├── rules.mock.json          # 演示规则（runtime=mock）
├── schemas/report.schema.json  # 结构化结论契约
├── skills/repo-daily-check/SKILL.md  # 第一个 skill：仓库只读巡检 SOP
├── server/                  # 中台：app / db / rules / config / views
├── worker/                  # 接单程序：poller / executor / codex_adapter / mock_adapter
├── triggers/                # 定时触发器 + triggers.json
├── tests/                   # unittest（13 项）
├── scripts/                 # server / worker / ticker / status / demo
├── docs/                    # 01 需求分析 / 02 任务清单 / 03 系统设计 / 04 实施记录
└── data/                    # SQLite + 运行产物（已 gitignore）
```

## 4. 怎么加东西

### 加一条规则（最常见）

`rules.json` 里加一项即可，十分钟内生效（或 `POST /api/rules/reload`）：

```json
{
  "name": "中台意见",
  "when": { "source": ["virxact-hub"], "types": ["feedback.new"] },
  "to": { "agent": "m4-codex", "cwd": "~/code/appeal-insight",
          "skill": "hub-feedback", "sandbox": "read-only", "runtime": "codex" },
  "ask": "按 skill 里「被中台叫起来」那一节处理。",
  "group": "hub-feedback", "parallel": 2, "tell": "needed"
}
```

### 加一个事件源

往 `POST /events` 发统一格式就行（定时、GitHub webhook、邮件、告警都一样）：

```bash
curl -X POST http://127.0.0.1:8765/events -H 'Content-Type: application/json' -d '{
  "source": "github", "type": "pull_request.opened",
  "subject": "repo#123", "summary": "有人开了 PR", "link": "https://github.com/...",
  "idempotency_key": "github:pr:123"
}'
```

### 加一个 skill

新建 `skills/<名字>/SKILL.md`，写清：目的、铁律、步骤、输出契约、反例。规则里 `to.skill` 指过去即可。
**skill 是给 Agent 的执行 SOP，不是给人看的文档**——写不清，Agent 就会自由发挥。

### 加一个执行器

在 `worker/` 新增一个模块，实现 `execute(run, cfg, dry_run) -> conclude body`，在 `worker/executor.py` 的 `execute` 里加一行分发。

## 5. 关键设计（为什么这么写）

- **事件先落库再派单**：事件不可变，`idem_key` 唯一索引兜去重；派单是另一张表，重派不改事件。
- **规则外置**：中台代码里没有任何业务名词，加业务只动 `rules.json` + `skills/`。
- **拉模式**：worker 主动长轮询，中台不需要能连到执行机（家里/公司内网都行）。
- **结论结构化**：`--output-schema` 让 Agent 直接交 JSON，中台不猜散文；解析失败也不假装成功（记 `parse_error` 并落原文）。
- **诚实的失败**：超时、非零退出、schema 不合、登录失效，都走失败重试/上报，不静默吞掉。

## 6. 已知限制 / 下一步（P1）

| 限制 | 影响 | P1 计划 |
|---|---|---|
| 没有决策动作 | `need_decision` 只展示，不能点 | 审批页：同意/拒绝 → 生成新事件回流 |
| 没有 `observe` 模式 | 非结构化来源接进来就会真开工 | 加 `off/observe/auto`，先只记录 |
| 没有授权对象 | 工单只记规则，不记「凭什么能做、能做多大」 | 简化 Mandate（强度/范围/规则版本） |
| 没有订阅直达 | 后续讨论要重新敲门 | Ticket ↔ Thread 订阅，后续消息直接喂执行者 |
| 没有 CI 质检 | Agent 的产出没人二次审查 | 只读审查（最多 3 轮）+ 分支保护 |
| 事件源只有定时 | 不够及时 | GitHub（webhook）/ 飞书 |
| 无鉴权（/events） | 仅本机可用 | 本地绑定 127.0.0.1；对外时加 token |

## 7. 常见问题

**Q：为什么 codex 一直失败？**
先跑 `codex login`。当前报错是 `refresh token was already used`，属于登录态失效，不是网络问题。另外 `codex exec` 需要完整本地权限（DSH 沙箱下会 EPERM）。

**Q：worker 挂了会丢活吗？**
不会。租约（默认 50 秒）过期后后台 sweeper 自动重派，最多 3 次，之后标失败并在仪表盘可见。

**Q：怎么确认没在乱烧 token？**
P0 的巡检规则是 `read-only` + 每天一次；演示/联调用 `rules.mock.json` 不花 token。规则里的 `ask` 越短，成本越低。
