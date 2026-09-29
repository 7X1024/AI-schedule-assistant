# 部署到 Cloudflare D1（方案 A）

把数据从 Google Sheets 搬到 Cloudflare D1。**App 的使用方式完全不变**，
只是底层换了个数据库——手机上照旧打开网页、粘贴、确认。

做完之后你就再也不需要维护那张 Sheets 了。

---

## 前置

- 一台电脑（Mac / Windows 都行），要能跑命令行
- Node.js 18 以上（`node --version` 能出版本号即可）
- Cloudflare 账号，**免费，不需要信用卡** → https://dash.cloudflare.com/sign-up

---

## 最省事的做法：一键脚本

```bash
bash d1/setup.sh
```

它会自己做完这些事，你只需要在浏览器点一次授权：

1. 检查（必要时安装）wrangler
2. 登录，并从 `wrangler whoami` 里读出 Account ID 填进 `wrangler.toml`
3. 建库，把输出的 database_id 填进 `wrangler.toml`
4. 建表
5. 生成一个随机 WORKER_TOKEN 并写进 Worker
6. 部署
7. 把 `WORKER_URL` / `WORKER_TOKEN` **追加**到 `.streamlit/secrets.toml`
8. 最后打印出你需要手动改的那两行 import

跑完只剩两件事：改 `app.py` 的两行 import，以及跑迁移脚本。

脚本是幂等的，重复跑安全。

<details>
<summary>想手动一步一步来（下面是等价的手工流程）</summary>

## 拿 Account ID

登录后点右上角头像 → **My Profile** → **Account ID**（32 位十六进制）。

先把它填进 `d1/wrangler.toml` 的 `account_id = "..."`，不填部署会直接失败。

---

## 第一段：把 Cloudflare 那边搭起来

**注意先 `cd d1`**——`wrangler.toml` 里的 `main = "worker.js"` 和下面的
`--file=schema.sql` 都是相对当前目录的，不切目录会找不到文件。

```bash
cd d1

# 0) 装 wrangler 并登录（浏览器点一下授权）
npm install -g wrangler
wrangler login

# 1) 建库，把输出里的 database_id 填进 wrangler.toml 的 database_id
wrangler d1 create schedule

# 2) 建表
wrangler d1 execute schedule --file=schema.sql --remote

# 3) 配密钥（会提示你输入，输完回车，不回显）
wrangler secret put WORKER_TOKEN   # 自己生成：openssl rand -hex 32

# 4) 部署
wrangler deploy
```

`wrangler deploy` 成功后的输出里会有一个地址，形如：

```
https://ai-schedule-assistant.<你的子域>.workers.dev
```

**把这个地址记下来**，第二步要用。

> 解析用的 DeepSeek key 留在 Streamlit 那边就够了，Worker 本身不调模型，
> 所以不用往 Worker 上设。

---

## 第二段：告诉 App 去哪连

编辑 `.streamlit/secrets.toml`，加上两行：

```toml
WORKER_URL = "https://ai-schedule-assistant.<你的子域>.workers.dev"
WORKER_TOKEN = "第 3 步你设的那串"
```

**Streamlit Cloud 上**：App → Settings → Secrets，把这两行加进去。

---

## 第三段：迁移旧数据（在第二段之后做，脚本要读上面这两个值）

```bash
cd ..                                   # 回到项目根目录
python3 d1/migrate_to_d1.py             # 预览，不写任何东西
python3 d1/migrate_to_d1.py --commit    # 确认无误后真正写入
```

预览会打印按账号 / 按表页 / 按类型的分布，以及每一条被跳过的记录和原因。
**逐条看过再 `--commit`。**

---

## 切换 App 的存储层

只改 `app.py` 的**第 11 行和第 14 行**：

```diff
- from sheets_storage import delete_event, delete_todo, load_events, load_todos, save_event, save_todo, toggle_todo, update_event, update_todo
+ from d1_storage import delete_event, delete_todo, load_events, load_todos, save_event, save_todo, toggle_todo, update_event, update_todo

- import sheets_storage
+ import d1_storage as sheets_storage
```

第二行的别名是故意的——`app.py` 后面还通过 `sheets_storage.get_diagnostics()`、
`get_last_save_outcome()`、`SKIPPED_MARKER` 这几个名字取东西，别名能让它们
继续工作。`d1_storage.py` 对这些名字的语义做了逐条对齐。

**其他任何一行都不用改。**

> 只想看数据不想改代码：用 `wrangler d1 execute schedule --remote --command "SELECT * FROM items LIMIT 20"`
> 就能确认数据有没有搬过来。

---

## 验证

```bash
# 看数据进没进去
wrangler d1 execute schedule --remote --command \
  "SELECT type, user, title, date FROM items ORDER BY created_at DESC LIMIT 20"

# 总量对不对得上（应等于迁移预览里那个「共 N 条」）
wrangler d1 execute schedule --remote --command "SELECT COUNT(*) AS n FROM items"

# 重复行检查。id 是主键，理论上永远查不出东西——
# 查出来就说明 schema 没按预期建，是个有用的哨兵，不是空检查。
wrangler d1 execute schedule --remote --command \
  "SELECT id, COUNT(*) c FROM items GROUP BY id HAVING c > 1"

# 服务是否活着
curl -H "Authorization: Bearer <WORKER_TOKEN>" https://<你的地址>/api/health
```

---

## 回退

D1 出任何问题，把上面那两行 import 改回 `sheets_storage` 即可，
Google Sheets 那边一行数据都没动过——迁移脚本是**只读源表**的。

但要清楚一点：**切换之后在 D1 里新产生的数据，在回退时会被丢掉**，
因为那时 App 已经不写 Sheets 了。真要回退，先把 D1 的数据导回去：

```bash
wrangler d1 execute schedule --remote --json --command "SELECT * FROM items" > items.json
```

（导出格式是 JSON，逐行转回 Sheets 表格需要手工处理；所以回退前先想清楚。）

---

## 和旧方案比

| | Google Sheets | D1 |
|---|---|---|
| 重复行 | 靠代码去重，可能漏 | **主键约束，物理上不可能** |
| 每次读取 | 拉整表到 Python 再筛 | 索引命中，只取需要的行 |
| 删表重建 | 曾导致 App 永久 404 | 无此概念 |
| 误删恢复 | 无 | **7 天 Time Travel** |
| 维护成本 | 要懂表结构、怕改错列 | 不用管 |

---

## 下一步（方案 B）

D1 跑通之后可以在同一个 Worker 上加：
- `/api/ingest` 接口 → iOS 快捷指令「分享 → 记日程」，不用再打开网页
- 定时 cron → 每天早上 8:30 推送今日日程与逾期待办（Bark）
