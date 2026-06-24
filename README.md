# BotMother

BotMother is a Python + SQLite Telegram mother bot that generates and runs other Telegram bots from user prompts.

The generated child bot is raw standalone Python. There is no user-facing schema, DSL, or generated function wrapper. BotMother strips accidental Markdown fences, validates the Python, writes `bot.py`, and runs it in a Bubblewrap sandbox on Ubuntu.

## Features

- Anyone chatting with the mother bot can create a child bot for testing.
- Users provide child bot tokens from `@BotFather`.
- Gemini code generation with `gemini-3.1-flash-lite`.
- SQLite state for users, bots, revisions, and recent logs.
- Commands for create, AI-guided prompt edit, ask, start, stop, restart, delete, status, and tail/logs.
- AI can ask follow-up questions before creating or editing a bot.
- AI runs a final essential-data readiness check before asking for the child bot token.
- AI runs several raw-Python refinement passes before deployment.
- Child bots are run as standalone Python subprocesses with only `BOT_TOKEN` and `BOT_DB_PATH` in their contract.

## Ubuntu Setup

```bash
sudo apt update
sudo apt install -y python3 python3-venv bubblewrap

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env

python -m botmother
```

Set these values in `.env`:

```env
MOTHER_BOT_TOKEN=123456:mother-token-from-botfather
GEMINI_API_KEY=your-gemini-key
GEMINI_MODEL=gemini-3.1-flash-lite
BOTMOTHER_DB=./data/botmother.sqlite3
BOTMOTHER_WORKDIR=./data/bots
OWNER_IDS=123456789
PYTHON_BIN=python3
BWRAP_BIN=bwrap
BOTMOTHER_REQUIRE_BWRAP=true
BOTMOTHER_LOG_LEVEL=INFO
BOTMOTHER_LOG_FILE=./data/botmother.log
```

For Ubuntu deployment, the simplest setting is the absolute venv interpreter path:

```env
PYTHON_BIN=/home/ubuntu/BotMother/.venv/bin/python
```

`PYTHON_BIN=/home/ubuntu/BotMother/.venv/bin/python3` also works when that file exists. Check with `ls -l .venv/bin/python*`. If `PYTHON_BIN=python3`, install the Python dependencies into the system Python environment visible inside Bubblewrap.

## Telegram Commands

- `/start` - show basic help.
- `/newbot` - create and launch a child bot; AI may ask follow-up questions first.
- `/bots` - list your bots. Owners see all bots.
- `/status [id]` - show one child bot status, or list your bots when no id is given.
- `/tail <id> [lines]` - show recent stdout/stderr lines, default 30 and max 100.
- `/logs <id> [lines]` - alias for `/tail`.
- `/ask <id> [question]` - ask the AI about a child bot using its saved prompt, status, source, env names, and recent logs.
- `/edit <id>` - describe a change in natural language; AI may ask follow-up questions first.
- `/stop <id>` - stop one child bot.
- `/restart <id>` - restart one child bot.
- `/delete <id>` - stop and soft-delete one child bot.
- `/revise <id>` - regenerate a child bot from a new prompt.
- `/killall` - owner-only emergency stop.
- `/cancel` - cancel an active create/revise flow.

Normal users can manage only bots they created. Owner IDs can manage every bot.

## Child Bot Contract

Generated child code must be one standalone Python file. It should read:

```python
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_DB_PATH = os.environ["BOT_DB_PATH"]
```

Allowed dependencies for generated child code:

- Python standard library
- `sqlite3`
- `python-telegram-bot`

If the AI asks for an external API key or config value, BotMother stores the supplied value as a per-bot environment variable and passes it to that child bot at runtime.

BotMother always injects `BOT_TOKEN`, `BOT_DB_PATH`, `PATH`, `PYTHONUNBUFFERED`, and `PYTHONIOENCODING` itself. The AI planner is told not to include those names in generated env values; if it does anyway, BotMother asks Gemini to repair the JSON response with the validation error before continuing.

## Security Notes

This is intentionally a testing-mode builder. Child tokens are stored plaintext in SQLite. Generated code is still dangerous by nature, so BotMother uses:

- Bubblewrap process isolation on Ubuntu.
- Syntax validation with `ast.parse`.
- A small denylist for obvious host-risk imports and calls.
- Per-bot work directories.
- Owner-only `/killall`.

The AST denylist is not a complete security boundary. Bubblewrap is the real isolation layer in this version.

## Run Tests

```bash
python -m unittest discover -s tests
```

On this Windows workspace, use:

```powershell
py -m unittest discover -s tests
```

## BotMother Logs

BotMother writes its own process logs to console and to `BOTMOTHER_LOG_FILE`, defaulting to `./data/botmother.log`.

```bash
tail -f ./data/botmother.log
```

Child bot stdout/stderr is still available through `/tail <id>`, `/logs <id>`, and per-bot files under `data/bots/<id>/`.

Negative child process return codes mean Linux signals. For example, `rc=-2` is `SIGINT`. BotMother logs signal names and will retry unexpected signal exits a bounded number of times.

## Prompt Editing

Use `/edit <id>`, then describe the change you want in normal language, for example `add a /help command` or `make the bot remember birthdays`. BotMother lets the AI ask follow-up questions, edits the existing generated Python internally, validates the new revision, saves it, and restarts the child bot when the edit is valid.

## AI Follow-Ups

For `/newbot` and `/edit`, BotMother asks Gemini for a strict JSON decision. The decision type is either `questions` or `code`. When the AI returns questions, BotMother sends Gemini's user-facing message directly, then sends the user's answer back into the next AI turn. The structured question fields are internal only, so BotMother does not add visible question numbers, suggestion labels, or follow-up counters. To avoid endless loops, BotMother allows up to 5 internal follow-up rounds, then forces a final code decision or ends the flow if the AI still cannot proceed safely.

After the normal `/newbot` questions, BotMother runs a separate readiness check before asking for the BotFather token. This check asks only for missing essential data needed to run the bot, such as required admin IDs, API keys, payment/contact details, or external service settings. It does not ask optional preference questions and it never asks for the Telegram token.

Before saving and launching generated code, BotMother runs 3 AI refinement layers. Each layer must return raw standalone Python. BotMother validates each candidate and keeps the last valid version, so a bad refinement pass cannot overwrite a deployable previous pass.

Planner JSON repair is also bounded. If Gemini returns invalid JSON or tries to set reserved runtime env vars such as `BOT_TOKEN`, BotMother sends the validation error back to Gemini for up to 2 repair attempts, then falls back to asking the user to restate the request.
