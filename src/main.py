import os
import json
import asyncio

import httpx
import websockets

from dotenv import load_dotenv
from telegram import Bot
from twitchio import Client

load_dotenv("../.env")

tg_bot_token = os.getenv("TG_BOT_TOKEN")
tg_channel_id = os.getenv("TG_CHANNEL_ID")

client_id = os.getenv("TWITCH_CLIENT_ID")
client_secret = os.getenv("TWITCH_CLIENT_SECRET")
broadcaster_login = os.getenv("TWITCH_BROADCASTER_LOGIN")

twitch_user_access_token = os.getenv("TWITCH_USER_ACCESS_TOKEN")

EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"


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


async def fetch_twitch_state() -> dict:
    client = Client(
        client_id=client_id,
        client_secret=client_secret,
    )

    await client.login()

    try:
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

        return data
    finally:
        await client.close()


def build_post_text(twitch_data: dict) -> str:
    return (
        f"Стример: {twitch_data['display_name']}\n"
        f"Логин: {twitch_data['login']}\n"
        f"Категория: {twitch_data['channel_game']}\n"
        f"Название стрима: {twitch_data['stream_title'] or twitch_data['channel_title']}\n"
        f"Ссылка: https://twitch.tv/{twitch_data['login']}"
    )


async def create_eventsub_subscription(
        subscription_type: str,
        broadcaster_user_id: str,
        session_id: str,
) -> None:
    url = "https://api.twitch.tv/helix/eventsub/subscriptions"

    headers = {
        "Authorization": f"Bearer {twitch_user_access_token}",
        "Client-Id": client_id,
        "Content-Type": "application/json",
    }

    body = {
        "type": subscription_type,
        "version": "1",
        "condition": {
            "broadcaster_user_id": broadcaster_user_id,
        },
        "transport": {
            "method": "websocket",
            "session_id": session_id,
        },
    }

    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.post(url, headers=headers, json=body)

    if response.status_code not in (200, 202):
        raise RuntimeError(
            f"Subscription failed for {subscription_type}: "
            f"{response.status_code} {response.text}"
        )

    print(f"subscribed: {subscription_type}")


async def handle_stream_online(bot: Bot) -> int:
    twitch_data = await fetch_twitch_state()
    text = build_post_text(twitch_data)
    message_id = await send_post(bot, tg_channel_id, text)
    print("stream online, post sent:", message_id)
    return message_id


async def handle_stream_offline(bot: Bot, message_id: int | None) -> None:
    if message_id is None:
        return

    await delete_post(bot, tg_channel_id, message_id)
    print("stream offline, post deleted:", message_id)


async def listen_eventsub(bot: Bot) -> None:
    initial_state = await fetch_twitch_state()
    broadcaster_user_id = initial_state["user_id"]

    current_message_id = None

    async with websockets.connect(EVENTSUB_WS_URL) as ws:
        print("websocket connected")

        welcome_raw = await ws.recv()
        welcome = json.loads(welcome_raw)

        message_type = welcome["metadata"]["message_type"]
        if message_type != "session_welcome":
            raise RuntimeError(f"Expected session_welcome, got {message_type}")

        session_id = welcome["payload"]["session"]["id"]
        print("session_id:", session_id)

        await create_eventsub_subscription(
            "stream.online",
            broadcaster_user_id,
            session_id,
        )
        await create_eventsub_subscription(
            "stream.offline",
            broadcaster_user_id,
            session_id,
        )

        while True:
            raw_message = await ws.recv()
            data = json.loads(raw_message)

            metadata = data.get("metadata", {})
            payload = data.get("payload", {})
            message_type = metadata.get("message_type")

            if message_type == "session_keepalive":
                print("keepalive")
                continue

            if message_type == "notification":
                subscription = payload.get("subscription", {})
                sub_type = subscription.get("type")

                if sub_type == "stream.online":
                    if current_message_id is None:
                        current_message_id = await handle_stream_online(bot)

                elif sub_type == "stream.offline":
                    await handle_stream_offline(bot, current_message_id)
                    current_message_id = None

                else:
                    print("unknown notification type:", sub_type)

                continue

            if message_type == "session_reconnect":
                reconnect_url = payload.get("session", {}).get("reconnect_url")
                raise RuntimeError(f"Reconnect requested: {reconnect_url}")

            if message_type == "revocation":
                raise RuntimeError(f"Subscription revoked: {data}")

            print("unhandled message:", data)


async def main() -> None:
    if not twitch_user_access_token:
        raise RuntimeError("Не найден TWITCH_USER_ACCESS_TOKEN")

    async with Bot(token=tg_bot_token) as bot:
        await listen_eventsub(bot)


if __name__ == "__main__":
    asyncio.run(main())