# AI 情报站 Telegram 自动发布器

每天中国时间上午 9:00，通过 OpenAI Responses API 搜索过去 24 小时的国内和全球 AI 新闻，向 `@aiqinbaozhan` 先发精简主帖，再以回复主帖的方式发送排版完整的深度 PDF。两个版本来自同一份新闻数据。

## 它会做什么

- 深度数据精选1～8个不同事件；严格核验后只有1～3条也允许发布，不凑数。每天仍搜索国内与全球，少量版允许只有一个地区；4～8条的常规版仍覆盖两地。主帖少量版精选1～3条，常规版精选2～5条。
- 主帖包含“⚡ 今天一句话”、国内AI、全球AI、创业机会栏目及“📌 今天只记住这 3 件事”；每条只保留标题和一句结论，总长度不超过1100 UTF-16字符，不靠截断缩短。
- PDF包含背景、事件、关键更新、重要性、中国用户影响、商业机会、关注判断，以及可点击的可靠来源链接；分析与事实明确区分。
- PDF设封面、阅读导航、国内AI、全球AI、创业/商业机会、AI工具、今日结论、新闻来源，嵌入中文字体并自动分页。
- 使用自己的中文表达，不照抄新闻原文。
- 严格限定过去 24 小时；1～3条不会因数量不足被拒绝。无可信新闻、时间不符、来源无效或排版校验失败时停止发布，不降低事实标准。可解析但未通过校验的内容最多重新检索一次。
- 默认 `DRY_RUN=true`，只在 Actions 日志预览，不发送消息。
- 生成PDF成功后才开始发送。用中国日期与分阶段状态防重；主帖成功、PDF明确被拒绝时，可重跑只补PDF，不重新发送主帖。

## 项目结构

```text
.
├─ .github/workflows/publish.yml  # 定时与手动工作流
├─ src/ai_news_bot.py             # 生成、校验、去重与发布
├─ src/deep_report.py             # 共享内容规则、精简排版、中文PDF
├─ requirements.txt              # ReportLab PDF依赖
├─ scripts/preview_layout.py      # 不调用API的合成版式测试
├─ tests/test_ai_news_bot.py      # 离线单元测试
└─ state/publish-state.json       # 当天发送状态（不含密钥）
```

## 第一步：创建 GitHub 仓库

1. 登录 GitHub，点击右上角 **+ → New repository**。
2. Repository name 建议填写 `aiqinbaozhan`。
3. 选择 **Private** 或 **Public** 均可。
4. 如果你准备上传本项目现有代码，创建时不要勾选自动添加 README、`.gitignore` 或 License，以免首次推送冲突。
5. 点击 **Create repository**。

## 第二步：上传代码

在本项目目录打开终端，把下面的 `YOUR_GITHUB_NAME` 替换成你的 GitHub 用户名：

```bash
git init
git add .
git commit -m "Initial commit: AI 情报站自动发布器"
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_NAME/aiqinbaozhan.git
git push -u origin main
```

也可以在空仓库页面点击 **uploading an existing file**，上传本项目全部文件。请确认 `.github/workflows/publish.yml` 也已上传；某些系统会隐藏以点开头的目录。

## 第三步：准备 Telegram 频道

1. 在 Telegram 联系 `@BotFather`，用 `/newbot` 创建机器人并取得 Bot Token。
2. 打开频道 `@aiqinbaozhan` 的管理设置，把机器人添加为管理员。
3. 至少授予机器人 **Post Messages / 发布消息** 权限。
4. 不要把 Bot Token 发到聊天、Issue、代码、README 或 Actions 普通变量中。

## 第四步：添加 GitHub Secrets 和 Variables

进入仓库 **Settings → Secrets and variables → Actions**。

在 **Secrets** 标签点击 **New repository secret**，逐个添加：

| Name | Secret |
|---|---|
| `OPENAI_API_KEY` | 你的 OpenAI API Key |
| `TELEGRAM_BOT_TOKEN` | BotFather 给出的 Telegram Bot Token |

在 **Variables** 标签点击 **New repository variable**，添加：

| Name | Value |
|---|---|
| `TELEGRAM_CHANNEL` | `@aiqinbaozhan` |
| `DRY_RUN` | `true` |

密钥只应放在 **Secrets**。不要把任何 Token 或 API Key 写入 Variables。

## 第五步：先用 DRY_RUN 测试

1. 打开仓库的 **Actions** 标签。
2. 左侧选择 **发布每日 AI 情报**。
3. 点击 **Run workflow**。
4. `dry_run` 保持为 `true`，再次点击绿色 **Run workflow**。
5. 打开本次运行，展开 **生成并按配置发布**。
6. 日志显示精简主帖和 `DRY_RUN=true`；回到该次运行的汇总页，在 **Artifacts** 下载 `aiqinbaozhan-deep-report-运行编号`，解压检查PDF。
7. 确认频道没有收到消息。DRY_RUN 不调用 Telegram，也不修改防重状态。

如果失败，日志只会输出经过脱敏的错误，不会输出请求头或密钥。常见原因是 OpenAI API 账户未启用计费、模型无权限、搜索暂时失败，或严格筛选后不足 4 条合格新闻。

## 第六步：切换成真实发布

完成预览后：

1. 回到 **Settings → Secrets and variables → Actions → Variables**。
2. 把 `DRY_RUN` 的值从 `true` 改成 `false`。
3. 定时任务会在每天 `01:00 UTC` 运行，即中国时间 `09:00`。

GitHub 的 cron 任务有时会因平台排队晚几分钟启动，但 cron 配置本身就是中国时间上午 9:00。

## 第七步：手动触发一次真实测试

1. 打开 **Actions → 发布每日 AI 情报 → Run workflow**。
2. 把 `dry_run` 选为 `false`。
3. 点击 **Run workflow**。
4. 频道应先收到精简主帖，再收到回复关联的当日深度PDF；日志显示主帖ID和PDF ID。状态文件最终为 `published`。

当天 `published` 状态再次运行会跳过；`main_sent` 状态只使用已保存的数据补发PDF，不调用OpenAI、不重发主帖。

> 手动运行表单中的 `dry_run` 会覆盖仓库变量 `DRY_RUN`；定时运行使用仓库变量。

## 防重复与故障恢复

真实发布采用“先占位、后发送”策略：

1. GitHub Actions 的 `concurrency` 保证同一时间只有一个发布任务执行。
2. 完成主帖与PDF生成后，提交 `sending_main`（含当日新闻数据）。
3. 主帖成功后提交 `main_sent`，保存 `telegram_message_id`；发PDF前提交 `sending_document`。
4. PDF成功后提交 `published`，保存两个消息ID和内容哈希。
5. PDF明确返回4xx拒绝时恢复为 `main_sent`，本次Actions失败；修复权限等问题后，当天手动重跑只补同一份PDF。
6. 网络超时、5xx或成功响应缺少ID属于未知结果，保留占位并报错，不自动重发。旧版 `sending` 与 `published` 状态继续兼容。

这样即使 Telegram 已收到消息、但工作流在保存最终状态前断开，也不会重复发送。代价是：如果 Telegram 请求结果不明确，程序宁可停止当天自动重试，也不冒险重复发帖。

未知发送结果的恢复方法（不要盲目清空状态）：

1. 先人工检查频道当天是否已经有帖子。
2. 主帖与PDF都已收到：只把 `status` 标记为 `published`，尽量补齐对应消息ID，不重跑发送。
3. 主帖已收到、PDF确定没有：保留原来的 `digest`、`generated_at`、`channel`、`date`，填写/核实主帖的 `telegram_message_id`，只把状态改为 `main_sent`；同一天重跑只补PDF。主帖的频道链接末尾数字通常就是消息ID。
4. 只有确认当天连主帖也没有发送，且没有仍在运行的任务，才可恢复为：

```json
{
  "date": null,
  "status": "never_published"
}
```

5. 提交修改后，再手动运行一次 `dry_run=false`。跨日期不自动补历史文档；可从原运行Artifacts取回PDF人工关联发布。禁止把有主帖的状态清空，否则会导致主帖重复。

状态保存失败时，最后一次发送占位仍会阻止重复。Telegram与GitHub不能组成原子事务，无法保证网络异常下恰好一次，因此采用保守的“未知结果不重发”策略。

## 安全设计

- 程序只从环境变量读取密钥；GitHub Actions 从 Repository Secrets 注入。
- 代码、README、初始状态文件和测试数据不包含真实密钥。
- 错误日志会替换已加载的 OpenAI、Telegram 和 GitHub Token，并额外过滤常见 Token 格式。
- 不会打印 API 请求头。
- `GITHUB_TOKEN` 由 GitHub Actions 自动生成，用于写入防重状态，不需要你创建。
- 工作流只有 `contents: write` 权限。

## 本地运行（可选）

需要 Python 3.11 或更高版本，先执行 `python -m pip install -r requirements.txt`。Linux安装 `fonts-wqy-zenhei`；Windows默认使用黑体。可用 `AI_PDF_FONT_PATH` 指定支持TrueType的中文TTF/TTC字体路径。字体缺失会失败，不先发主帖。

仅检查排版（合成测试内容，不是真实新闻，不会发布）：`python scripts/preview_layout.py`，输出至 `output/pdf/aiqinbaozhan-layout-preview.pdf`。

本次升级不需要新增Secrets或更换频道变量。Actions自动安装PDF依赖和字体。生成的PDF保存为运行Artifact30天；仓库状态只保存公开新闻数据，不保存密钥。来源URL与时间检查是技术护栏，不是对每项事实的独立人工核验，仍应通过DRY_RUN抽查。

运行测试：

```bash
python -m unittest discover -s tests -v
```

本地 DRY_RUN 预览：

```bash
export OPENAI_API_KEY="在你自己的终端设置，不要写入文件"
export DRY_RUN="true"
python src/ai_news_bot.py
```

Windows PowerShell：

```powershell
$env:OPENAI_API_KEY = "在你自己的终端设置，不要写入文件"
$env:DRY_RUN = "true"
python src/ai_news_bot.py
```

## 可选调整

- 默认模型是 `gpt-5.6-luna`。如需临时更换，可在运行环境设置 `OPENAI_MODEL`；无需改代码。
- 发布时间由 `.github/workflows/publish.yml` 的 `cron` 控制。GitHub 使用 UTC，当前 `0 1 * * *` 对应中国时间每天 09:00。
