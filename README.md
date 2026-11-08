# Promo Finder — Kuku PS-1

Reads a microdrama's screenplays and finds the best 20–60s promo clips, giving the episode, exact start and end timestamps for each segment, the hook, the cliffhanger, why it works, and a ranked posting order.

## How it works

```
screenplay .txt ──► parser (code) ──► rule signals (code) ──► candidates (LLM, 3 eps/call)
                                                              │
       ranked promos ◄── editor posting plan (LLM) ◄── dedupe + diversity rank (code)
                                                     ◄── validate & snap to real blocks (code)
```

| Step | Who | Why |
|---|---|---|
| Parse every CAMERA / ACTION / SFX / dialogue block with timestamps | code | Deterministic. Every output timestamp comes from a parsed block. |
| Hook / cliffhanger keyword + emotion-tag + SFX signals | code | Free, explainable, and a second opinion on the LLM |
| Propose promos and refer to moments by **block index only** | LLM | Judging drama is the LLM's strength. It never writes timestamps, so it can't invent them. |
| Validate: index exists, dead air trimmed so the hook lands in 3s, 20–60s length, snap cuts to shot boundaries | code | Invalid picks are rejected and logged, not silently fixed |
| Score = LLM hook/cliff/standalone − spoiler risk + rule signals + early-episode bonus | code | The formula is transparent and tunable |
| Dedupe overlaps, soft diversity penalty for repeated tropes and episodes | code | Keeps the top 10 from being five takes on one scene |
| Posting plan for launch week | LLM | Answers "which would you post first, and why" |
| Rules vs LLM disagreement list | code | Tells the human editor which picks to double-check |

A **rules-only mode** runs with no API key. It's the baseline to compare the LLM against.

## Run locally

```bash
uv venv && uv pip install -r requirements.txt     # or: python -m venv .venv && pip install -r requirements.txt
cp .env.example .env    # then put your key(s) in .env
./start.sh cli --data data/broken-soldier-duke --mode llm   # CLI (writes runs/<run_id>/)
./start.sh              # web app on http://localhost:8000
```

Put the input pack's episode `.txt` files in `data/<show-name>/`, or upload a folder or `.zip` from the web UI.

## Swap the model

All settings live in `.env` (copy `.env.example`). `./start.sh` and the CLI load it automatically, so you never need to `export`. Real environment variables, such as Railway Variables, override the file.

The tool talks to **any OpenAI-compatible Chat Completions API** through three settings: `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`. Switching provider or model means changing those, with no code change.

| Provider | `LLM_BASE_URL` | `LLM_MODEL` | Notes |
|---|---|---|---|
| Groq | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` | free; add `LLM_TPM=8000`, `LLM_CONCURRENCY=1` |
| AWS Bedrock | `https://bedrock-runtime.<region>.amazonaws.com/openai/v1` | `openai.gpt-oss-120b-1:0` | key = Bedrock API key; ~$0.03 per 50-episode run |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | a Gemini model | free tier |

```bash
./start.sh                                   # web app
./start.sh cli --data data/broken-soldier-duke --mode llm   # CLI
```

Responses are cached on disk under `.cache/llm/`, keyed by model and prompt. Re-runs are free, and a run that hits Groq's daily cap resumes where it stopped.

**Free tier budget.** Groq `openai/gpt-oss-120b` allows 8K tokens/min and 200K/day. A 50-episode show is about 45K tokens of compact script, sent in 17 calls, roughly 90K tokens and 12 minutes. The client throttles itself to `LLM_TPM`.

## Deploy on Railway

1. Push this repo to GitHub, or run `railway up` from this folder.
2. Railway builds the `Dockerfile` (config in `railway.json`, healthcheck `/health`).
3. Set the variables `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` (same as your `.env`).
4. *(Optional)* Add a volume mounted at `/app/runs` so runs survive redeploys.

Boot: `start.sh` prints a banner and starts uvicorn right away. Warm-up (model config, dataset discovery, parsing, signals, run index) runs in a background thread, and the page shows a boot loader with each step's progress. `/health` returns 200 straight away, so Railway's healthcheck never races the warm-up.

> **Data confidentiality.** `data/` is in `.gitignore` because the input pack is Kuku's confidential content. On a deploy from a public repo, upload the show from the UI instead. If your repo is **private** and you want the show bundled, remove `data/` from `.gitignore`.

## Outputs (`runs/<run_id>/`)

- `report.md`: ranked promos, posting plan and quality summary, readable by people
- `promos.json` / `promos.csv`: full detail, including the script lines inside each cut
- `quality.json`: validity rate, rejections with reasons, coverage, flags, rules-vs-LLM disagreements
- `candidates_raw.json`, `llm_responses.json`: unedited model output

## Evaluate against hand-picked promos

Label promos yourself first, without looking at the tool's output (see `eval/gold.example.json`). Then run:

```bash
python -m promo_finder.evaluate --run runs/<run_id> --gold eval/gold.json
```

It reports recall, top-10 precision against your picks, and mean start and end offsets in seconds.
