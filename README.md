# GenX Fullstack Technical Assessment

| Assignment | Folder | Write-up |
|---|---|---|
| Python Backend: webhook ingestion service (FastAPI, SQLite) | [`backend/`](backend/) | [`backend/DECISIONS.md`](backend/DECISIONS.md) |
| React Frontend: event log & live AI summary (Next.js App Router) | [`frontend/`](frontend/) | [`frontend/DECISIONS.md`](frontend/DECISIONS.md) |

Each folder keeps the original README with setup steps, plus a "Solution overview" section.

## Quick start

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -v -s

# Frontend
cd frontend
npm install
npm run dev            # http://localhost:3000
```

The AI-assisted development chat history is provided separately as a PDF, as the
assignment requires.
