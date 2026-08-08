"""From a FRESH process, find the session started by quickstart_start.py and read it."""

import asyncio

import tmux_kit


async def main() -> None:
    sessions = await tmux_kit.list_sessions()
    print("still running:", [s.name for s in sessions])
    print("it printed:", await tmux_kit.read("quickstart-demo"))


asyncio.run(main())
