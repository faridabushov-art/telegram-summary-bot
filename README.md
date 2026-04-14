# Telegram Group Summary Bot — LangGraph ReAct Agent Edition

A Telegram bot that silently listens to a group chat between sales staff and ERP admin staff. Uses a **LangGraph ReAct managed agent** to process every incoming message — handling text, transcribing voice, and describing images. Run `/summary` to get a structured recap.

---

## Architecture

```
[Telegram Group]
      │ message event (text / voice / photo / document)
      ▼
[python-telegram-bot — polling]
      │ thin handler: extract raw bytes + metadata
      ▼
[LangGraph ReAct Agent]          ← managed agent loop
      ├── tool: store_message(...)
      ├── tool: transcribe_voice(...)
      ├── tool: describe_image(...)
      ├── tool: get_history(...)
      └── tool: build_summary(...)
      ▼
[SQLite — /data/messages.db]     ← single source of truth
```

---

## Setup (local)

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Fill in your keys
   ```

3. **Run:**
   ```bash
   python main.py
   ```

---

## Deploy to Railway

1. Push this repo to GitHub
2. Create a new Railway project → Deploy from GitHub repo
3. Add a Volume → mount path `/data`
4. Set environment variables:
   - `TELEGRAM_BOT_TOKEN`
   - `ANTHROPIC_API_KEY`
   - `OPENAI_API_KEY`
   - `SUMMARY_LANGUAGE` = `English`

---

## Bot Setup (IMPORTANT)

1. Create bot via [@BotFather](https://t.me/BotFather) → `/newbot`
2. **Disable Privacy Mode:** `/mybots` → your bot → Bot Settings → Group Privacy → **Turn off**
3. Add bot to your Telegram group
4. Send `/start` to verify it responds

> Without disabling Privacy Mode, the bot only receives `/commands` and misses all regular messages.

---

## Commands

| Command | Description |
|---------|-------------|
| `/summary` | Generate structured recap of the conversation |
| `/clear` | Wipe the log and start fresh |
| `/start` | Show help |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `OPENAI_API_KEY` | From platform.openai.com/api-keys |
| `SUMMARY_LANGUAGE` | Language for summaries (default: English) |
| `DB_PATH` | SQLite path (default: `/data/messages.db`) |

---

## Privacy Note

Messages are stored locally in SQLite. Data is sent to external APIs only for:
- **Anthropic API** — summarization + image description (`claude-haiku-4-5`)
- **OpenAI API** — voice transcription (`whisper-1`)
