from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import shutil
import sys
from typing import Any

from .config import Settings
from .db import Database


class RunnerError(RuntimeError):
    pass


@dataclass
class ProcessRecord:
    process: asyncio.subprocess.Process
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    desired_stop: bool = False


def _existing_paths(paths: list[str]) -> list[Path]:
    return [Path(p) for p in paths if Path(p).exists()]


class ProcessManager:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.active: dict[int, ProcessRecord] = {}

    def _resolve_python_bin(self) -> str:
        configured = self.settings.python_bin
        if Path(configured).is_absolute():
            return configured
        return shutil.which(configured) or configured

    def _python_bind_roots(self, python_bin: str) -> list[Path]:
        path = Path(python_bin)
        if not path.is_absolute():
            return []
        resolved = path.resolve()
        roots: list[Path] = []
        if str(resolved).startswith("/usr/"):
            return roots

        current = resolved.parent
        while current != current.parent:
            if (current / "pyvenv.cfg").exists():
                roots.append(current)
                return roots
            current = current.parent
        roots.append(resolved.parent)
        return roots

    def build_sandbox_command(self, bot_dir: Path) -> list[str]:
        python_bin = self._resolve_python_bin()
        cmd = [
            self.settings.bwrap_bin,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]

        for path in _existing_paths(["/usr", "/bin", "/lib", "/lib64", "/etc/ssl", "/etc/ca-certificates"]):
            cmd.extend(["--ro-bind", str(path), str(path)])

        for path in _existing_paths(["/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"]):
            cmd.extend(["--ro-bind", str(path), str(path)])

        for root in self._python_bind_roots(python_bin):
            if root.exists():
                cmd.extend(["--ro-bind", str(root), str(root)])

        cmd.extend(
            [
                "--bind",
                str(bot_dir),
                "/app",
                "--chdir",
                "/app",
                python_bin,
                "bot.py",
            ]
        )
        return cmd

    def build_plain_command(self) -> list[str]:
        return [self.settings.python_bin, "bot.py"]

    def _child_env(self, token: str, bot_db_path: str) -> dict[str, str]:
        return {
            "BOT_TOKEN": token,
            "BOT_DB_PATH": bot_db_path,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }

    async def start_bot(self, bot_id: int) -> None:
        if bot_id in self.active:
            return

        bot = self.db.get_bot(bot_id)
        if bot is None:
            raise RunnerError(f"Bot {bot_id} does not exist.")
        revision = self.db.latest_revision(bot_id)
        if revision is None or revision["validation_status"] != "ok":
            raise RunnerError(f"Bot {bot_id} has no valid revision.")

        bot_dir = Path(bot["workdir"])
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "bot.py").write_text(revision["code"], encoding="utf-8")
        child_db = bot_dir / "bot.sqlite3"
        env = self._child_env(bot["token"], "/app/bot.sqlite3" if self.settings.require_bwrap else str(child_db))

        if self.settings.require_bwrap:
            if shutil.which(self.settings.bwrap_bin) is None:
                self.db.update_bot_status(bot_id, "sandbox_missing")
                raise RunnerError(
                    f"Bubblewrap executable '{self.settings.bwrap_bin}' was not found. "
                    "Install it with: sudo apt install bubblewrap"
                )
            command = self.build_sandbox_command(bot_dir)
            cwd = None
        else:
            command = self.build_plain_command()
            cwd = str(bot_dir)

        self.db.update_bot_status(bot_id, "starting")
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        record = ProcessRecord(process=process)
        self.active[bot_id] = record
        self.db.mark_started(bot_id, process.pid or 0)
        self.db.add_log(bot_id, "system", f"Started process pid={process.pid}", self.settings.log_tail_rows)

        if process.stdout is not None:
            record.tasks.append(asyncio.create_task(self._pump_stream(bot_id, "stdout", process.stdout, bot_dir)))
        if process.stderr is not None:
            record.tasks.append(asyncio.create_task(self._pump_stream(bot_id, "stderr", process.stderr, bot_dir)))
        record.tasks.append(asyncio.create_task(self._watch_process(bot_id, record)))

    async def _pump_stream(
        self,
        bot_id: int,
        stream_name: str,
        reader: asyncio.StreamReader,
        bot_dir: Path,
    ) -> None:
        log_path = bot_dir / f"{stream_name}.log"
        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
            self.db.add_log(bot_id, stream_name, text, self.settings.log_tail_rows)

    async def _watch_process(self, bot_id: int, record: ProcessRecord) -> None:
        return_code = await record.process.wait()
        current = self.active.get(bot_id)
        if current is record:
            self.active.pop(bot_id, None)
        if record.desired_stop:
            self.db.add_log(bot_id, "system", f"Stopped process rc={return_code}", self.settings.log_tail_rows)
            return
        self.db.mark_stopped(bot_id, "crashed")
        self.db.add_log(bot_id, "system", f"Process exited unexpectedly rc={return_code}", self.settings.log_tail_rows)

    async def stop_bot(self, bot_id: int, mark_stopped: bool = True) -> None:
        record = self.active.get(bot_id)
        if record is None:
            if mark_stopped:
                self.db.mark_stopped(bot_id)
            return

        record.desired_stop = True
        process = record.process
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        for task in record.tasks:
            if not task.done():
                task.cancel()
        self.active.pop(bot_id, None)
        if mark_stopped:
            self.db.mark_stopped(bot_id)
            self.db.add_log(bot_id, "system", "Stopped by request", self.settings.log_tail_rows)

    async def restart_bot(self, bot_id: int) -> None:
        await self.stop_bot(bot_id, mark_stopped=False)
        await self.start_bot(bot_id)

    async def restore_running_bots(self) -> None:
        for bot in self.db.running_bots():
            try:
                await self.start_bot(int(bot["id"]))
            except Exception as exc:
                self.db.mark_stopped(int(bot["id"]), "restore_failed")
                self.db.add_log(int(bot["id"]), "system", f"Restore failed: {exc}", self.settings.log_tail_rows)

    async def stop_all(self, mark_stopped: bool = True) -> None:
        bot_ids = list(self.active)
        for bot_id in bot_ids:
            await self.stop_bot(bot_id, mark_stopped=mark_stopped)
        if mark_stopped:
            self.db.set_many_stopped(bot_ids)

