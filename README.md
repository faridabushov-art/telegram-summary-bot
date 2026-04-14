# Telegram Group Summary Bot — LangGraph ReAct Agent Edition

A Telegram bot that silently listens to a group chat between sales staff and ERP admin staff. It uses a **LangGraph ReAct managed agent** to process every incoming message — deciding autonomously how to handle text, transcribe voice, and describe images.

---

## What Changed vs. Raw API Version

This version uses a **LangGraph ReAct managed agent** instead of direct function calls. The agent (in `agent.py`) autonomously selects tools based on message type. You can extend it by adding new `@tool` functions — no changes to `handlers.py` needed.

```
[Telegram Group]
      │ message event (text / voice / photo / document)
      ▼
[python-telegram-bot — polling]
      │ thin handler: extract raw bytes + metadata
      ▼
[LangGraph ReAct Agent]          ← managed agent loop lives here
      │ reasons about message type
      │ selects and calls tools autonomously
      ├── tool: store_message(...)
      ├── tool: transcribe_voice(...)
      ├── tool: describe_image(...)
      ├── tool: get_history(...)
      └── tool: build_summary(...)
      │
      ▼
[SQLite — messages.db]           ← single source of truth
```

---

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure your environment:**
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and fill in your three API keys:
   - `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather) on Telegram
   - `ANTHROPIC_API_KEY` — from [console.anthropic.com](https://console.anthropic.com)
   - `OPENAI_API_KEY` — from [platform.openai.com/api-keys](https://platform.openai.com/api-keys)

3. **Run the bot:**
   ```bash
   python main.py
   ```

---

## Adding the Bot to Your Group

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → copy the token
2. Add the bot to your Telegram group
3. Promote it to **Admin** so it can read all messages
   - By default, Telegram bots only receive messages starting with `/` (Privacy Mode)
   - Making it an admin bypasses Privacy Mode — or disable it via `/setprivacy` in BotFather
4. Done — it listens silently

---

## Commands

| Command | Description |
|---------|-------------|
| `/summary` | Agent retrieves history and generates a structured recap |
| `/clear` | Wipes the log for this chat |
| `/start` | Shows help message |

**Tip:** Run `/clear` at the start of each work day or topic, then `/summary` at the end for a clean recap.

---

## Extending the Bot

To add a new capability (e.g. detect urgent requests and alert a manager):

1. Add a new `@tool` function in `agent.py` with a clear docstring
2. Append it to the `tools` list in `agent.py`
3. Restart the bot — the agent will use it automatically when appropriate

No changes to `handlers.py`, `storage.py`, or `main.py` are needed.

---

## Deploying to Railway

Railway runs the bot in a Docker container. Because Railway's filesystem is
ephemeral (wiped on every redeploy), the SQLite database must live on a
**persistent Volume** mounted at `/data`. The `railway.toml` and `Dockerfile`
included in this repo handle that automatically — you just need to create the
Volume and set your environment variables.

### Step 1 — Push your code to GitHub

Railway deploys from a Git repository.

```bash
git init
git add .
git commit -m "initial commit"
# create a repo on GitHub, then:
git remote add origin https://github.com/YOUR_USERNAME/telegram-summary-bot.git
git push -u origin main
```

> Make sure `.env` is in `.gitignore` — never commit real secrets.

### Step 2 — Create a Railway project

1. Go to [railway.app](https://railway.app) and sign in
2. Click **New Project → Deploy from GitHub repo**
3. Select your `telegram-summary-bot` repository
4. Railway detects the `Dockerfile` automatically — click **Deploy**
   (the first deploy will fail because env vars aren't set yet — that's fine)

### Step 3 — Add a persistent Volume

The bot stores `messages.db` at `/data/messages.db`. Without a Volume this
file is lost on every redeploy.

1. In your Railway project, click your service
2. Go to **Settings → Volumes → Add Volume**
3. Set **Mount Path** to `/data`
4. Click **Add** — Railway creates and attaches the volume immediately

### Step 4 — Set environment variables

1. In your service, go to **Variables**
2. Add each of these:

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | your token from @BotFather |
   | `ANTHROPIC_API_KEY` | your key from console.anthropic.com |
   | `OPENAI_API_KEY` | your key from platform.openai.com |
   | `SUMMARY_LANGUAGE` | `English` (or any language you prefer) |

   > No `DB_PATH` variable needed — it defaults to `/data/messages.db`.

3. Railway automatically triggers a redeploy after you save variables

### Step 5 — Verify the deployment

1. Go to **Deployments** and watch the build log
2. Once status shows **Active**, click **View Logs**
3. You should see:
   ```
   Database initialised.
   Bot is running. Press Ctrl+C to stop.
   ```
4. Send `/start` to your bot in Telegram to confirm it responds

### Redeploying after a code change

```bash
git add .
git commit -m "your change"
git push
```

Railway picks up the push automatically and rebuilds the image. The `/data`
volume persists across all redeploys, so your message history is safe.

### Monitoring & logs

- **Live logs**: Railway dashboard → your service → **Logs** tab
- **Crash restarts**: `railway.toml` sets `restartPolicyType = "on_failure"`
  with up to 10 retries, so transient errors self-recover

---

## File Structure

```
telegram-summary-bot/
├── CLAUDE.md          ← specification
├── .env               ← secrets (never commit)
├── .env.example       ← safe template to commit
├── requirements.txt   ← Python dependencies
├── Dockerfile         ← production container image
├── .dockerignore      ← excludes secrets + cache from image
├── railway.toml       ← Railway build/deploy/volume config
├── main.py            ← entry point, registers handlers
├── handlers.py        ← thin Telegram event handlers
├── agent.py           ← LangGraph ReAct agent + all tools
├── storage.py         ← async SQLite helpers (aiosqlite)
└── README.md          ← this file
```

---

## Privacy Note

All messages are stored locally in `messages.db` (SQLite) in the same folder as the bot. Data is sent to external APIs only for processing:
- **Anthropic API** — conversation summarization and image description (`claude-haiku-4-5`)
- **OpenAI API** — voice transcription (`whisper-1`)

Review your data agreements with Anthropic and OpenAI before deploying in privacy-sensitive environments.
