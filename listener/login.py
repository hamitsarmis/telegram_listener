import asyncio
import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from telethon import TelegramClient

load_dotenv(find_dotenv(usecwd=True))

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.environ.get("TG_SESSION", "data/listener")


async def main() -> None:
    # Ensure the parent dir exists so SQLite can create the .session file.
    # Inside the container the bind mount provides it; outside, this is a safety net.
    Path(SESSION).parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()  # prompts for phone + code if needed
    me = await client.get_me()
    session_file = Path(f"{SESSION}.session").resolve()
    print(f"Logged in as {me.first_name} (@{me.username}). Session saved to {session_file}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
