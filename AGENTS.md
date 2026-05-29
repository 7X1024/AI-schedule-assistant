# AGENTS.md — AI Schedule Assistant

## Stack
Python 3.11+, Streamlit, DeepSeek V4 Flash (`openai` SDK), Pydantic v2, Pandas, Google Sheets (via `gspread`).

## Commands
```bash
streamlit run app.py              # dev server (http://localhost:8501)
pip install -r requirements.txt   # install deps
```

## API Key / Secrets
- Streamlit Cloud: Secrets → `DEEPSEEK_API_KEY`, `GOOGLE_SHEETS_ID`, `gcp` (full service-account JSON)
- Local: `.streamlit/secrets.toml`

```toml
DEEPSEEK_API_KEY = "sk-..."

GOOGLE_SHEETS_ID = "1a2b3c4d..."

[gcp]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = """-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"""
client_email = "...@....iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
```
- Base URL: `https://api.deepseek.com`, model: `deepseek-v4-flash`

## Architecture
```
app.py              — UI (3-column: input | events | todos)
models.py           — ScheduleItem Pydantic model
ai_parser.py        — DeepSeek call + JSON parse + Pydantic validation
sheets_storage.py   — Google Sheets CRUD (primary, append-only writes)
storage.py          — Local JSON CRUD (fallback, not used by default)
```

## Data Model
```python
class ScheduleItem(BaseModel):
    id: str
    type: "event"|"todo"
    title: str
    date: str | None
    start_time: str | None
    end_time: str | None
    deadline: str | None
    location: str | None
    time_period: "morning"|"noon"|"afternoon"|"evening"|"night" | None
    priority: "low"|"medium"|"high"
    source_text: str
    confidence: float = 0.0
    needs_confirmation: bool = True
    is_completed: bool = False
    deleted: bool = False
    created_at: str | None = None
    updated_at: str | None = None
```

## Storage Rules
1. **Append-only writes**: `save_event/todo` only appends rows — never clears or overwrites.
2. **Soft delete**: `delete_event/todo` sets `deleted=TRUE` on the existing row — never removes rows.
3. **Cell-level updates**: `toggle_todo` updates only `completed` + `updated_at` cells by id lookup.
4. `loaded_events/todos` filters out `deleted=TRUE` rows.
5. `_ensure_worksheet` creates missing sheets and writes headers only if empty — never clears data.

## UI Rules
- 3-column desktop, stacked on mobile (CSS media query at 768px)
- Cards with subtle shadows, 12px radius, generous padding
- Priority colors: high=red, medium=amber, low=gray
- Empty states show friendly messages, not blank panels
- No default Streamlit look; target Notion/Linear/TickTick aesthetic

## After Every Change
State: which files changed, why, what was accomplished.

## 修改时的注意事项
不许重写整个代码，哪些语句有问题就改哪些