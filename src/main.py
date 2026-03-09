import asyncio
import json
import logging
import os
import signal
from collections import deque
from typing import Optional

import httpx
import websockets
from dotenv import load_dotenv
from telegram import Bot
from twitchio import Client

load_dotenv("../.env")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHANNEL_ID = os.getenv("TG_CHANNEL_ID")

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_BROADCASTER_LOGIN = os.getenv("TWITCH_BROADCASTER_LOGIN")

TWITCH_USER_ACCESS_TOKEN = os.getenv("TWITCH_USER_ACCESS_TOKEN")

EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"
RECONNECT_DELAY_SECONDS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("showstreaminfobot")


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable not found: {name}")
    return value


async def send_post(bot: Bot, chat_id: str, text: str) -> int:
    message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        connect_timeout=10,
        read_timeout=10,
        write_timeout=10,
        pool_timeout=10,
    )
    logger.info("Message sent | chat_id=%s | message_id=%s", chat_id, message.message_id)
    return message.message_id


async def delete_post(bot: Bot, chat_id: str, message_id: int) -> None:
    result = await bot.delete_message(
        chat_id=chat_id,
        message_id=message_id,
        connect_timeout=10,
        read_timeout=10,
        write_timeout=10,
        pool_timeout=10,
    )
    logger.info(
        "Message deleted | chat_id=%s | message_id=%s | result=%s",
        chat_id,
        message_id,
        result,
    )


async def fetch_twitch_state() -> dict:
    client = Client(
        client_id=TWITCH_CLIENT_ID,
        client_secret=TWITCH_CLIENT_SECRET,
    )

    await client.login()

    try:
        users = await client.fetch_users(logins=[TWITCH_BROADCASTER_LOGIN])
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
        f"Streamer: {twitch_data['display_name']}\n"
        f"Login: {twitch_data['login']}\n"
        f"Category: {twitch_data['channel_game']}\n"
        f"Stream title: {twitch_data['stream_title'] or twitch_data['channel_title']}\n"
        f"Link: https://twitch.tv/{twitch_data['login']}"
    )


async def create_eventsub_subscription(
        subscription_type: str,
        broadcaster_user_id: str,
        session_id: str,
) -> None:
    url = "https://api.twitch.tv/helix/eventsub/subscriptions"

    headers = {
        "Authorization": f"Bearer {TWITCH_USER_ACCESS_TOKEN}",
        "Client-Id": TWITCH_CLIENT_ID,
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

    logger.info(
        "Subscription created | type=%s | broadcaster_user_id=%s | session_id=%s",
        subscription_type,
        broadcaster_user_id,
        session_id,
    )


class StreamWatcher:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.current_message_id: Optional[int] = None
        self.broadcaster_user_id: Optional[str] = None
        self.seen_message_ids = deque(maxlen=200)

    async def ensure_broadcaster_user_id(self) -> str:
        if self.broadcaster_user_id is None:
            twitch_data = await fetch_twitch_state()
            self.broadcaster_user_id = twitch_data["user_id"]
            logger.info(
                "Determined broadcaster_user_id | login=%s | user_id=%s",
                TWITCH_BROADCASTER_LOGIN,
                self.broadcaster_user_id,
            )
        return self.broadcaster_user_id

    async def handle_stream_online(self) -> None:
        if self.current_message_id is not None:
            logger.info(
                "Received stream.online, but message already exists | message_id=%s",
                self.current_message_id,
            )
            return

        twitch_data = await fetch_twitch_state()
        text = build_post_text(twitch_data)

        logger.info(
            "Detected stream.online | login=%s | title=%s",
            twitch_data["login"],
            twitch_data["stream_title"] or twitch_data["channel_title"],
            )

        self.current_message_id = await send_post(self.bot, TG_CHANNEL_ID, text)

    async def handle_stream_offline(self) -> None:
        logger.info("Detected stream.offline | login=%s", TWITCH_BROADCASTER_LOGIN)

        if self.current_message_id is None:
            logger.info("Nothing to delete | current_message_id=None")
            return

        await delete_post(self.bot, TG_CHANNEL_ID, self.current_message_id)
        self.current_message_id = None

    async def subscribe(self, session_id: str) -> None:
        broadcaster_user_id = await self.ensure_broadcaster_user_id()

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

    async def listen_once(self, websocket_url: str, stop_event: asyncio.Event) -> Optional[str]:
        logger.info("Connecting to EventSub WebSocket | url=%s", websocket_url)

        async with websockets.connect(websocket_url) as ws:
            welcome_raw = await ws.recv()
            welcome = json.loads(welcome_raw)

            welcome_type = welcome["metadata"]["message_type"]
            if welcome_type != "session_welcome":
                raise RuntimeError(f"Expected session_welcome, got {welcome_type}")

            session_id = welcome["payload"]["session"]["id"]
            logger.info("Received session_welcome | session_id=%s", session_id)

            await self.subscribe(session_id)

            while not stop_event.is_set():
                raw_message = await ws.recv()
                data = json.loads(raw_message)

                metadata = data.get("metadata", {})
                payload = data.get("payload", {})
                message_type = metadata.get("message_type")
                message_id = metadata.get("message_id")

                if message_id:
                    if message_id in self.seen_message_ids:
                        logger.warning("Skipped duplicate message | message_id=%s", message_id)
                        continue
                    self.seen_message_ids.append(message_id)

                if message_type == "session_keepalive":
                    logger.info("keepalive")
                    continue

                if message_type == "notification":
                    subscription = payload.get("subscription", {})
                    sub_type = subscription.get("type")

                    if sub_type == "stream.online":
                        await self.handle_stream_online()
                    elif sub_type == "stream.offline":
                        await self.handle_stream_offline()
                    else:
                        logger.info("Unhandled notification type=%s", sub_type)

                    continue

                if message_type == "session_reconnect":
                    reconnect_url = payload.get("session", {}).get("reconnect_url")
                    logger.warning("Twitch requested reconnect | reconnect_url=%s", reconnect_url)
                    return reconnect_url

                if message_type == "revocation":
                    raise RuntimeError(f"Subscription revoked: {data}")

                logger.info("Unhandled message: %s", data)

        return None


async def run_watcher(bot: Bot, stop_event: asyncio.Event) -> None:
    watcher = StreamWatcher(bot)
    websocket_url = EVENTSUB_WS_URL

    while not stop_event.is_set():
        try:
            reconnect_url = await watcher.listen_once(websocket_url, stop_event)

            if stop_event.is_set():
                break

            if reconnect_url:
                websocket_url = reconnect_url
                continue

            websocket_url = EVENTSUB_WS_URL
            logger.warning("Connection closed without reconnect_url, reconnecting in %s sec", RECONNECT_DELAY_SECONDS)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Error in watcher: %s", e)
            websocket_url = EVENTSUB_WS_URL

        if not stop_event.is_set():
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    logger.info("Watcher stopped")


async def main() -> None:
    require_env("TG_BOT_TOKEN")
    require_env("TG_CHANNEL_ID")
    require_env("TWITCH_CLIENT_ID")
    require_env("TWITCH_CLIENT_SECRET")
    require_env("TWITCH_BROADCASTER_LOGIN")
    require_env("TWITCH_USER_ACCESS_TOKEN")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        if not stop_event.is_set():
            logger.info("Received shutdown signal, starting graceful shutdown")
            stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, request_shutdown)
            except NotImplementedError:
                pass

    async with Bot(token=TG_BOT_TOKEN) as bot:
        watcher_task = asyncio.create_task(run_watcher(bot, stop_event))

        await stop_event.wait()

        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            logger.info("Watcher task cancelled")

    logger.info("Program terminated gracefully")


if __name__ == "__main__":
    asyncio.run(main())