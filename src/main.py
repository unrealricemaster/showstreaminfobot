import os
import asyncio

from dotenv import load_dotenv
from telegram import Bot
from twitchio import Client

load_dotenv("../.env")

tg_bot_token = os.getenv("TG_BOT_TOKEN")
tg_channel_id = os.getenv("TG_CHANNEL_ID")
client_id = os.getenv("TWITCH_CLIENT_ID")
client_secret = os.getenv("TWITCH_CLIENT_SECRET")
broadcaster_login = os.getenv("TWITCH_BROADCASTER_LOGIN")

async def fetch_twitch_state() -> dict:
    client = Client(
        client_id=client_id,
        client_secret=client_secret,
    )

    await client.login()

    users = await client.fetch_users(logins=[broadcaster_login])
    if not users:
        raise RuntimeError("Twitch user not found")

    user = users[0]

    channels = await client.fetch_channels(broadcaster_ids=[user.id])
    if not channels:
        raise RuntimeError("Channel not found")

    channel = channels[0]

    streams = [stream async for stream in client.fetch_streams(user_ids=[user.id])]

    data = {
        "user_id": user.id,
        "login": user.name,
        "display_name": user.display_name,
        "channel_title": channel.title,
        "channel_game": channel.game_name,
        "is_live": False,
        "stream_title": None,
        "thumbnail_url": None,
    }

    if streams:
        stream = streams[0]
        data["is_live"] = True
        data["stream_title"] = stream.title
        data["thumbnail_url"] = stream.thumbnail.url

    await client.close()
    return data

async def send_post(bot: Bot, chat_id: str, text: str) -> int:
    message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        connect_timeout=10,
        read_timeout=10,
        write_timeout=10,
        pool_timeout=10,
    )
    return message.message_id

async def delete_post(bot: Bot, chat_id: str, message_id: int) -> None:
    await bot.delete_message(
        chat_id=chat_id,
        message_id=message_id,
        connect_timeout=10,
        read_timeout=10,
        write_timeout=10,
        pool_timeout=10,
    )

async def main() -> None:
    twitch_data = await fetch_twitch_state()

    text = (
        f"Стример: {twitch_data['display_name']}\n"
        f"Логин: {twitch_data['login']}\n"
        f"Категория: {twitch_data['channel_game']}\n"
        f"Название канала: {twitch_data['channel_title']}\n"
        f"В эфире: {'да' if twitch_data['is_live'] else 'нет'}"
    )

    async with Bot(token=tg_bot_token) as bot:
        message_id = await send_post(
            bot=bot,
            chat_id=tg_channel_id,
            text=text
        )

        print("message_id =", message_id)
        await asyncio.sleep(15)
        await delete_post(
            bot=bot,
            chat_id=tg_channel_id,
            message_id=message_id
        )

        print("deleted")

if __name__ == "__main__":
    asyncio.run(main())