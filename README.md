# BotMother

BotMother is a Python + SQLite Telegram mother bot that generates and runs other Telegram bots from user prompts.

The generated child bot is raw standalone Python. There is no user-facing schema, DSL, or generated function wrapper. BotMother strips accidental Markdown fences, validates the Python, writes `bot.py`, and runs it in a Bubblewrap sandbox on Ubuntu.

## Features

- Anyone chatting with the mother bot can create a child bot for testing.
- Users provide child bot tokens from `@BotFather`.
- Gemini code generation with `gemini-3.1-flash-lite`.
- SQLite state for users, bots, revisions, and recent logs.
- Commands for create, examples/help, AI-guided prompt edit, ask, start, stop, restart, delete, status, identity, health, and tail/logs.
- Button-first manager UX with a persistent reply keyboard and inline bot action buttons.
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

## Telegram Controls

BotMother is designed as a tap-first manager. `/start` removes the old persistent reply keyboard and opens an inline home menu. Bot lists, bot details, confirmations, help categories, and setup results use inline buttons so users normally do not need to type bot IDs.

Inline actions:

- `🪄 New Bot` - create and launch a child bot; AI may ask follow-up questions first.
- `📦 My Bots` - list your bots and open per-bot action buttons.
- `📊 Status` - choose a bot and inspect process status.
- `💬 Ask Bot` - choose a bot and ask AI about its prompt, source, env names, status, and recent logs.
- `✏️ Edit Bot` - choose a bot and describe a change in normal language.
- `♻️ Revise` - choose a bot and regenerate it from a fresh prompt.
- `🧾 Logs` - choose a bot and view recent stdout/stderr.
- `🔄 Restart`, `🛑 Stop`, `🗑️ Delete` - choose a bot, then run the operation.
- `✨ Examples`, `🪪 Profile`, `🩺 Health`, `🌐 Language`, `📚 Help`, `❌ Cancel` - open examples, full Telegram profile/chat info, health, language settings, category help, or leave the current flow.

The Help menu opens category screens with inline buttons for Create, Manage, Operations, Utilities, and command fallbacks. Bot-specific screens include action buttons only when they are useful.

Typed commands remain available as a fallback and for power users:

- `/start` - show basic help.
- `/help`, `/commands`, `/usage` - show the category help menu.
- `/examples` - show copy-ready bot prompt examples.
- `/language` - choose English or Myanmar for BotMother menus and messages.
- `/newbot` - create and launch a child bot.
- `/bots` - list your bots. Owners see all bots.
- `/status [id]` - show one child bot status, or open the bot list when no id is given.
- `/tail [id] [lines]` - show recent stdout/stderr lines, default 30 and max 100; opens a picker when no id is given.
- `/logs [id] [lines]` - alias for `/tail`.
- `/ask [id] [question]` - ask the AI about a child bot; opens a picker when no id is given.
- `/edit [id]` - describe a change in natural language; opens a picker when no id is given.
- `/stop [id]` - stop one child bot; opens a picker when no id is given.
- `/restart [id]` - restart one child bot; opens a picker when no id is given.
- `/delete [id]` - stop and soft-delete one child bot; opens a picker when no id is given.
- `/revise [id]` - regenerate a child bot from a new prompt; opens a picker when no id is given.
- `/id`, `/whoami` - show your full Telegram user/chat info for admin configuration.
- `/health` - show manager and child-process health summary.
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

Generated child bots should prefer Telegram-native controls over command-heavy text flows:

- `ReplyKeyboardMarkup` for persistent main menus and common user actions.
- `InlineKeyboardMarkup` for choices, confirmations, product/item selection, pagination, admin actions, and next-step navigation.
- Avoid asking users to type IDs, option names, or command syntax when a button can represent the choice.
- Slash commands should remain as fallback entry points, but primary workflows should be tappable.

Generated child bots must register a global `application.add_error_handler(...)`. The validator rejects generated code that does not include one, so deployed bots have a fallback path for unexpected handler errors.

For Telegram formatting, generated bots should prefer `ParseMode.HTML` and escape dynamic values with `html.escape`. If a child bot uses MarkdownV2, it must escape dynamic values with `telegram.helpers.escape_markdown(value, version=2)`. This avoids broken Telegram Markdown/HTML rendering from unescaped user content.

If the AI asks for an external API key or config value, BotMother stores the supplied value as a per-bot environment variable and passes it to that child bot at runtime.

BotMother always injects `BOT_TOKEN`, `BOT_DB_PATH`, `PATH`, `PYTHONUNBUFFERED`, and `PYTHONIOENCODING` itself. The AI planner is told not to include those names in generated env values; if it does anyway, BotMother asks Gemini to repair the JSON response with the validation error before continuing.

## Localization

Manager UI text is loaded from JSON locale files under `botmother/locales/`. English and Myanmar are included:

```text
botmother/locales/en.json
botmother/locales/my.json
```

Generated bots default to English unless the user explicitly asks for another language or multilingual support. BotMother no longer asks a separate localization question before planning.

Users can change the manager language from the `🌐 Language` inline button or `/language`. The choice is stored per Telegram user in SQLite and overrides Telegram's `language_code`.

## Security Notes

This is intentionally a testing-mode builder. Child tokens are stored plaintext in SQLite. Generated code is still dangerous by nature, so BotMother uses:

- Bubblewrap process isolation on Ubuntu.
- Syntax validation with `ast.parse`.
- A small denylist for obvious host-risk imports and calls.
- Static checks for obvious generated-code bugs, including a lightweight AST pass and a `mypy` pass when installed.
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

Child bot stdout/stderr is still available through the Logs action, `/tail`, `/logs`, and per-bot files under `data/bots/<id>/`.

Negative child process return codes mean Linux signals. For example, `rc=-2` is `SIGINT`. BotMother logs signal names and will retry unexpected signal exits a bounded number of times.

## Prompt Editing

Tap `✏️ Edit Bot`, choose the bot, then describe the change you want in normal language, for example `add a help menu with buttons` or `make the bot remember birthdays`. BotMother lets the AI ask follow-up questions, edits the existing generated Python internally, validates the new revision, saves it, and restarts the child bot when the edit is valid.

## Templates, Dashboard, And Auto Fix

New Bot starts with a mode picker: Shop, Booking, Support, Quiz, Channel, or Other. The selected mode is added as hidden planning context, so users still describe the bot naturally while Gemini gets a stronger product starting point.

Each child bot page now acts as a dashboard with username, status, process state, PID, owner, revision count, env var names, validation summary, and the latest issue from recent logs. The `🧪 Validation` action shows the syntax, security, static AST, Telegram hook, and mypy layers.

Use `🛠️ Auto Fix` or `/fix <id>` when a bot has an error. BotMother sends the latest source, original prompt, validation report, env names, status, and recent logs to the AI as a targeted edit request, then validates and restarts the bot through the normal edit pipeline.

## AI Follow-Ups

For New Bot and Edit Bot, BotMother asks Gemini for a strict JSON decision. The decision type is either `questions` or `code`. BotMother includes useful Telegram user/chat context, such as user ID, username, names, language code, chat ID, and chat type, so Gemini can make better defaults without asking for basic identity details. When the AI returns questions, BotMother sends Gemini's user-facing message directly, then sends the user's answer back into the next AI turn. The structured question fields are internal only, so BotMother does not add visible question numbers, suggestion labels, or follow-up counters. To avoid endless loops, BotMother allows up to 5 internal follow-up rounds, then forces a final code decision or ends the flow if the AI still cannot proceed safely.

After the normal `/newbot` questions, BotMother runs a separate readiness check before asking for the BotFather token, including after follow-up answers. This check asks only for missing essential data needed to run the bot, such as required admin IDs, API keys, payment/contact details, or external service settings. It does not ask optional preference questions and it never asks for the Telegram token.

Before saving and launching generated code, BotMother runs bounded AI refinement layers. Each layer must return raw standalone Python. BotMother validates each candidate with syntax, denylist, static AST, required Telegram UX hooks, and `mypy` checks, then keeps the last valid version, so a bad refinement pass cannot overwrite a deployable previous pass.

Planner JSON repair is also bounded. If Gemini returns invalid JSON or tries to set reserved runtime env vars such as `BOT_TOKEN`, BotMother sends the validation error back to Gemini for up to 2 repair attempts, then falls back to asking the user to restate the request.
