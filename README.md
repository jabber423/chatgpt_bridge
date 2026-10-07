# MuskazzeGPT Discord Bot

A Discord bot that answers direct mentions, optionally answers detected questions, and can run targeted troll-style banter. Requests use an AI planner to decide whether Discord history is needed, then retrieve the selected channels before answering.

## Setup

Requirements: Python 3.9 or newer, a Discord bot token with the required message-content access, and an OpenAI API key.

Install dependencies from the project root:

```powershell
python -m pip install -r requirements.txt openai
```

Set the required environment variables and start the bot:

```powershell
$env:MUSK_GPT = "<Discord bot token>"
$env:OPENAI_API_KEY = "<OpenAI API key>"
$env:MUSK_GPT_MODE = "mentioned"
python .\discord_gpt_bot_planner.py
```

Do not commit tokens or API keys. The bot reads environment variables from its process environment; it does not load a `.env` file itself.

## Environment Variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `MUSK_GPT` | Yes | None | Discord bot token. |
| `OPENAI_API_KEY` | Yes | None | OpenAI API key. |
| `OPENAI_MODEL` | No | `gpt-5.4` | Model used for answers, Q&A confidence checks, and troll replies. |
| `MUSK_GPT_PLANNER_MODEL` | No | Value of `OPENAI_MODEL` | Model used to choose Discord channels and retrieval settings. |
| `MUSK_GPT_MODE` | No | `mentioned` | Initial runtime mode flags. Values can be names or bitmasks; see [Modes](#modes). |
| `MUSK_GPT_MODE_ADMIN_USER_ID` | No | `604330313602170960` | Discord user ID allowed to change modes. |
| `MUSK_GPT_MODE_ADMIN_ROLE` | No | `Arbiter` | Discord role name allowed to change modes. Matching is case-insensitive. |
| `MUSK_GPT_MAX_HISTORY_PER_CHANNEL` | No | `150` | Upper bound on messages retrieved per channel. |
| `MUSK_GPT_ANSWER_HISTORY_LIMIT` | No | `50` | Q&A history limit; clamped to the maximum history setting. |
| `MUSK_GPT_MAX_CHANNELS_PER_QUERY` | No | `3` | Maximum number of channels the planner can select. |
| `MUSK_GPT_MAX_IMAGES` | No | `4` | Maximum number of retrieved images included in an answer request. |
| `MUSK_GPT_LOG_FILE` | No | `muskazze_gpt_log.jsonl` | Path to the JSON Lines interaction/error log. Relative paths are resolved from the process working directory. |
| `MUSK_GPT_ANSWER_PROMPT_FILE` | No | `answer_mode_prompt.txt` beside the script | Path to the shared answer instructions. The file is read for each answer request, so prompt edits take effect without restarting. |

## Modes

The startup mode defaults to `mentioned`. Mode names are case-insensitive. Legacy aliases `planner` and `answer` map to `mentioned` and `qa` respectively.

| Mode | Behavior |
| --- | --- |
| `mentioned` | Handle direct bot mentions and DMs. These replies are not confidence-gated. |
| `qa` | Detect question-like messages, ask the planner what history is needed, and reply only when the confidence check returns a confident answer. |
| `troll` | Generate short, playful banter only when the configured target member posts in the configured server. |

`both` enables `mentioned` and `qa`. Modes can also be combined with commas or pipes:

```text
MUSK_GPT_MODE=qa,mentioned
MUSK_GPT_MODE=mentioned|qa
```

Bitmask values are `1` for `mentioned`, `2` for `qa`, and `4` for `troll`; combine bits by adding them, such as `3` for `mentioned` plus `qa`. Troll mode needs a target, so configure it with the runtime command rather than only with a startup bitmask.

## Runtime Mode Commands

Only the configured admin user ID or a member with the configured Arbiter role can change runtime modes. A command can be sent with or without mentioning the bot; a role mention prefix is also accepted.

```text
enable qa mode
enable mentioned mode
enable qa,mentioned mode
enable qa,mentioned,troll mode muskazze
```

When `troll` is included with a target, the bot must find one unique member by exact case-insensitive display name, username, or global name. A Discord user mention can be used as an unambiguous target. The bot replies to that member's own messages, not every message that mentions their name.

If `troll` is enabled without a target, the bot clears any previous troll target, enables `qa` and `mentioned`, and reports that no name was provided. A target name without `troll` is rejected. To disable troll mode, enable the modes you want without `troll`, for example:

```text
enable qa,mentioned mode
```

Runtime changes are held in memory and reset to `MUSK_GPT_MODE` when the bot restarts. The target is also cleared on restart.

## Retrieval Notes

The planner receives the current question and the channels the bot can view, excluding the hardcoded `CHANNELS_TO_IGNORE` list in the Python file. It selects up to `MUSK_GPT_MAX_CHANNELS_PER_QUERY` channels. The Monday, Tuesday, Friday, and Sunday raid channels always have embed collection enabled when selected. The answer prompt is in `answer_mode_prompt.txt` by default and is loaded at request time.

## Logs

The console reports incoming messages, the selected route, the planner result, retrieved channels, and whether a reply was sent. Interactions and errors are appended as JSON Lines to `MUSK_GPT_LOG_FILE`.
