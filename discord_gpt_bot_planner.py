import os
import re
import json
import time
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from openai import OpenAI, RateLimitError


# ============================================================
# Configuration
# ============================================================

DISCORD_TOKEN = os.environ["MUSK_GPT"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")
PLANNER_MODEL = os.getenv("MUSK_GPT_PLANNER_MODEL", OPENAI_MODEL)


def parse_bot_modes(raw_value: str) -> set[str]:
    value = (raw_value or "mentioned").strip().lower()
    if not value:
        return {"mentioned"}

    mode_aliases = {
        "mentioned": "mentioned",
        "planner": "mentioned",
        "qa": "qa",
        "answer": "qa",
    }

    if value == "both":
        return {"mentioned", "qa"}

    if "|" in value or "," in value:
        mask_tokens = [
            part.strip()
            for part in re.split(r"[|,\s]+", value)
            if part.strip()
        ]
        if mask_tokens and all(
            re.fullmatch(r"[0-9]+", token)
            for token in mask_tokens
        ):
            mask = 0
            for token in mask_tokens:
                mask |= int(token, 0)
            modes = set()
            if mask & 1:
                modes.add("mentioned")
            if mask & 2:
                modes.add("qa")
            if modes:
                return modes

    try:
        mask = int(value, 0)
    except ValueError:
        mask = None

    if mask is not None:
        modes = set()
        if mask & 1:
            modes.add("mentioned")
        if mask & 2:
            modes.add("qa")
        if modes:
            return modes

    tokens = {
        token.strip()
        for token in re.split(r"[,|\s]+", value)
        if token.strip()
    }
    modes = {
        mode_aliases[token]
        for token in tokens
        if token in mode_aliases
    }
    if modes:
        return modes

    raise ValueError(
        "MUSK_GPT_MODE must be 'mentioned', 'qa', 'both', "
        "or a bitmask (1|2)"
    )


BOT_MODES = parse_bot_modes(os.getenv("MUSK_GPT_MODE", "mentioned"))
BOT_MODE = "both" if BOT_MODES == {"mentioned", "qa"} else next(iter(BOT_MODES))

# Safety bounds. The planner can ask for less, but never more.
MAX_HISTORY_PER_CHANNEL = int(
    os.getenv("MUSK_GPT_MAX_HISTORY_PER_CHANNEL", "150")
)
ANSWER_HISTORY_LIMIT = min(
    max(1, int(os.getenv("MUSK_GPT_ANSWER_HISTORY_LIMIT", "50"))),
    MAX_HISTORY_PER_CHANNEL,
)

MAX_CHANNELS_PER_QUERY = int(
    os.getenv("MUSK_GPT_MAX_CHANNELS_PER_QUERY", "3")
)

RAID_SIGNUP_CHANNELS = {
    "monday-raid",
    "tuesday-raid",
    "friday-raid",
    "sunday-raid",
}

CHANNELS_TO_IGNORE = {
    "👋welcome-page",
    "🌀role-assignments",
    "📣guild-notices",
    "bt-trash-farm",
    "gruuls-tk",
    "kara",
    "🕐late-absences",
    "▫softres-tokens",
    "🌀class-assignments",
    "🌀vanguards",
    "warlocks",
    "warriors",
    "priests",
    "rogues",
    "shamans",
    "druids",
    "hunter",
    "mages",
    "paladins",
    "⚔️raid-guides",
    "🔨📄crafting-requests",
    "🎵music",
    "🎥videos-and-clips",
    "🔍lfg-arena-help-requests",
    "💩shitlist",
    "🛑📜officer-threads",
    "anime",
    "🍖food",
    "crit-healers",
}

MAX_IMAGES = int(
    os.getenv("MUSK_GPT_MAX_IMAGES", "4")
)

LOG_FILE = Path(
    os.getenv("MUSK_GPT_LOG_FILE", "muskazze_gpt_log.jsonl")
)
ANSWER_PROMPT_FILE = Path(
    os.getenv(
        "MUSK_GPT_ANSWER_PROMPT_FILE",
        str(Path(__file__).with_name("answer_mode_prompt.txt")),
    )
)
def load_answer_instructions() -> str:
    return ANSWER_PROMPT_FILE.read_text(encoding="utf-8")


# ============================================================
# OpenAI / Discord setup
# ============================================================

client_ai = OpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

discord_client = discord.Client(intents=intents)
request_lock = asyncio.Lock()


PLANNER_INSTRUCTIONS = """
You are the Discord retrieval planner for MuskazzeGPT.

Your ONLY job is to decide whether answering the user's current Discord message
requires retrieving Discord channel history.

You will receive:
- the user's exact question
- the current channel name
- a list of Discord text channels available to the bot

Return ONLY valid JSON. No Markdown. No explanation outside the JSON.

JSON format:
{
  "needs_discord": true or false,
  "channels": [
    {"name": "channel-name", "history": 10}
  ],
    "include_embeds": true or false,
  "include_images": true or false,
  "reason": "short reason"
}

Rules:
1. If the message can be answered normally without server-specific information,
   set needs_discord=false and channels=[].
   Examples:
   - "say hi"
   - "what is 2+2"
   - "write a funny raid message"
   - "what does this WoW ability do"

2. Retrieve Discord history only when the answer depends on information in the
   server, such as:
   - rosters
   - attendance
   - signups
   - absences
   - who said what
   - previous discussions
   - raid scheduling
   - comparing prior raid events
   - "check the history"

3. Select ONLY channels likely to contain the needed information.
   Do not query every channel by default.

4. history means the number of MOST RECENT messages to retrieve from that
   channel. Use the smallest amount likely to answer correctly.
   Typical values:
   - The history can be short for the raid sign up channels, we don't talk much there - 10
   - history could be longer if a really deep type search was needed. Like "generate a roster" you would need deep history from all channels - 50 or 100

5. Set include_embeds=true when embed contents are relevant. Always set it true
    when selecting monday-raid, tuesday-raid, sunday-raid, or friday-raid; Raid-Helper
    signups are commonly stored in embeds.

6. Set include_images=true only when screenshots/images are likely relevant.
    Discord embeds such as Raid-Helper rosters are parsed as text and do NOT
    require include_images=true.

7. Channel names MUST come from the provided available-channel list.

8. If the user's current channel alone is likely enough, select only that
   channel.

9. Never request more than 3 channels.
"""

# ============================================================
# Console / file logging
# ============================================================

def console_log(message: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def write_log(record: dict):
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        console_log(f"WARNING: Could not write log: {e}")


def log_interaction(message, question: str, answer: str, plan: dict):
    write_log({
        "type": "interaction",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "guild": message.guild.name if message.guild else "DM",
        "guild_id": message.guild.id if message.guild else None,
        "channel": getattr(message.channel, "name", "DM"),
        "channel_id": message.channel.id,
        "user": message.author.display_name,
        "user_id": message.author.id,
        "question": question,
        "plan": plan,
        "answer": answer,
    })


def log_error(message, error):
    write_log({
        "type": "error",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "guild": message.guild.name if message.guild else "DM",
        "guild_id": message.guild.id if message.guild else None,
        "channel": getattr(message.channel, "name", "DM"),
        "channel_id": message.channel.id,
        "user": message.author.display_name,
        "user_id": message.author.id,
        "error_type": type(error).__name__,
        "error": repr(error),
    })


# ============================================================
# Discord ID decoding
# ============================================================

async def resolve_discord_user(guild, user_id: int) -> str:
    if not guild:
        return str(user_id)

    member = guild.get_member(user_id)
    if member:
        return member.display_name

    try:
        member = await guild.fetch_member(user_id)
        return member.display_name
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return str(user_id)


async def decode_discord_ids(text: str, guild) -> str:
    if not guild or not text:
        return text

    # <@123> / <@!123>
    mention_ids = set(re.findall(r"<@!?(\d{17,20})>", text))

    for raw_id in mention_ids:
        try:
            name = await resolve_discord_user(guild, int(raw_id))
            if name != raw_id:
                text = re.sub(
                    rf"<@!?{re.escape(raw_id)}>",
                    name,
                    text
                )
        except ValueError:
            pass

    # Bare user IDs
    bare_ids = set(re.findall(r"\b\d{17,20}\b", text))

    for raw_id in bare_ids:
        try:
            name = await resolve_discord_user(guild, int(raw_id))
            if name != raw_id:
                text = text.replace(raw_id, name)
        except ValueError:
            pass

    return text


# ============================================================
# Discord content extraction
# ============================================================

def is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = attachment.content_type or ""

    if content_type.startswith("image/"):
        return True

    return attachment.filename.lower().endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif")
    )


async def collect_channel_history(
    channel,
    guild,
    limit: int,
    before=None,
    collect_embeds=False,
    collect_images=False,
):
    messages = []
    images = []

    try:
        async for msg in channel.history(limit=limit, before=before):
            parts = []

            decoded_content = await decode_discord_ids(
                msg.content,
                guild
            )

            if decoded_content:
                parts.append(decoded_content)

            # Parse Discord embeds, including Raid-Helper.
            for embed in msg.embeds:
                if collect_embeds:
                    embed_parts = []

                    if embed.author and embed.author.name:
                        value = await decode_discord_ids(
                            embed.author.name,
                            guild
                        )
                        embed_parts.append(f"Embed author: {value}")

                    if embed.title:
                        value = await decode_discord_ids(
                            embed.title,
                            guild
                        )
                        embed_parts.append(f"Embed title: {value}")

                    if embed.description:
                        value = await decode_discord_ids(
                            embed.description,
                            guild
                        )
                        embed_parts.append(f"Embed description: {value}")

                    for field in embed.fields:
                        field_name = await decode_discord_ids(
                            field.name or "",
                            guild
                        )

                        field_value = await decode_discord_ids(
                            field.value or "",
                            guild
                        )

                        embed_parts.append(
                            f"{field_name}: {field_value}"
                        )

                    if embed.footer and embed.footer.text:
                        value = await decode_discord_ids(
                            embed.footer.text,
                            guild
                        )
                        embed_parts.append(f"Footer: {value}")

                    if embed_parts:
                        parts.append(
                            "[DISCORD EMBED]\n"
                            + "\n".join(embed_parts)
                            + "\n[/DISCORD EMBED]"
                        )

                if collect_images:
                    if embed.image and embed.image.url:
                        images.append({
                            "timestamp": msg.created_at,
                            "url": embed.image.url,
                            "channel": getattr(channel, "name", "DM"),
                            "filename": "embed-image",
                        })

                    if embed.thumbnail and embed.thumbnail.url:
                        images.append({
                            "timestamp": msg.created_at,
                            "url": embed.thumbnail.url,
                            "channel": getattr(channel, "name", "DM"),
                            "filename": "embed-thumbnail",
                        })

            # Normal attachments.
            if collect_images:
                attachment_names = []

                for attachment in msg.attachments:
                    if is_image_attachment(attachment):
                        attachment_names.append(
                            attachment.filename
                        )

                        images.append({
                            "timestamp": msg.created_at,
                            "url": attachment.url,
                            "channel": getattr(channel, "name", "DM"),
                            "filename": attachment.filename,
                        })

                if attachment_names:
                    parts.append(
                        f"[Attached images: {', '.join(attachment_names)}]"
                    )

            if not parts:
                continue

            messages.append({
                "timestamp": msg.created_at,
                "channel": getattr(channel, "name", "DM"),
                "author": msg.author.display_name,
                "text": "\n".join(parts),
            })

    except discord.Forbidden:
        console_log(
            f"RETRIEVAL: skipping #{getattr(channel, 'name', channel.id)} "
            f"(missing permission)"
        )

    except discord.HTTPException as e:
        console_log(
            f"RETRIEVAL: skipping #{getattr(channel, 'name', channel.id)} "
            f"({e})"
        )

    return messages, images


# ============================================================
# AI planner
# ============================================================

def strip_json_fences(text: str) -> str:
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    return text.strip()


QUESTION_PREFIX_PATTERN = re.compile(
    r"^(?:how|what|why|when|where|who|whom|whose|which|"
    r"can|could|did|do|does|is|are|was|were|will|would|should|"
    r"have|has|had)\b",
    re.IGNORECASE,
)


def looks_like_question(text: str) -> bool:
    text = text.strip()
    return bool(text) and ("?" in text or QUESTION_PREFIX_PATTERN.search(text))

# Used to Ask AI what Discord data is needed to answer the user's question.
def ask_planner_sync(
    question: str,
    current_channel: str,
    available_channels: list[str],
) -> dict:
    planner_input = {
        "question": question,
        "current_channel": current_channel,
        "available_channels": available_channels,
    }

    response = client_ai.responses.create(
        model=PLANNER_MODEL,
        instructions=PLANNER_INSTRUCTIONS,
        input=[
            {
                "role": "user",
                "content": json.dumps(
                    planner_input,
                    ensure_ascii=False
                ),
            }
        ],
    )

    raw = strip_json_fences(response.output_text)
    return json.loads(raw)


def normalize_plan(
    plan: dict,
    current_channel: str,
    available_channels: list[str],
) -> dict:
    available_map = {
        name.lower(): name
        for name in available_channels
    }

    normalized = {
        "needs_discord": bool(plan.get("needs_discord", False)),
        "channels": [],
        "include_embeds": bool(plan.get("include_embeds", False)),
        "include_images": bool(plan.get("include_images", False)),
        "reason": str(plan.get("reason", ""))[:500],
    }

    if not normalized["needs_discord"]:
        return normalized

    seen = set()

    for requested in plan.get("channels", []):
        if not isinstance(requested, dict):
            continue

        raw_name = str(requested.get("name", "")).strip()
        matched_name = available_map.get(raw_name.lower())

        if not matched_name:
            continue

        if matched_name.lower() in seen:
            continue

        try:
            history = int(requested.get("history", 50))
        except (TypeError, ValueError):
            history = 50

        history = max(
            1,
            min(history, MAX_HISTORY_PER_CHANNEL)
        )

        normalized["channels"].append({
            "name": matched_name,
            "history": history,
        })

        seen.add(matched_name.lower())

        if len(normalized["channels"]) >= MAX_CHANNELS_PER_QUERY:
            break

    # If planner says Discord is required but gives no valid channel,
    # fall back to the current channel.
    if (
        normalized["needs_discord"]
        and not normalized["channels"]
        and current_channel.lower() in available_map
    ):
        normalized["channels"].append({
            "name": available_map[current_channel.lower()],
            "history": 50,
        })

    if any(
        item["name"].lower() in RAID_SIGNUP_CHANNELS
        for item in normalized["channels"]
    ):
        normalized["include_embeds"] = True

    return normalized

# This flow asks OpenAI what Discord data is needed to answer the user's question, and returns a normalized plan.
async def create_retrieval_plan(message, question: str) -> dict:
    current_channel = getattr(message.channel, "name", "DM")

    if message.guild is None:
        available_channels = [current_channel]
    else:
        available_channels = [
            channel.name
            for channel in message.guild.text_channels
            if channel.permissions_for(message.guild.me).view_channel
            and channel.name.casefold() not in CHANNELS_TO_IGNORE
        ]

    console_log("PHASE 1: Asking OpenAI what Discord data is needed...")

    start = time.perf_counter()

    raw_plan = await asyncio.to_thread(
        ask_planner_sync,
        question,
        current_channel,
        available_channels,
    )

    elapsed = time.perf_counter() - start

    plan = normalize_plan(
        raw_plan,
        current_channel,
        available_channels,
    )

    console_log(
        f"PLANNER RESPONSE ({elapsed:.2f}s): "
        f"{json.dumps(plan, ensure_ascii=False)}"
    )

    return plan


# ============================================================
# Execute Discord retrieval plan
# ============================================================

async def execute_retrieval_plan(message, plan: dict):
    if not plan["needs_discord"]:
        console_log("PHASE 2: No Discord history required.")
        return "", []

    console_log("PHASE 2: Executing Discord retrieval plan...")

    all_messages = []
    all_images = []

    if message.guild is None:
        channel_lookup = {
            getattr(message.channel, "name", "DM").lower():
                message.channel
        }
    else:
        channel_lookup = {
            channel.name.lower(): channel
            for channel in message.guild.text_channels
        }

    for item in plan["channels"]:
        channel = channel_lookup.get(
            item["name"].lower()
        )

        if channel is None:
            console_log(
                f"RETRIEVAL: channel not found: #{item['name']}"
            )
            continue

        # Do not include the current user's new question itself when
        # fetching the current channel.
        before = (
            message
            if channel.id == message.channel.id
            else None
        )

        console_log(
            f"RETRIEVAL: #{channel.name} "
            f"history={item['history']} "
            f"embeds={plan['include_embeds']} "
            f"images={plan['include_images']}"
        )

        messages, images = await collect_channel_history(
            channel,
            message.guild,
            item["history"],
            before=before,
            collect_embeds=plan["include_embeds"],
            collect_images=plan["include_images"],
        )

        console_log(
            f"RETRIEVAL RESULT: #{channel.name}: "
            f"{len(messages)} messages, "
            f"{len(images)} images discovered"
        )

        all_messages.extend(messages)
        all_images.extend(images)

    all_messages.sort(
        key=lambda item: item["timestamp"]
    )

    history_lines = []

    for item in all_messages:
        stamp = item["timestamp"].astimezone(
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

        history_lines.append(
            f"[{stamp}] #{item['channel']} | "
            f"{item['author']}: {item['text']}"
        )

    # Only send the newest configured number of images.
    all_images.sort(
        key=lambda item: item["timestamp"],
        reverse=True
    )

    selected_images = []
    seen_urls = set()

    for image in all_images:
        if len(selected_images) >= MAX_IMAGES:
            break

        if image["url"] in seen_urls:
            continue

        selected_images.append(image)
        seen_urls.add(image["url"])

    console_log(
        f"RETRIEVAL COMPLETE: "
        f"{len(all_messages)} messages, "
        f"{len(selected_images)} images selected"
    )

    for image in selected_images:
        console_log(
            f"IMAGE SELECTED: "
            f"#{image['channel']} - {image['filename']}"
        )

    return "\n".join(history_lines), selected_images


# ============================================================
# Final OpenAI answer
# ============================================================

def answer_direct_question_sync(
    question: str,
    current_channel: str,
    discord_history: str,
    image_urls: list[str],
) -> str:
    if discord_history:
        text = f"""
Current Discord channel: #{current_channel}

User message:
{question}

The Discord retrieval planner determined that the following server data is
relevant:

--- RETRIEVED DISCORD DATA ---
{discord_history}
--- END RETRIEVED DISCORD DATA ---

Answer the user's original message using the retrieved data when relevant.
"""
    else:
        text = question

    content = [
        {
            "type": "input_text",
            "text": text,
        }
    ]

    for url in image_urls:
        content.append({
            "type": "input_image",
            "image_url": url,
        })

    response = client_ai.responses.create(
        model=OPENAI_MODEL,
        instructions=load_answer_instructions(),
        tools=[
            {"type": "web_search"}
        ],
        input=[
            {
                "role": "user",
                "content": content,
            }
        ],
    )

    return response.output_text


def answer_indirect_question_sync(
    question: str,
    current_channel: str,
    discord_history: str,
    image_urls: list[str],
) -> Optional[str]:
    prompt_text = f"""
Current Discord channel: #{current_channel}

User question:
{question}

Recent channel history:
--- BEGIN HISTORY ---
{discord_history}
--- END HISTORY ---

This is a confidence check. Return only valid JSON with exactly these fields:
{"confident": true, "answer": "Your concise answer"}

Decide whether you can provide a confident, useful answer under the answer
instructions. If history does not establish a raid outcome, the configured
Warcraft Logs link is an allowed fallback; do not claim an unverified outcome.
Return confident=false and an empty answer only when neither the available
context nor an allowed fallback supports a useful answer.
"""

    content = [{
        "type": "input_text",
        "text": prompt_text,
    }]

    for url in image_urls:
        content.append({
            "type": "input_image",
            "image_url": url,
        })

    response = client_ai.responses.create(
        model=OPENAI_MODEL,
        instructions=load_answer_instructions(),
        tools=[
            {"type": "web_search"}
        ],
        input=[
            {
                "role": "user",
                "content": content,
            }
        ],
    )

    try:
        result = json.loads(strip_json_fences(response.output_text))
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(result, dict):
        return None

    if result.get("confident") is not True:
        return None

    answer = result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return None

    return answer.strip()


# ============================================================
# Discord output
# ============================================================

async def send_long_message(message, text):
    while text:
        if len(text) <= 2000:
            await message.reply(text, mention_author=False)
            break

        split_at = text.rfind("\n", 0, 1900)

        if split_at == -1:
            split_at = text.rfind(" ", 0, 1900)

        if split_at == -1:
            split_at = 1900

        chunk = text[:split_at]
        text = text[split_at:].lstrip()

        await message.reply(chunk, mention_author=False)


async def send_openai_error(message, error):
    error_text = str(error)

    if (
        "credit_balance_exhausted" in error_text
        or "insufficient_quota" in error_text
        or "no credits remaining" in error_text.lower()
    ):
        console_log("OPENAI API CREDIT BALANCE EXHAUSTED")

        await message.reply(
            "I can't answer right now because the OpenAI API account "
            "is out of credits. An admin needs to add API credits "
            "before I can continue."
        )
    else:
        console_log(
            f"OPENAI RATE LIMIT ERROR: {repr(error)}"
        )

        await message.reply(
            "OpenAI is temporarily rate-limiting requests. "
            "Please try again in a little bit."
        )


# ============================================================
# Discord events
# ============================================================

@discord_client.event
async def on_ready():
    print(f"Logged in as {discord_client.user}")
    print(f"Bot ID: {discord_client.user.id}")
    print(f"Bot modes: {', '.join(sorted(BOT_MODES))}")
    print(f"Answer model: {OPENAI_MODEL}")
    print(f"Planner model: {PLANNER_MODEL}")
    print(f"Max history/channel: {MAX_HISTORY_PER_CHANNEL}")
    print(f"Max channels/query: {MAX_CHANNELS_PER_QUERY}")
    print(f"Answer history limit: {ANSWER_HISTORY_LIMIT}")
    print(f"Answer prompt file: {ANSWER_PROMPT_FILE.resolve()}")
    print(f"Max images/query: {MAX_IMAGES}")
    print(f"Log file: {LOG_FILE.resolve()}")
    print("Ready.")


@discord_client.event
async def on_message(message):
    if message.author.bot:
        return

    channel_name = getattr(message.channel, "name", "DM")

    console_log(
        f"INCOMING from {message.author.display_name} "
        f"in #{channel_name}: {message.content!r}"
    )

    if re.search(r"\bryan\b", message.content, re.IGNORECASE):
        console_log("KEYWORD MATCH: Ryan; sending negative reply.")
        try:
            await message.reply("Fuck Ryan!", mention_author=False)
        except discord.Forbidden:
            console_log("Could not reply to Ryan keyword message.")
        return

    bot_id = (
        discord_client.user.id
        if discord_client.user
        else None
    )

    parsed_mention = (
        discord_client.user is not None
        and discord_client.user in message.mentions
    )

    raw_mention = (
        bot_id is not None
        and bot_id in message.raw_mentions
    )

    literal_mention = (
        bot_id is not None
        and (
            f"<@{bot_id}>" in message.content
            or f"<@!{bot_id}>" in message.content
        )
    )

    is_dm = isinstance(
        message.channel,
        discord.DMChannel
    )

    mentioned = (
        parsed_mention
        or raw_mention
        or literal_mention
    )

    console_log(
        f"ROUTING: mentioned={mentioned}, "
        f"parsed={parsed_mention}, "
        f"raw={raw_mention}, "
        f"literal={literal_mention}, "
        f"dm={is_dm}"
    )

    question = message.content

    if discord_client.user:
        question = question.replace(
            f"<@{discord_client.user.id}>",
            ""
        ).replace(
            f"<@!{discord_client.user.id}>",
            ""
        ).strip()

    if not question and not message.attachments:
        try:
            await message.reply("What's up?")
        except discord.Forbidden:
            console_log(
                "Could not reply in this channel."
            )
        return

    mention_trigger = "mentioned" in BOT_MODES and (mentioned or is_dm)
    qa_trigger = "qa" in BOT_MODES and looks_like_question(question)

    if not mention_trigger and not qa_trigger:
        console_log(
            "IGNORED: message matched neither enabled trigger "
            "(direct mention nor question)."
        )
        return

    trigger_type = "mentioned" if mention_trigger else "qa"

    print()
    console_log("=" * 72)
    console_log(
        f"QUESTION from {message.author.display_name} "
        f"in #{channel_name}"
    )
    console_log(question)
    console_log("=" * 72)

    if request_lock.locked():
        console_log("REQUEST QUEUED: waiting for the active request to finish")

    await request_lock.acquire()

    try:
        start_time = time.perf_counter()
        console_log(
            f"ROUTE: {trigger_type.upper()} trigger; "
            "planner and retrieval run before the response decision."
        )
        plan = await create_retrieval_plan(
            message,
            question,
        )
        discord_history, selected_images = (
            await execute_retrieval_plan(
                message,
                plan,
            )
        )

        image_urls = []
        seen_urls = set()

        for attachment in message.attachments:
            if (
                is_image_attachment(attachment)
                and attachment.url not in seen_urls
            ):
                image_urls.append(attachment.url)
                seen_urls.add(attachment.url)

        for image in selected_images:
            if len(image_urls) >= MAX_IMAGES:
                break

            if image["url"] not in seen_urls:
                image_urls.append(image["url"])
                seen_urls.add(image["url"])

        if trigger_type == "qa":
            console_log(
                "PHASE 2: Evaluating whether the planner-selected context "
                "supports a confident answer..."
            )
            answer = await asyncio.to_thread(
                answer_indirect_question_sync,
                question,
                channel_name,
                discord_history,
                image_urls,
            )
            if answer is None:
                console_log(
                    "ROUTE RESULT: Q&A confidence was insufficient; "
                    "no reply sent."
                )
                return
        else:
            console_log("PHASE 2: Answering direct mention regardless of confidence...")
            answer = await asyncio.to_thread(
                answer_direct_question_sync,
                question,
                channel_name,
                discord_history,
                image_urls,
            )

        elapsed = time.perf_counter() - start_time

        console_log(
            f"PHASE 4: OpenAI response received "
            f"in {elapsed:.2f}s"
        )

        log_interaction(
            message,
            question,
            answer,
            plan,
        )

        console_log(
            "PHASE 5: Sending response to Discord..."
        )

        await send_long_message(message, answer)

        console_log("DONE")
        print()

    except json.JSONDecodeError as e:
        log_error(message, e)

        console_log(
            f"PLANNER JSON ERROR: {repr(e)}"
        )

        try:
            await message.reply(
                "I had trouble deciding what Discord data to "
                "look up. Please try that question again."
            )
        except discord.Forbidden:
            console_log(
                "Could not reply in this channel."
            )

    except RateLimitError as e:
        log_error(message, e)

        try:
            await send_openai_error(
                message,
                e,
            )
        except discord.Forbidden:
            console_log(
                "Could not notify user in this channel."
            )

    except discord.Forbidden as e:
        log_error(message, e)

        console_log(
            f"DISCORD PERMISSION ERROR: {e}"
        )

    except Exception as e:
        log_error(message, e)

        console_log(
            f"ERROR: {repr(e)}"
        )

        try:
            await message.reply(
                f"Something went wrong: "
                f"`{type(e).__name__}`"
            )
        except discord.Forbidden:
            console_log(
                "Could not reply in this channel."
            )

    finally:
        request_lock.release()


discord_client.run(DISCORD_TOKEN)
