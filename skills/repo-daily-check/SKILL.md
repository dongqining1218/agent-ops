# 仓库日报（repo-daily-check）

## 目的

每天一次，对本仓库做**只读**巡检，产出一份「今天有什么值得我管」的简短日报。

## 铁律

- **只读**：不 commit、不 push、不改文件、不装依赖。你只有 read-only 沙箱。
- **不联外网**：不要试图调用需要登录的外部服务（`gh` 未登录），只用本地 git 和文件。
- **宁缺勿滥**：没有值得人管的事，就 `status: "nothing"`，不要为了凑数写废话。
- **不超过 3 条** findings。人的注意力是最贵的资源。

## 步骤（按顺序，能跳就跳）

1. `git status --short`：有没有未提交/未跟踪的改动？如果有，判断是"正在做的活"还是"忘了收尾的垃圾"。
2. `git log --oneline --since="7 days ago"`：这周改了什么？有没有一大坨没说明的提交？
3. 扫 `TODO` / `FIXME` / `XXX`（跳过 `node_modules`、`.next`、`dist`、`.git`）：
   `grep -rn --include="*.ts" --include="*.tsx" --include="*.py" --include="*.md" -E "TODO|FIXME" . | head -40`
   只关心**新增或明显在腐坏的**，不要把陈年老 TODO 全列出来。
4. 读 `README.md`：路线图里有没有已经做完但没打勾的项？有没有承诺了但代码里没有的东西？
5. 如果需要判断某处是否真的缺失，去读对应文件确认，**不要猜**。

## 输出契约

最后一条消息必须是符合 schema 的 JSON（不要包 markdown 代码块，不要加解释）：

- `status`：
  - `"done"`：有值得汇报的发现
  - `"nothing"`：巡检完成，没有值得打扰人的事
  - `"blocked"`：仓库状态异常（比如不是 git 仓库、权限不对）导致无法巡检
- `title`：一句话标题（≤30 字）
- `summary`：一到三句话的结论，只说结论不说过程
- `findings`：0~3 条，按重要性排序，每条包含 what / why_it_matters / where
- `needs_human`：是否必须人做决定或动手
- `human_question`：needs_human 为 true 时，写**一个**具体问题

## 反例（不要这样写）

- ❌ "我检查了 git status，然后看了提交记录，又扫了 TODO……"（这是过程，不是结论）
- ❌ 把 20 个老 TODO 全列出来
- ❌ "建议可以考虑优化一下代码结构"（不可执行、不具体）
- ✅ "web/app/cases 里有 3 处 TODO 指向同一个未完成功能（Excel 导入），README 路线图已打勾但代码没实现"
