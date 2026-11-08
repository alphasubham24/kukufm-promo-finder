# 🎬 Promo Finder

> **Give it a show's screenplay. Get back the best 20–60 second clips to post on TikTok, Reels & Shorts.**

Built for vertical microdrama — it reads episode scripts, finds the most scroll-stopping moments, and ranks them with a posting plan for launch week.

---

## ✨ What it does

```
📄 Screenplays  ──▶  🔍 Find hooks & cliffhangers  ──▶  🤖 LLM picks the best clips
                                                               │
📊 Ranked list  ◀──  🗓️ Posting plan for launch week  ◀──  ✅ Validate & score
```

| | |
|---|---|
| ⚡ **Hook detection** | Finds the lines that stop the scroll in the first 3 seconds |
| 🎭 **Cliffhanger scoring** | Cuts at unresolved moments — threat, reveal, unanswered question |
| 🧠 **LLM-powered** | Works with Groq, AWS Bedrock, Gemini — any OpenAI-compatible API |
| ✅ **Validated timestamps** | Every cut is snapped to real script blocks — no hallucinated times |
| 🗓️ **Launch week plan** | Suggests which clips to post first and why |
| 🌐 **Web UI + CLI** | Upload a zip from the browser, or run from terminal |

---

## 🚀 Quick Start

**1. Install dependencies**
```bash
pip install -r requirements.txt
```

**2. Add your API key**
```bash
cp .env.example .env
# Edit .env with your LLM provider key
```

**3. Run**
```bash
# Web app → open http://localhost:8000
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# CLI (saves results to runs/)
python -m promo_finder --data data/<show-name> --mode llm
```

---

## 🔑 Supported LLM Providers

Swap providers by changing 3 lines in `.env` — no code changes needed.

| Provider | Free Tier? |
|---|---|
| **Groq** | ✅ Yes — fast, generous free tier |
| **Google Gemini** | ✅ Yes — free tier available |
| **AWS Bedrock** | 💳 Pay-per-use (~\$0.03 / 50-episode show) |

See `.env.example` for the exact config values for each provider.

> 💡 Responses are cached locally — re-runs cost nothing, and interrupted runs resume where they stopped.

---

## 📦 Output files

After each run, results are saved to `runs/<run-id>/`:

| File | What's inside |
|---|---|
| `report.md` | 📋 Human-readable ranked promos + posting plan |
| `promos.json` | Full promo data with script lines |
| `promos.csv` | Spreadsheet-friendly version |
| `quality.json` | Stats — validity rate, coverage, flags |

---

## 🌐 Deploy to Railway

1. Push this repo to GitHub
2. Connect to [Railway](https://railway.app) — it auto-detects the `Dockerfile`
3. Set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` as environment variables
4. Done — the app is live with a `/health` endpoint ready

---

## 🧪 Evaluate accuracy

Compare the tool's picks against your own hand-picked promos:

```bash
python -m promo_finder.evaluate --run runs/<run-id> --gold eval/gold.json
```

See `eval/gold.example.json` for the format. It reports recall, precision, and timestamp accuracy.

---

## 🛠 Tech Stack

`Python` · `FastAPI` · `httpx` · `Pydantic` · `any OpenAI-compatible LLM`
