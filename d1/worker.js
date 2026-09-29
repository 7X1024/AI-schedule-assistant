/**
 * AI 日程助手 · Cloudflare Worker
 *
 * 职责：唯一写入方。所有写操作（网页 App、iOS 快捷指令）都必须经过这里，
 *       这样就不可能出现两个地方同时写导致的重复行。
 *
 * 鉴权：请求头 Authorization: Bearer <WORKER_TOKEN>
 *
 * 端点
 *   GET  /api/health                      健康检查
 *   GET  /api/list?type=&user=             列出（已过滤软删除）
 *   POST /api/save     {type,item,user}    新增（按 id 幂等）
 *   POST /api/delete   {type,id}           软删除
 *   POST /api/toggle   {id}                勾选/取消勾选
 *   POST /api/update   {type,id,updates}   改字段
 *
 * 设计约束（与 Python 端 d1_storage.py 严格对齐，改一处必须改两处）
 *   - save 在 INSERT 成功后绝不抛错，也不把已写入的行报成失败。
 *   - 命中同 id 且未删除 → status="skipped"，返回串带 SKIPPED_MARKER。
 *   - 命中同 id 但已软删 → 先物理清除再插入，id 可复用。
 *   - 每个写操作都必须带 `user`（由 d1_storage 从 Streamlit 会话推出，
 *     不是浏览器能控制的参数）。归属行只有本人能改；无归属行只有
 *     LEGACY_USER_OWNER 指定的人能改，否则无人能改。
 *
 * 威胁模型：WORKER_TOKEN 只存在于 Streamlit 服务端 secrets，浏览器拿不到。
 * 这里的 user 校验是纵深防御——防止 d1_storage 自身出 bug 时跨账号写坏数据。
 */

const SKIPPED_MARKER = "[skipped:duplicate-id]";

const PRIORITIES = new Set(["low", "medium", "high"]);
const TIME_PERIODS = new Set(["morning", "noon", "afternoon", "evening", "night"]);

const NULLABLE = ["date", "start_time", "end_time", "deadline", "location", "time_period"];
const WRITABLE = [
  "title", "priority", "date", "start_time", "end_time", "deadline",
  "location", "needs_confirmation", "is_completed", "time_period",
];

// 资源上限。没有限流，下面这些是唯一能挡住滥用客户端的东西。
const MAX_BODY_BYTES = 64 * 1024;
const MAX_UPDATE_KEYS = 50;
const MAX_TEXT_LEN = 2000;
const MAX_TITLE_LEN = 300;
// id 和 user 是身份，不截断只拒绝——见 save() 里的说明。
const MAX_ID_LEN = 128;
const MAX_USER_LEN = 64;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function nowIso() {
  return new Date().toISOString();
}

function str(v) {
  return v === undefined || v === null ? null : String(v);
}

function toInt(v, fallback = 0) {
  if (v === true) return 1;
  if (v === false) return 0;
  if (typeof v === "number") return v ? 1 : 0;
  const s = String(v ?? "").trim().toLowerCase();
  if (s === "true" || s === "1" || s === "yes") return 1;
  if (s === "false" || s === "0" || s === "no" || s === "") return 0;
  return fallback;
}

/**
 * 枚举归一化。与 sheets_storage._safe_choice 语义一致，且同样做大小写折叠
 * （Sheets 那一侧会 .lower()，这里不折会让 priority="HIGH" 静默退化成 medium）。
 * 非法值退到 fallback，绝不让整行校验失败、整条记录凭空消失。
 */
function safeChoice(raw, allowed, fallback) {
  const orig = raw === undefined || raw === null ? "" : String(raw);
  const s = orig.trim().toLowerCase();
  if (allowed.has(s)) return { value: s, normalised: orig !== s && orig !== "" };
  return { value: fallback, normalised: orig.trim() !== "" };
}

/** 一次保存最多留一条 info/warning，随响应返回；Python 端负责累计成 diagnostics。 */
class Notices {
  constructor() { this.items = []; }
  add(code, message, level = "warning") {
    this.items.push({ code, message, level, count: 1, first_seen: nowIso() });
  }
  toArray() { return this.items; }
}

/**
 * 写权限。caller 是 d1_storage 从会话推出的当前登录用户。
 *   - 有归属的行：只有本人可写。
 *   - 无归属的行：只有 LEGACY_USER_OWNER 指定的人可写；未指定则无人可写。
 * 语义与 sheets_storage._row_is_writable 一致，但比它更严：它会真的比对人。
 */
function isWritable(rowUser, caller, env) {
  const owner = String(rowUser ?? "").trim();
  const me = String(caller ?? "").trim();
  if (!me) return false;
  if (owner) return owner === me;
  const custodian = String(env.LEGACY_USER_OWNER ?? "").trim();
  return custodian !== "" && custodian === me;
}

/** 截断超长文本。超限时记一条通知——静默截断等于悄悄改用户的日程。 */
function cap(s, n, field, notices) {
  const t = String(s ?? "");
  // 判断和截断必须用同一把尺子：都按码点算，否则全是 emoji 的文本会被
  // 谎报成「已截断」而其实一个字都没少。
  const cps = Array.from(t);
  if (cps.length <= n) return t;
  const cut = cps.slice(0, n).join("");   // 按码点切，别把代理对劈成 U+FFFD
  notices?.add("field_truncated",
    `${field} 超过 ${n} 字，已截断（原文 ${cps.length} 字）`, "info");
  return cut;
}

// ── 端点实现 ────────────────────────────────────────────────────────────────

async function health(env) {
  const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM items").first();
  return { ok: true, rows: row ? row.n : 0, skipped_marker: SKIPPED_MARKER };
}

async function listItems(env, url) {
  const type = (url.searchParams.get("type") || "event").trim();
  const user = (url.searchParams.get("user") || "").trim();
  if (!["event", "todo"].includes(type)) return { ok: false, error: "bad type" };

  // user 为空 = "看全部"，与 sheets_storage._matches_user 一致
  //（user 为空时那边返回 True，也就是不筛）。有 user 时只能看自己的 + 无归属的。
  // 两条 SQL 的占位符个数不同，必须分别绑定——多绑一个会直接 500。
  const stmt = user
    ? env.DB.prepare(
        `SELECT * FROM items
         WHERE type = ?1 AND deleted = 0 AND (user = ?2 OR user = '')
         ORDER BY COALESCE(date, deadline, created_at) ASC, COALESCE(start_time,'99:99') ASC, title ASC`
      ).bind(type, user)
    : env.DB.prepare(
        `SELECT * FROM items
         WHERE type = ?1 AND deleted = 0
         ORDER BY COALESCE(date, deadline, created_at) ASC, COALESCE(start_time,'99:99') ASC, title ASC`
      ).bind(type);

  const { results } = await stmt.all();
  return { ok: true, items: results || [] };
}

async function save(env, body, notices) {
  const type = String(body.type || "").trim();
  const item = body.item || {};
  const user = String(body.user ?? "").trim();
  const id = String(item.id ?? "").trim();

  if (!['event', 'todo'].includes(type)) {
    return { status: "error", saved: false, skipped: false, detail: `未知类型: ${type}` };
  }
  if (!id) {
    return { status: "error", saved: false, skipped: false, detail: "缺少 id" };
  }

  // 1) 同 id 幂等
  const existing = await env.DB
    .prepare("SELECT deleted, user FROM items WHERE id = ?1")
    .bind(id)
    .first();

  if (existing && Number(existing.deleted) === 0) {
    return {
      status: "skipped",
      saved: false,
      skipped: true,
      sheet: type === "event" ? "events" : "todos",
      item_id: id,
      range: `d1!items/${id}（id 已存在，未重复追加）${SKIPPED_MARKER}`,
      detail: "id 已存在，未重复追加",
    };
  }
  if (existing && Number(existing.deleted) === 1) {
    // 软删行不占用 id：物理清除后允许重新录入
    await env.DB.prepare("DELETE FROM items WHERE id = ?1").bind(id).run();
  }

  const pri = safeChoice(item.priority, PRIORITIES, "medium");
  const tp = safeChoice(item.time_period, TIME_PERIODS, null);
  if (pri.normalised) notices.add("normalised_field", `priority「${item.priority}」已归一化为 ${pri.value}`);
  if (tp.normalised && item.time_period) {
    notices.add("normalised_field", `time_period「${item.time_period}」已归一化为 ${tp.value}`);
  }
  if (!String(user || "").trim()) {
    notices.add("blank_user", "这条记录没有归属用户，所有账号都能看到，但默认都不能修改");
  }

  const ts = nowIso();
  // id 和 user 绝不能截断：截断后的 id 找不到、截断后的 user 对不上，
  // 结果是"存进去了却再也操作不到"。超长直接拒绝，让调用方自己改。
  if (id.length > MAX_ID_LEN) {
    return { status: "error", saved: false, skipped: false,
             detail: `id 太长（${id.length} > ${MAX_ID_LEN}）` };
  }
  if (user.length > MAX_USER_LEN) {
    return { status: "error", saved: false, skipped: false,
             detail: `用户名太长（${user.length} > ${MAX_USER_LEN}）` };
  }

  const values = [
    id,
    user,
    type,
    cap(String(item.title ?? "").trim() || "(无标题)", MAX_TITLE_LEN, "title", notices),
    // time_period 必须写归一化后的 tp.value，不能写 item.time_period 原文——
    // 否则通知说"已归一化为 morning"，存进去的却还是 "MORNING"。
    ...NULLABLE.map((f) => {
      if (f === "time_period") return tp.value;
      return cap(str(item[f]), MAX_TEXT_LEN, f, notices) || null;
    }),
    pri.value,
    cap(item.source_text, MAX_TEXT_LEN * 4, "source_text", notices),
    Number.isFinite(Number(item.confidence)) ? Number(item.confidence) : 0,
    toInt(item.needs_confirmation, 1),
    toInt(item.is_completed, 0),
    0,
    str(item.created_at) || ts,
    ts,
  ];

  const sql = `INSERT INTO items
    (id,user,type,title,date,start_time,end_time,deadline,location,time_period,
     priority,source_text,confidence,needs_confirmation,is_completed,deleted,created_at,updated_at)
    VALUES (${values.map((_, i) => `?${i + 1}`).join(",")})`;

  try {
    await env.DB.prepare(sql).bind(...values).run();
  } catch (e) {
    return {
      status: "error", saved: false, skipped: false,
      sheet: type === "event" ? "events" : "todos", item_id: id,
      range: "", detail: `写入失败: ${e && e.message ? e.message : e}`,
    };
  }

  // ── 写入已经成功，下面任何一步都不得把它变成「失败」 ──
  return {
    status: "saved",
    saved: true,
    skipped: false,
    sheet: type === "event" ? "events" : "todos",
    item_id: id,
    range: `d1!items/${id}`,
    detail: "已写入",
  };
}

/** app.py 按「code 含 todo」或「message 含 待办」把提示分到待办栏。
 *  所以凡是发生在待办上的事，措辞里必须带上"待办"，否则会跑到日程栏去。 */
function kindLabel(row) {
  return row && row.type === "todo" ? "待办" : "日程";
}

async function fetchRow(env, id) {
  // id 是全库主键，一行至多一条；不需要再按 type 分支，也不该用 ORDER BY 瞎猜。
  return env.DB
    .prepare("SELECT * FROM items WHERE id = ?1 AND deleted = 0")
    .bind(String(id ?? "").trim())
    .first();
}

async function removeItem(env, body, notices) {
  const id = String(body.id ?? "").trim();
  const caller = String(body.user ?? "").trim();

  const row = await fetchRow(env, id);
  if (!row) {
    notices.add("delete_not_found",
      `未找到要删除的${body.type === "todo" ? "待办" : "记录"} (id=${id.slice(0, 8)}…)，可能已被删除`);
    return { ok: false, code: "not_found", detail: "未找到" };
  }
  if (!isWritable(row.user, caller, env)) {
    notices.add(
      "unattributed_write_blocked",
      `${kindLabel(row)} ${id.slice(0, 8)}… ${row.user ? `属于 ${row.user}` : "没有归属账号"}，已拒绝修改`
    );
    return { ok: false, code: "forbidden", detail: "无权修改该记录" };
  }

  await env.DB
    .prepare("UPDATE items SET deleted = 1, updated_at = ?1 WHERE id = ?2")
    .bind(nowIso(), id)
    .run();
  return { ok: true, code: "deleted", detail: "已删除" };
}

async function toggleItem(env, body, notices) {
  const id = String(body.id ?? "").trim();
  const caller = String(body.user ?? "").trim();

  const row = await fetchRow(env, id);
  if (!row) {
    notices.add("toggle_not_found", `未找到待办 (id=${id.slice(0, 8)}…)，可能已被删除`);
    return { ok: false, code: "not_found", detail: "未找到" };
  }
  if (!isWritable(row.user, caller, env)) {
    notices.add(
      "todo_toggle_blocked",
      `待办 ${id.slice(0, 8)}… ${row.user ? `属于 ${row.user}` : "没有归属账号"}，已拒绝修改`
    );
    return { ok: false, code: "forbidden", detail: "无权修改该待办" };
  }

  const next = Number(row.is_completed) === 1 ? 0 : 1;
  await env.DB
    .prepare("UPDATE items SET is_completed = ?1, updated_at = ?2 WHERE id = ?3")
    .bind(next, nowIso(), id)
    .run();
  return { ok: true, code: "toggled", is_completed: next === 1 };
}

async function updateItem(env, body, notices) {
  const id = String(body.id ?? "").trim();
  const caller = String(body.user ?? "").trim();
  const updates = body.updates && typeof body.updates === "object" ? body.updates : {};

  const keyCount = Object.keys(updates).length;
  if (keyCount > MAX_UPDATE_KEYS) {
    return { ok: false, code: "too_many_fields", written: 0, unknown: [],
             detail: `一次最多改 ${MAX_UPDATE_KEYS} 个字段，收到了 ${keyCount} 个` };
  }

  const row = await fetchRow(env, id);
  if (!row) return { ok: false, code: "not_found", written: 0, unknown: [], detail: "未找到" };
  if (!isWritable(row.user, caller, env)) {
    notices.add(
      "update_blocked",
      `${kindLabel(row)} ${id.slice(0, 8)}… ${row.user ? `属于 ${row.user}` : "没有归属账号"}，已拒绝修改`
    );
    return { ok: false, code: "forbidden", written: 0, unknown: [], detail: "无权修改该记录" };
  }

  // 与 sheets_storage.update_* 的返回值严格对齐：
  //   找到了但 updates 为空 → True（行在，只是没东西可改）
  //   找到了、updates 非空、但没有字段可写 → False
  if (keyCount === 0) return { ok: true, code: "unchanged", written: 0, unknown: [], detail: "无需修改" };

  const unknown = Object.keys(updates).filter((k) => !WRITABLE.includes(k));
  const sets = [];
  const binds = [];
  for (const [k, v] of Object.entries(updates)) {
    if (!WRITABLE.includes(k)) continue;
    if (k === "priority") {
      const p = safeChoice(v, PRIORITIES, "medium");
      if (p.normalised) notices.add("normalised_field", `priority「${v}」已归一化为 ${p.value}`);
      sets.push("priority = ?"); binds.push(p.value);
    } else if (k === "time_period") {
      const t = safeChoice(v, TIME_PERIODS, null);
      sets.push("time_period = ?"); binds.push(t.value);
    } else if (k === "needs_confirmation" || k === "is_completed") {
      sets.push(`${k} = ?`); binds.push(toInt(v, 0));
    } else if (k === "title") {
      sets.push("title = ?"); binds.push(cap(String(v ?? "").trim() || "(无标题)", MAX_TITLE_LEN, "title", notices));
    } else {
      sets.push(`${k} = ?`); binds.push(cap(str(v), MAX_TEXT_LEN, k, notices) || null);
    }
  }

  if (!sets.length) {
    if (unknown.length) notices.add("unknown_field", `忽略了未知字段: ${unknown.join(", ")}`);
    return { ok: false, code: "nothing_writable", written: 0, unknown, detail: "没有可写字段" };
  }
  if (unknown.length) {
    notices.add("unknown_field", `忽略了未知字段: ${unknown.join(", ")}`);
  }

  sets.push("updated_at = ?");
  binds.push(nowIso(), id);
  await env.DB
    .prepare(`UPDATE items SET ${sets.join(", ")} WHERE id = ?${binds.length}`)
    .bind(...binds)
    .run();

  return { ok: true, code: "updated", written: sets.length - 1, unknown, detail: "已更新" };
}

// ── 路由 ────────────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (path === "/" || path === "/api") {
      return json({
        service: "ai-schedule-assistant",
        endpoints: ["/api/health", "/api/list", "/api/save", "/api/delete", "/api/toggle", "/api/update"],
      });
    }

    const auth = request.headers.get("Authorization") || "";
    if (!env.WORKER_TOKEN || auth !== `Bearer ${env.WORKER_TOKEN}`) {
      return json({ ok: false, error: "unauthorized" }, 401);
    }

    const notices = new Notices();
    try {
      if (path === "/api/health" && request.method === "GET") {
        return json(await health(env));
      }
      if (path === "/api/list" && request.method === "GET") {
        const r = await listItems(env, url);
        return json({ ...r, notices: notices.toArray() });
      }

      if (request.method !== "POST") return json({ ok: false, error: "method not allowed" }, 405);

      // 先按 content-length 快速拒绝，再按实际长度复核一次——
      // 只信 content-length 的话，chunked 编码不带这个头就能绕过。
      const declared = Number(request.headers.get("content-length") || 0);
      if (declared > MAX_BODY_BYTES) {
        return json({ ok: false, error: "request body too large" }, 413);
      }
      let raw = await request.text();
      // 按字节算，不是按字符——中文一个字 3 字节，按字符算会漏掉三倍
      const bytes = new TextEncoder().encode(raw).length;
      if (bytes > MAX_BODY_BYTES) {
        return json({ ok: false, error: "request body too large" }, 413);
      }
      let body;
      try {
        body = JSON.parse(raw);
      } catch {
        return json({ ok: false, error: "invalid JSON body" }, 400);
      }
      if (!body || typeof body !== "object" || Array.isArray(body)) {
        return json({ ok: false, error: "body must be a JSON object" }, 400);
      }

      if (path === "/api/save") {
        const r = await save(env, body, notices);
        return json({ ...r, notices: notices.toArray() });
      }
      if (path === "/api/delete") {
        const r = await removeItem(env, body, notices);
        return json({ ...r, notices: notices.toArray() });
      }
      if (path === "/api/toggle") {
        const r = await toggleItem(env, body, notices);
        return json({ ...r, notices: notices.toArray() });
      }
      if (path === "/api/update") {
        const r = await updateItem(env, body, notices);
        return json({ ...r, notices: notices.toArray() });
      }
      return json({ ok: false, error: "not found" }, 404);
    } catch (e) {
      // 到这里说明是 Worker 内部错误；已经写进去的数据不受影响
      return json(
        { ok: false, error: "internal error", detail: String((e && e.message) || e),
          notices: notices.toArray() },
        500
      );
    }
  },
};
