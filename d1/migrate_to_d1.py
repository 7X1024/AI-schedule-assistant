#!/usr/bin/env python3
"""把现有 Google Sheet 里的数据迁到 Cloudflare D1。

用法（顺序不能反）
    # 0) 先看要搬什么，不写任何东西
    python3 d1/migrate_to_d1.py

    # 1) 确认无误后真正写入
    python3 d1/migrate_to_d1.py --commit

它通过 Worker 的 /api/save 写入，所以去重、软删、枚举归一化这些规则
和网页端保存时**完全一致**——不会出现"迁移脚本写进去一套、App 写进去另一套"。

凭据来源
    WORKER_URL / WORKER_TOKEN : 环境变量，或 .streamlit/secrets.toml
    Google 服务账号           : .streamlit/secrets.toml 里的 [gcp] + GOOGLE_SHEETS_ID
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJ = Path(__file__).resolve().parent.parent
SECRETS = PROJ / ".streamlit" / "secrets.toml"

# 与 sheets_storage.EVENTS_HEADERS 逐列对应
COL = {
    "id": 0, "type": 1, "title": 2,
    "date": 3, "start_time": 4, "end_time": 5,
    "deadline": 6, "location": 7,
    "priority": 8, "source_text": 9,
    "confidence": 10, "needs_confirmation": 11,
    "completed": 12, "deleted": 13,
    "created_at": 14, "updated_at": 15,
    "time_period": 16, "user": 17,
}

BOOL_FIELDS = {"needs_confirmation", "is_completed", "deleted"}
PRIORITIES = {"low", "medium", "high"}
TIME_PERIODS = {"morning", "noon", "afternoon", "evening", "night"}


def load_secrets() -> Dict[str, str]:
    if not SECRETS.exists():
        return {}
    raw = SECRETS.read_text(encoding="utf-8")
    out: Dict[str, str] = {}

    def g(key: str) -> Optional[str]:
        m = re.search(rf'^{key}\s*=\s*"([^"]*)"', raw, re.M)
        return m.group(1) if m else None

    for k in ("GOOGLE_SHEETS_ID", "DEEPSEEK_API_KEY", "WORKER_URL", "WORKER_TOKEN"):
        v = g(k)
        if v:
            out[k] = v
    pk = re.search(r'private_key\s*=\s*"""(.*?)"""', raw, re.S) or \
         re.search(r'private_key\s*=\s*"((?:[^"\\]|\\.)*)"', raw, re.S)
    if pk:
        out["_private_key"] = pk.group(1).replace("\\n", "\n")
    for k in ("project_id", "client_email", "client_id", "token_uri"):
        v = g(k)
        if v:
            out[k] = v
    return out


def conf(key: str, secrets: Dict[str, str]) -> str:
    return str(os.environ.get(key) or secrets.get(key) or "").strip()


def truthy(v: str) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes")


def cell(row: List[str], field: str) -> str:
    idx = COL[field]
    return row[idx].strip() if len(row) > idx else ""


def norm_ts(raw: str) -> str:
    """把时间戳统一成 UTC ISO（…Z）。

    表里混着三种写法：带微秒的本地时间、不带时区的本地时间、Worker 写的 UTC。
    混在一列里会让 `ORDER BY created_at DESC` 的排序变得没有意义。统一不了就
    返回空串，让 Worker 补一个当前时间——那也比留一个错格式强。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            t = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return ""
    if t.tzinfo is None:
        t = t.astimezone()  # 本地时间 → 带时区
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def kind_of(ws_title: str, raw_type: str) -> Optional[str]:
    """判断这行属于 event 还是 todo。判不出来就返回 None——绝不猜。"""
    t = str(raw_type or "").strip().lower()
    if t in ("event", "todo"):
        return t
    name = str(ws_title or "").strip().lower().rstrip("s")
    return name if name in ("event", "todo") else None


def read_sheet(secrets: Dict[str, str]) -> List[Tuple[str, Dict[str, Any]]]:
    """返回 [(worksheet_name, item_dict)]，已滤掉表头与软删行。"""
    try:
        import gspread
        from google.oauth2 import service_account
    except ImportError:
        sys.exit("缺少依赖。请先执行：pip install gspread google-auth")

    if not secrets.get("_private_key") or not secrets.get("GOOGLE_SHEETS_ID"):
        sys.exit("读不到 Google 凭据。请检查 .streamlit/secrets.toml 里的 [gcp] 与 GOOGLE_SHEETS_ID。")

    creds = service_account.Credentials.from_service_account_info(
        {
            "type": "service_account",
            "project_id": secrets.get("project_id"),
            "private_key": secrets["_private_key"],
            "client_email": secrets.get("client_email"),
            "client_id": secrets.get("client_id"),
            "token_uri": secrets.get("token_uri"),
        },
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(secrets["GOOGLE_SHEETS_ID"])

    out: List[Tuple[str, Dict[str, Any]]] = []
    for ws in ss.worksheets():
        values = ws.get_all_values()
        if not values:
            continue

        # F4（数据丢失）：不要想当然地把第一行当表头。
        # 有些表页压根没有表头行，此时把第一行也当数据处理，否则它会被静默丢掉。
        header = [c.strip() for c in values[0]]
        if "id" in header and "title" in header:
            data_rows = values[1:]
        else:
            data_rows = values
            print(f"  ⚠️  表页「{ws.title}」第一行不是表头（缺 id/title 列），"
                  f"已把第一行也当作数据处理")

        for i, row in enumerate(data_rows, start=1):
            where = f"{ws.title} 第 {i} 行"
            rid = cell(row, "id")
            title = cell(row, "title")
            if not rid or not title:
                print(f"  ⚠️  {where} 缺少 id 或 title，已跳过"
                      f"（title={title[:20]!r}）")
                continue
            if truthy(cell(row, "deleted")):
                print(f"  ⚠️  {where} 已软删（{title[:20]}），不迁移")
                continue

            kind = kind_of(ws.title, cell(row, "type"))
            if kind is None:
                print(f"  ✗ {where} 类型无法判断（type={cell(row,'type')!r}，"
                      f"表页名={ws.title!r}），不会迁移这条")
                continue

            pri = cell(row, "priority") or "medium"
            if pri not in PRIORITIES:
                print(f"  ⚠️  {where} priority「{pri}」非法，归一化为 medium")
                pri = "medium"
            tp = cell(row, "time_period")
            if tp and tp not in TIME_PERIODS:
                print(f"  ⚠️  {where} time_period「{tp}」非法，归一化为空")
                tp = ""

            conf = cell(row, "confidence")
            try:
                conf_val: Any = float(conf)
            except ValueError:
                if conf:
                    print(f"  ⚠️  {where} confidence「{conf}」不是数字，归零")
                conf_val = 0.0

            item: Dict[str, Any] = {
                "id": rid,
                "type": kind,
                "title": title,
                "priority": pri,
                "source_text": cell(row, "source_text"),
                "confidence": conf_val,
                "needs_confirmation": truthy(cell(row, "needs_confirmation")),
                "is_completed": truthy(cell(row, "completed")),
                "time_period": tp or None,
                "created_at": norm_ts(cell(row, "created_at")),
            }
            for f in ("date", "start_time", "end_time", "deadline", "location"):
                item[f] = cell(row, f) or None
            out.append((ws.title, {"user": cell(row, "user"), "item": item}))
    return out


def post(base: str, token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(
        f"{base.rstrip('/')}/api/save",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="真正写入 D1（不加这个参数就只打印预览）")
    args = ap.parse_args()

    secrets = load_secrets()
    base = conf("WORKER_URL", secrets)
    token = conf("WORKER_TOKEN", secrets)

    print("=" * 60)
    print("Google Sheet → Cloudflare D1 迁移")
    print("=" * 60)

    if not base or not token:
        print("\n✗ 缺少 WORKER_URL / WORKER_TOKEN。")
        print("  先把它们加到 .streamlit/secrets.toml 或环境变量里再运行。\n")
        return

    if args.commit:
        print("\n确认连到 Worker:", base)
    else:
        print("\n当前是预览模式（DRY RUN）。确认无误后加 --commit 才会写入。")

    print("\n读取 Google Sheet …")
    rows = read_sheet(secrets)
    if not rows:
        print("  表里没有可迁移的数据。")
        return

    # 本地预检：源数据自身的重复 id
    seen: Dict[str, int] = {}
    dupes: List[str] = []
    for _, payload in rows:
        rid = payload["item"]["id"]
        seen[rid] = seen.get(rid, 0) + 1
        if seen[rid] == 2:
            dupes.append(rid)
    if dupes:
        print(f"\n⚠️  源数据里有 {len(dupes)} 个重复 id（D1 的主键只允许一个）：")
        for d in dupes[:10]:
            print(f"     {d}")
        print("  D1 会保留第一条、跳过其余。迁移后请在网页上确认一下。")

    by_user: Dict[str, int] = {}
    by_sheet: Dict[str, int] = {}
    by_type: Dict[str, int] = {}
    for sheet_name, payload in rows:
        key = payload["user"] or "<无归属>"
        by_user[key] = by_user.get(key, 0) + 1
        by_sheet[sheet_name] = by_sheet.get(sheet_name, 0) + 1
        t = payload["item"]["type"]
        by_type[t] = by_type.get(t, 0) + 1
    print(f"\n共 {len(rows)} 条待迁移")
    print(f"  按账号 : {by_user}")
    print(f"  按表页 : {by_sheet}")
    print(f"  按类型 : {by_type}")

    if not args.commit:
        print("\n预览结束，没有写入任何东西。")
        print("确认无误后运行：python3 d1/migrate_to_d1.py --commit\n")
        return

    print("\n开始写入 …")
    saved = skipped = failed = 0
    first_error = ""
    for sheet_name, payload in rows:
        body = {"type": payload["item"]["type"], **payload}
        try:
            r = post(base, token, body)
            status = r.get("status")
            if status == "saved":
                saved += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
                first_error = first_error or f"{payload['item']['title'][:24]} → {r.get('detail')}"
                print(f"  ✗ {payload['item']['title'][:24]} → {r.get('detail')}")
        except Exception as e:
            failed += 1
            first_error = first_error or str(e)
            print(f"  ✗ {payload['item']['title'][:24]} → {e}")

    print("\n" + "=" * 60)
    print(f"迁移完成：成功 {saved} · 已存在跳过 {skipped} · 失败 {failed}")
    print("=" * 60)
    if failed:
        # 说清楚到底是什么错，别让人对着一个 401 反复重跑。
        if "401" in first_error or "unauthorized" in first_error.lower():
            print("\n失败原因像是鉴权：检查 WORKER_TOKEN 是否和 wrangler secret 里设的一致。")
        else:
            print(f"\n第一条错误：{first_error}")
        print("重跑是安全的——id 幂等，已写入的会被跳过，不会产生重复行。")
    print("\n下一步：把 app.py 的 import 从 sheets_storage 换成 d1_storage。\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
