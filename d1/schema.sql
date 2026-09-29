-- AI 日程助手 · Cloudflare D1 建表脚本
-- 用法：wrangler d1 execute schedule --file=schema.sql --remote
--
-- 设计说明
-- 1. id 是 PRIMARY KEY **NOT NULL**。SQLite 对 rowid 表里的非整型主键有「隐式
--    允许 NULL」的例外，只写 PRIMARY KEY 是拦不住 NULL 的——实测能插进两行
--    id 为 NULL 的记录，主键形同虚设。加上 NOT NULL 才是真正的硬保证。
-- 2. 软删除的行（deleted=1）不占用 id：保存时先物理清除同 id 的软删行，
--    于是「删掉后重新录入同一个 id」能正常工作，与 sheets_storage 的策略一致。
-- 3. 四个索引覆盖高频查询：今日/本周日程、临期提醒、未归属行、单行定位。
--    没有索引时每次都要全表扫描——这正是 Google Sheets 的老毛病。
-- 4. 已知的取舍：列表查询按 COALESCE(date,deadline,created_at) 排序，这个
--    表达式用不上索引，SQLite 会为它建临时 B 树。行数到万级时再考虑换成
--    一个物化的 sort_key 列。

CREATE TABLE IF NOT EXISTS items (
    id                 TEXT    PRIMARY KEY NOT NULL,
    user               TEXT    NOT NULL DEFAULT '',
    type               TEXT    NOT NULL CHECK (type IN ('event', 'todo')),
    title              TEXT    NOT NULL,
    date               TEXT,
    start_time         TEXT,
    end_time           TEXT,
    deadline           TEXT,
    location           TEXT,
    time_period        TEXT,
    priority           TEXT    NOT NULL DEFAULT 'medium',
    source_text        TEXT    NOT NULL DEFAULT '',
    confidence         REAL    NOT NULL DEFAULT 0,
    needs_confirmation INTEGER NOT NULL DEFAULT 1,
    is_completed       INTEGER NOT NULL DEFAULT 0,
    deleted            INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

-- 今日 / 本周日程：WHERE user=? AND type=? AND deleted=0 ORDER BY date
CREATE INDEX IF NOT EXISTS idx_user_type_date
    ON items (user, type, deleted, date);

-- 临期提醒：WHERE user=? AND type=? AND deleted=0 AND is_completed=0
--            AND deadline BETWEEN ? AND ?
CREATE INDEX IF NOT EXISTS idx_user_type_deadline
    ON items (user, type, deleted, is_completed, deadline);

-- 未归属行（user=''）要对全员可见，这张索引让那部分查询也能走索引
CREATE INDEX IF NOT EXISTS idx_unattributed
    ON items (type, deleted) WHERE user = '';

-- 按 id 定位单行（删除 / 勾选 / 编辑都是单行操作）。
-- 实际上是主键自带索引在起作用，这一条是为可读性保留的。
CREATE INDEX IF NOT EXISTS idx_type_id
    ON items (type, id);
