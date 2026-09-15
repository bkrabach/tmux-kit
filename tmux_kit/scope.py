"""Non-owning, explicitly socket-bound tmux observation.

``TmuxScope`` observes one caller-supplied tmux server without discovering,
creating, changing, or tearing down that server.  It deliberately snapshots
its subprocess environment and always passes ``-S`` so application-global
environment configuration and an inherited ``$TMUX`` cannot redirect an
observation after construction.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from tmux_kit import observe, proc


class TmuxScope:
    """Observe the active pane of sessions on one explicit socket path.

    This class owns neither the tmux server nor its socket. Subprocess and
    target-resolution failures raise instead of becoming empty results.
    Malformed active-pane records and pane metadata also raise; session
    enumeration retains the shared parser's per-field metadata tolerance.

    User-home notation in the socket path is expanded once in the caller's
    environment, after which the path must be absolute. ``env`` controls only
    the child subprocess environment, not that path expansion.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        path = os.path.expanduser(os.fspath(socket_path))
        if not path:
            raise ValueError("tmux socket path must not be empty")
        if "\x00" in path:
            raise ValueError("tmux socket path must not contain NUL")
        if not os.path.isabs(path):
            raise ValueError(f"tmux socket path must be absolute: {path!r}")

        self._socket_path = path
        self._env = dict(os.environ) if env is None else dict(env)
        self._env.pop("TMUX", None)

    @property
    def socket_path(self) -> str:
        """The explicitly supplied absolute socket pathname."""
        return self._socket_path

    async def _run(
        self,
        *args: str,
        operation: str,
        target: str | None = None,
    ) -> str:
        """Use the package's sole tmux subprocess door with pinned routing."""
        try:
            return await proc.run_tmux(
                "-S", self.socket_path, *args, env=dict(self._env)
            )
        except (RuntimeError, OSError) as exc:
            target_note = f" for session {target!r}" if target is not None else ""
            detail = str(exc).strip() or "tmux produced no error output"
            raise RuntimeError(
                f"tmux scope observation failed on socket {self.socket_path!r} "
                f"during {operation}{target_note}: {detail}"
            ) from exc

    async def enumerate_sessions(
        self,
    ) -> tuple[list[str], dict[str, float], dict[str, float], dict[str, str]]:
        """Return names, activity, creation time, and cwd without cache writes."""
        output = await self._run(
            "list-sessions",
            "-F",
            "#{session_name}\t#{window_activity}\t#{session_created}\t#{pane_current_path}",
            operation="session enumeration",
        )
        return observe._parse_session_listing(output)

    async def capture_pane(
        self,
        session_name: str,
        lines: int = observe.DEFAULT_CAPTURE_LINES,
        *,
        escapes: bool = True,
    ) -> str:
        """Capture the active pane's final *lines* rows on an exact session."""
        pane_id = await self._active_pane(session_name)
        args = ["capture-pane"]
        if escapes:
            args.append("-e")
        args += ["-p", "-t", pane_id, "-S", f"-{lines}"]
        return await self._run(
            *args, operation="pane capture", target=session_name
        )

    async def capture_pane_metadata(
        self, session_name: str
    ) -> tuple[int, int, int]:
        """Read active-pane scrollback metadata for an exact session."""
        pane_id = await self._active_pane(session_name)
        output = await self._run(
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{history_size}\t#{pane_height}\t#{history_limit}",
            operation="pane metadata",
            target=session_name,
        )
        return self._parse_metadata(output, session_name)

    async def capture_pane_window(
        self,
        session_name: str,
        s: int,
        e: int | None,
        *,
        escapes: bool = True,
    ) -> tuple[int, int, int, str]:
        """Read metadata and one active-pane capture window in one tmux call."""
        pane_id = await self._active_pane(session_name)
        args = [
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{history_size}\t#{pane_height}\t#{history_limit}",
            ";",
            "capture-pane",
        ]
        if escapes:
            args.append("-e")
        args += ["-p", "-t", pane_id, "-S", str(s)]
        if e is not None:
            args += ["-E", str(e)]
        output = await self._run(
            *args, operation="pane window capture", target=session_name
        )
        header, _, text = output.partition("\n")
        return (*self._parse_metadata(header, session_name), text)

    @staticmethod
    def _validate_session_name(session_name: str) -> None:
        if not session_name:
            raise ValueError("tmux session name must not be empty")
        if "\x00" in session_name:
            raise ValueError("tmux session name must not contain NUL")
        if ":" in session_name or "." in session_name:
            raise ValueError(
                "tmux scope accepts a session name only, not a window or pane target"
            )

    async def _active_pane(self, session_name: str) -> str:
        self._validate_session_name(session_name)
        output = await self._run(
            "list-panes",
            "-t",
            f"={session_name}:",
            "-F",
            "#{pane_id}\t#{pane_active}",
            operation="active-pane resolution",
            target=session_name,
        )
        active_panes: list[str] = []
        for raw_line in output.splitlines():
            fields = raw_line.split("\t")
            if len(fields) != 2:
                raise RuntimeError(
                    f"malformed active-pane observation on socket {self.socket_path!r} "
                    f"for session {session_name!r}: expected pane id and active flag"
                )
            pane_id, active = (field.strip() for field in fields)
            if not pane_id.startswith("%") or not pane_id[1:].isdigit():
                raise RuntimeError(
                    f"malformed active-pane observation on socket {self.socket_path!r} "
                    f"for session {session_name!r}: invalid pane id {pane_id!r}"
                )
            if active not in {"0", "1"}:
                raise RuntimeError(
                    f"malformed active-pane observation on socket {self.socket_path!r} "
                    f"for session {session_name!r}: invalid active flag {active!r}"
                )
            if active == "1":
                active_panes.append(pane_id)
        if len(active_panes) != 1:
            raise RuntimeError(
                f"active-pane observation on socket {self.socket_path!r} for session "
                f"{session_name!r} returned {len(active_panes)} active panes; expected one"
            )
        return active_panes[0]

    def _parse_metadata(
        self, output: str, session_name: str
    ) -> tuple[int, int, int]:
        try:
            return observe._parse_pane_metadata(output, session_name)
        except RuntimeError as exc:
            raise RuntimeError(
                f"tmux scope observation failed on socket {self.socket_path!r} "
                f"during pane metadata for session {session_name!r}: {exc}"
            ) from exc
