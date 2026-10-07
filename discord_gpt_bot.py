import os
import re
import json
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from openai import OpenAI, RateLimitError


# ============================================================
# Configuration
# ============================================================

DISCORD_TOKEN = os.environ["MUSK_GPT"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6")

# Comma-separated list of channels the bot should use as shared guild context.
# Override in Windows CMD with, for example:
#   set MUSK_GPT_CONTEXT_CHANNELS=monday-raid,tuesday-raid,friday-raid,sunday-raid,late-absences
DEFAULT_CONTEXT_CHANNELS = (
    "monday-raid,"
    "tuesday-raid,"
    "friday-raid,"
    # "sunday-raid,"
    # "late-absences,"
    # "guild-events,"
    # "gruuls-tk,"
    # "kara"
)

CONTEXT_CHANNEL_NAMES = {
    name.strip().lower()
    for name in os.getenv(
        "MUSK_GPT_CONTEXT_CHANNELS",
        DEFAULT_CONTEXT_CHANNELS
    ).split(",")
    if name.strip()
}

HISTORY_LIMIT_PER_CHANNEL = int(
    os.getenv("MUSK_GPT_HISTORY_LIMIT", "25")
)

MAX_IMAGES = int(
    os.getenv("MUSK_GPT_MAX_IMAGES", "4")
)

LOG_FILE = Path(
    os.getenv("MUSK_GPT_LOG_FILE", "muskazze_gpt_log.jsonl")
)

STATE_FILE = Path(
    os.getenv("MUSK_GPT_STATE_FILE", "muskazze_gpt_state.json")
)


# ============================================================
# OpenAI / Discord setup
# ============================================================

client_ai = OpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

discord_client = discord.Client(intents=intents)

SYSTEM_INSTRUCTIONS = """
You are MuskazzeGPT, a helpful assistant inside a Discord server.

Keep answers conversational and relatively concise unless the user asks
for detail.

Discord supports Markdown.

When discussing World of Warcraft, understand common WoW terminology,
raiding terminology, abbreviations, classes, specs, loot systems, logs,
Raid-Helper, attendance, guild management, and raid scheduling.

You may be given recent chat history from multiple Discord channels.
Pay attention to the channel name and timestamp shown on each line.
Use information across channels when it is relevant to the user's question.

Treat Raid-Helper signup/roster data as signup or roster information unless
the surrounding chat clearly confirms actual attendance.

If an image is included, analyze it when relevant. If the user refers to a
previous screenshot or image, use the supplied image inputs.

Do not ping @everyone or @here unless explicitly asked.
"""

def console_log(message: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

# ============================================================
# Persistent OpenAI conversation IDs
# One shared OpenAI conversation per Discord guild/server.
# ============================================================

def load_conversation_state():
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        return {
            int(guild_id): conversation_id
            for guild_id, conversation_id in raw.items()
        }
    except Exception as e:
        print(f"WARNING: Could not load state file: {e}")
        return {}


def save_conversation_state():
    try:
        data = {
            str(guild_id): conversation_id
            for guild_id, conversation_id in guild_conversations.items()
        }

        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"WARNING: Could not save state file: {e}")


guild_conversations = load_conversation_state()


# ============================================================
# Logging
# ============================================================

def write_log(record: dict):
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"WARNING: Could not write log: {e}")


def log_interaction(message, question: str, answer: str):
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

    # First decode Discord mention format: <@123> or <@!123>
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

    # Then decode any remaining bare numeric Discord IDs
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
# Discord history / image collection
# ============================================================

def is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = attachment.content_type or ""
    if content_type.startswith("image/"):
        return True

    lower_name = attachment.filename.lower()
    return lower_name.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif")
    )


async def collect_channel_history(channel, guild, limit, before=None):
    messages = []
    images = []

    try:
        async for msg in channel.history(limit=limit, before=before):
            decoded_content = await decode_discord_ids(msg.content, guild)

            parts = []

            if decoded_content:
                parts.append(decoded_content)

            # ------------------------------------------------
            # Read Discord embeds, including Raid-Helper embeds
            # ------------------------------------------------
            for embed in msg.embeds:
                embed_parts = []

                if embed.title:
                    embed_parts.append(f"Embed title: {embed.title}")

                if embed.description:
                    description = await decode_discord_ids(
                        embed.description,
                        guild
                    )
                    embed_parts.append(
                        f"Embed description: {description}"
                    )

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
                    embed_parts.append(
                        f"Footer: {embed.footer.text}"
                    )

                if embed.author and embed.author.name:
                    embed_parts.append(
                        f"Embed author: {embed.author.name}"
                    )

                if embed_parts:
                    parts.append(
                        "[DISCORD EMBED]\n"
                        + "\n".join(embed_parts)
                        + "\n[/DISCORD EMBED]"
                    )

                # Some embeds may contain an image
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

            # ------------------------------------------------
            # Normal Discord file/image attachments
            # ------------------------------------------------
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
        print(
            f"Skipping #{getattr(channel, 'name', channel.id)}: "
            "missing permission."
        )

    except discord.HTTPException as e:
        print(
            f"Skipping #{getattr(channel, 'name', channel.id)}: {e}"
        )

    return messages, images
    
async def build_guild_context(message, check_history=False):
    console_log("PHASE 1: Collecting Discord history...")

    if message.guild is None:
        messages, images = await collect_channel_history(
            message.channel,
            None,
            HISTORY_LIMIT_PER_CHANNEL,
            before=message,
        )

    elif not check_history:
        console_log(
            f"History mode OFF - reading current channel only: "
            f"#{message.channel.name}"
        )

        messages, images = await collect_channel_history(
            message.channel,
            message.guild,
            HISTORY_LIMIT_PER_CHANNEL,
            before=message,
        )

    else:
        console_log("History mode ON - reading configured shared channels...")

        channels = []

        for channel in message.guild.text_channels:
            if (
                channel.name.lower() in CONTEXT_CHANNEL_NAMES
                or channel.id == message.channel.id
            ):
                channels.append(channel)

        console_log(
            "Channels selected: "
            + ", ".join(f"#{c.name}" for c in channels)
        )

        messages = []
        images = []

        for channel in channels:
            console_log(f"Reading #{channel.name}...")

            before = message if channel.id == message.channel.id else None

            channel_messages, channel_images = await collect_channel_history(
                channel,
                message.guild,
                HISTORY_LIMIT_PER_CHANNEL,
                before=before,
            )

            console_log(
                f"#{channel.name}: "
                f"{len(channel_messages)} messages, "
                f"{len(channel_images)} images"
            )

            messages.extend(channel_messages)
            images.extend(channel_images)

    console_log("PHASE 2: Sorting and formatting history...")

    messages.sort(key=lambda item: item["timestamp"])

    history_lines = []

    for item in messages:
        timestamp = item["timestamp"].astimezone(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )

        history_lines.append(
            f"[{timestamp}] #{item['channel']} | "
            f"{item['author']}: {item['text']}"
        )

    console_log(f"Total history messages included: {len(messages)}")

    console_log("PHASE 3: Selecting recent images...")

    images.sort(key=lambda item: item["timestamp"], reverse=True)
    selected_images = images[:MAX_IMAGES]

    current_images = []

    for attachment in message.attachments:
        if is_image_attachment(attachment):
            current_images.append({
                "url": attachment.url,
                "filename": attachment.filename,
            })

    ordered_image_urls = []
    seen_urls = set()

    for image in current_images:
        if image["url"] not in seen_urls:
            ordered_image_urls.append(image["url"])
            seen_urls.add(image["url"])

    for image in selected_images:
        if len(ordered_image_urls) >= MAX_IMAGES:
            break

        if image["url"] not in seen_urls:
            ordered_image_urls.append(image["url"])
            seen_urls.add(image["url"])

    console_log(f"Images included: {len(ordered_image_urls)}")

    discord_history = "\n".join(history_lines)

    current_channel_name = getattr(message.channel, "name", "DM")

    prompt_text = f"""
Here is recent Discord history.

--- DISCORD HISTORY ---
{discord_history}
--- END DISCORD HISTORY ---

Current channel: #{current_channel_name}
Current user: {message.author.display_name}
Current request:
{message.content}

Answer using the supplied Discord history when relevant.
"""

    input_content = [
        {
            "type": "input_text",
            "text": prompt_text,
        }
    ]

    for url in ordered_image_urls:
        input_content.append({
            "type": "input_image",
            "image_url": url,
        })

    return input_content 

# ============================================================
# OpenAI
# ============================================================

def ask_openai(conversation_key: int, input_content: list) -> str:
    conversation_id = guild_conversations.get(conversation_key)

    if conversation_id is None:
        conversation = client_ai.conversations.create()
        conversation_id = conversation.id
        guild_conversations[conversation_key] = conversation_id
        save_conversation_state()

    response = client_ai.responses.create(
        model=OPENAI_MODEL,
        conversation=conversation_id,
        instructions=SYSTEM_INSTRUCTIONS,
        input=[
            {
                "role": "user",
                "content": input_content,
            }
        ],
    )

    return response.output_text


# ============================================================
# Discord output
# ============================================================

async def send_long_message(channel, text):
    while text:
        if len(text) <= 2000:
            await channel.send(text)
            break

        split_at = text.rfind("\n", 0, 1900)

        if split_at == -1:
            split_at = text.rfind(" ", 0, 1900)

        if split_at == -1:
            split_at = 1900

        chunk = text[:split_at]
        text = text[split_at:].lstrip()

        await channel.send(chunk)


# ============================================================
# Discord events
# ============================================================

@discord_client.event
async def on_ready():
    print(f"Logged in as {discord_client.user}")
    print(f"Bot ID: {discord_client.user.id}")
    print(f"Model: {OPENAI_MODEL}")
    print(
        "Shared context channels: "
        + ", ".join(sorted(CONTEXT_CHANNEL_NAMES))
    )
    print(f"History per channel: {HISTORY_LIMIT_PER_CHANNEL}")
    print(f"Max images per request: {MAX_IMAGES}")
    print(f"Log file: {LOG_FILE.resolve()}")
    print("Ready.")


@discord_client.event
@discord_client.event
async def on_message(message):
    if message.author.bot:
        return

    mentioned = discord_client.user in message.mentions
    is_dm = isinstance(message.channel, discord.DMChannel)

    if not mentioned and not is_dm:
        return

    text = message.content

    if discord_client.user:
        text = text.replace(
            f"<@{discord_client.user.id}>",
            ""
        ).replace(
            f"<@!{discord_client.user.id}>",
            ""
        ).strip()

    if not text and not message.attachments:
        try:
            await message.reply("What's up?")
        except discord.Forbidden:
            print("Could not reply in this channel.")
        return

    message.content = text

    print()
    console_log("=" * 70)
    console_log(
        f"QUESTION from {message.author.display_name} "
        f"in #{getattr(message.channel, 'name', 'DM')}"
    )
    console_log(text)
    console_log("=" * 70)

    try:
        check_history = "check the history" in text.lower()

        input_content = await build_guild_context(
            message,
            check_history=check_history,
        )

        conversation_key = (
            message.guild.id
            if message.guild is not None
            else message.channel.id
        )

        console_log("PHASE 4: Sending request to OpenAI...")

        start_time = time.perf_counter()

        answer = await asyncio.to_thread(
            ask_openai,
            conversation_key,
            input_content,
        )

        elapsed = time.perf_counter() - start_time

        console_log(
            f"PHASE 5: OpenAI response received in {elapsed:.2f} seconds"
        )

        console_log("PHASE 6: Logging interaction...")
        log_interaction(message, text, answer)

        console_log("PHASE 7: Sending response to Discord...")
        await send_long_message(message.channel, answer)

        console_log("DONE")
        print()
    except RateLimitError as e:
        log_error(message, e)

        error_text = str(e)

        if (
            "credit_balance_exhausted" in error_text
            or "insufficient_quota" in error_text
            or "no credits remaining" in error_text.lower()
        ):
            console_log("OPENAI API CREDIT BALANCE EXHAUSTED")

            try:
                await message.reply(
                    "I can't answer right now because the OpenAI API account is out of credits. "
                    "An admin needs to add API credits before I can continue."
                )
            except discord.Forbidden:
                console_log("Could not notify user because I cannot send messages in this channel.")

        else:
            console_log(f"OPENAI RATE LIMIT ERROR: {repr(e)}")

            try:
                await message.reply(
                    "OpenAI is temporarily rate-limiting requests. Please try again in a little bit."
                )
            except discord.Forbidden:
                console_log("Could not reply in this channel.")
    except discord.Forbidden as e:
        log_error(message, e)
        console_log(f"DISCORD PERMISSION ERROR: {e}")

    except Exception as e:
        log_error(message, e)
        console_log(f"ERROR: {repr(e)}")

        try:
            await message.reply(
                f"Something went wrong: `{type(e).__name__}`"
            )
        except discord.Forbidden:
            console_log("Could not reply in this channel.")

discord_client.run(DISCORD_TOKEN)
