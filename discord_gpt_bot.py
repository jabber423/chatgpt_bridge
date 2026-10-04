import os
import asyncio
import discord
from openai import OpenAI

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# You can change this later.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6")

client_ai = OpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True

discord_client = discord.Client(intents=intents)

# One OpenAI conversation per Discord channel.
# NOTE: This resets when the bot restarts.
channel_conversations = {}

SYSTEM_INSTRUCTIONS = """
You are a helpful assistant inside a Discord server.

Keep answers conversational and relatively concise unless the user asks
for detail.

Discord supports Markdown.

When discussing World of Warcraft, understand common WoW terminology,
raiding terminology, abbreviations, classes, specs, loot systems, logs,
and guild management.

Do not ping @everyone or @here unless explicitly asked.
"""


def ask_openai(channel_id: int, username: str, message_text: str) -> str:
    conversation_id = channel_conversations.get(channel_id)

    if conversation_id is None:
        conversation = client_ai.conversations.create()
        conversation_id = conversation.id
        channel_conversations[channel_id] = conversation_id

    response = client_ai.responses.create(
        model=OPENAI_MODEL,
        conversation=conversation_id,
        instructions=SYSTEM_INSTRUCTIONS,
        input=[
            {
                "role": "user",
                "content": f"{username}: {message_text}"
            }
        ],
    )

    return response.output_text


async def send_long_message(channel, text):
    # Discord message limit is 2000 characters.
    while text:
        if len(text) <= 2000:
            await channel.send(text)
            break

        split_at = text.rfind("\n", 0, 1900)

        if split_at == -1:
            split_at = 1900

        chunk = text[:split_at]
        text = text[split_at:].lstrip()

        await channel.send(chunk)


@discord_client.event
async def on_ready():
    print(f"Logged in as {discord_client.user}")
    print(f"Bot ID: {discord_client.user.id}")
    print("Ready.")


@discord_client.event
async def on_message(message):
    # Don't respond to itself or other bots.
    if message.author.bot:
        return

    # Respond if:
    #   1. Bot is mentioned
    #   2. User sends the bot a DM
    mentioned = discord_client.user in message.mentions
    is_dm = isinstance(message.channel, discord.DMChannel)

    if not mentioned and not is_dm:
        return

    text = message.content

    # Remove the bot mention from the prompt.
    if discord_client.user:
        text = text.replace(
            f"<@{discord_client.user.id}>",
            ""
        ).replace(
            f"<@!{discord_client.user.id}>",
            ""
        ).strip()

    if not text:
        await message.reply("What's up?")
        return

    async with message.channel.typing():
        try:
            # OpenAI's Python SDK is synchronous here, so run it
            # outside Discord's event loop.
            answer = await asyncio.to_thread(
                ask_openai,
                message.channel.id,
                message.author.display_name,
                text,
            )

            await send_long_message(message.channel, answer)

        except Exception as e:
            print(f"ERROR: {e}")
            await message.reply(
                f"Something went wrong talking to OpenAI: `{type(e).__name__}`"
            )


discord_client.run(DISCORD_TOKEN)