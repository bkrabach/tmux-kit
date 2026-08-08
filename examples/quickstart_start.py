"""Spawn a tmux-kit session, then exit -- the session outlives this process."""

import asyncio

import tmux_kit


async def main() -> None:
    ok, err = await tmux_kit.start(
        "quickstart-demo", "echo hello from tmux-kit; sleep 300"
    )
    print("started:", ok, err)


asyncio.run(main())
