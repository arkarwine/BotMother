from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from pathlib import Path
import shutil
import signal
from typing import Any

from .config import Settings
from .db import Database


logger = logging.getLogger(__name__)


MAX_UNEXPECTED_SIGNAL_RESTARTS = 2
UNEXPECTED_SIGNAL_RESTART_DELAY_SECONDS = 2


class RunnerError(RuntimeError):
    pass


@dataclass
class ProcessRecord:
    process: asyncio.subprocess.Process
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    desired_stop: bool = False


def _existing_paths(paths: list[str]) -> list[Path]:
    return [Path(p) for p in paths if Path(p).exists()]


def format_return_code(return_code: int | None) -> str:
    if return_code is None:
        return "unknown"
    if return_code < 0:
        signal_number = -return_code
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = f"signal {signal_number}"
        return f"{return_code} ({signal_name})"
    return str(return_code)


def is_signal_exit(return_code: int | None) -> bool:
    return return_code is not None and return_code < 0


class ProcessManager:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.active: dict[int, ProcessRecord] = {}
        self.unexpected_signal_restarts: dict[int, int] = {}

    def _resolve_python_bin(self) -> str:
        configured = self.settings.python_bin
        if Path(configured).is_absolute():
            return configured
        return shutil.which(configured) or configured

    def _host_python_exists(self, python_bin: str) -> bool:
        path = Path(python_bin)
        if path.is_absolute():
            return path.exists()
        return shutil.which(python_bin) is not None

    def _find_venv_root(self, path: Path) -> Path | None:
        current = path if path.is_dir() else path.parent
        while current != current.parent:
            if (current / "pyvenv.cfg").exists():
                return current
            current = current.parent
        return None

    def _python_bind_roots(self, python_bin: str) -> list[Path]:
        path = Path(python_bin)
        if not path.is_absolute():
            return []

        roots: list[Path] = []
        configured_venv = self._find_venv_root(path)
        if configured_venv is not None:
            roots.append(configured_venv)

        resolved = path.resolve()
        resolved_venv = self._find_venv_root(resolved)
        if resolved_venv is not None and resolved_venv not in roots:
            roots.append(resolved_venv)

        if roots:
            return roots

        if str(resolved).startswith("/usr/"):
            return roots
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

    def _child_env(self, token: str, bot_db_path: str, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "BOT_TOKEN": token,
            "BOT_DB_PATH": bot_db_path,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        if extra_env:
            for key, value in extra_env.items():
                if key not in env:
                    env[key] = value
        return env

    async def start_bot(self, bot_id: int) -> None:
        if bot_id in self.active:
            logger.info("Start skipped; bot already active: bot_id=%s", bot_id)
            return

        bot = self.db.get_bot(bot_id)
        if bot is None:
            logger.error("Start failed; bot does not exist: bot_id=%s", bot_id)
            raise RunnerError(f"Bot {bot_id} does not exist.")
        revision = self.db.latest_revision(bot_id)
        if revision is None or revision["validation_status"] != "ok":
            logger.error("Start failed; no valid revision: bot_id=%s", bot_id)
            raise RunnerError(f"Bot {bot_id} has no valid revision.")

        bot_dir = Path(bot["workdir"])
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "bot.py").write_text(revision["code"], encoding="utf-8")
        child_db = bot_dir / "bot.sqlite3"
        extra_env = self.db.get_bot_env_vars(bot_id)
        env = self._child_env(bot["token"], "/app/bot.sqlite3" if self.settings.require_bwrap else str(child_db), extra_env)

        if self.settings.require_bwrap:
            if not self._host_python_exists(self._resolve_python_bin()):
                self.db.update_bot_status(bot_id, "python_missing")
                raise RunnerError(
                    f"Python executable '{self.settings.python_bin}' was not found. "
                    "Set PYTHON_BIN to an existing interpreter, for example .venv/bin/python."
                )
            if shutil.which(self.settings.bwrap_bin) is None:
                logger.error("Bubblewrap missing: executable=%s bot_id=%s", self.settings.bwrap_bin, bot_id)
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
        logger.info(
            "Starting child bot: bot_id=%s sandbox=%s cwd=%s command=%s",
            bot_id,
            self.settings.require_bwrap,
            cwd or "-",
            command,
        )
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
        logger.info("Child process started: bot_id=%s pid=%s", bot_id, process.pid)

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
        return_code_text = format_return_code(return_code)
        current = self.active.get(bot_id)
        if current is record:
            self.active.pop(bot_id, None)
        if record.desired_stop:
            logger.info("Child process stopped: bot_id=%s rc=%s", bot_id, return_code_text)
            self.db.add_log(bot_id, "system", f"Stopped process rc={return_code_text}", self.settings.log_tail_rows)
            return
        if is_signal_exit(return_code):
            self.db.mark_stopped(bot_id, "interrupted")
            self.db.add_log(bot_id, "system", f"Process interrupted unexpectedly rc={return_code_text}", self.settings.log_tail_rows)
            logger.warning("Child process interrupted by signal: bot_id=%s rc=%s", bot_id, return_code_text)
            await self._restart_after_unexpected_signal(bot_id, return_code_text)
            return
        self.db.mark_stopped(bot_id, "crashed")
        self.db.add_log(bot_id, "system", f"Process exited unexpectedly rc={return_code_text}", self.settings.log_tail_rows)
        logger.warning("Child process crashed/exited: bot_id=%s rc=%s", bot_id, return_code_text)

    async def _restart_after_unexpected_signal(self, bot_id: int, return_code_text: str) -> None:
        attempts = self.unexpected_signal_restarts.get(bot_id, 0)
        if attempts >= MAX_UNEXPECTED_SIGNAL_RESTARTS:
            logger.error(
                "Auto-restart skipped after repeated signal exits: bot_id=%s attempts=%s rc=%s",
                bot_id,
                attempts,
                return_code_text,
            )
            self.db.add_log(
                bot_id,
                "system",
                f"Auto-restart skipped after {attempts} signal exits.",
                self.settings.log_tail_rows,
            )
            return

        attempt_number = attempts + 1
        self.unexpected_signal_restarts[bot_id] = attempt_number
        logger.warning(
            "Auto-restarting child after unexpected signal: bot_id=%s attempt=%s/%s rc=%s",
            bot_id,
            attempt_number,
            MAX_UNEXPECTED_SIGNAL_RESTARTS,
            return_code_text,
        )
        self.db.add_log(
            bot_id,
            "system",
            f"Auto-restarting after signal exit ({attempt_number}/{MAX_UNEXPECTED_SIGNAL_RESTARTS}).",
            self.settings.log_tail_rows,
        )
        await asyncio.sleep(UNEXPECTED_SIGNAL_RESTART_DELAY_SECONDS)
        try:
            await self.start_bot(bot_id)
        except Exception as exc:
            logger.exception("Auto-restart after signal failed: bot_id=%s", bot_id)
            self.db.mark_stopped(bot_id, "restart_failed")
            self.db.add_log(bot_id, "system", f"Auto-restart after signal failed: {exc}", self.settings.log_tail_rows)

    async def stop_bot(self, bot_id: int, mark_stopped: bool = True) -> None:
        record = self.active.get(bot_id)
        if record is None:
            logger.info("Stop requested for inactive bot: bot_id=%s", bot_id)
            if mark_stopped:
                self.unexpected_signal_restarts.pop(bot_id, None)
                self.db.mark_stopped(bot_id)
            return

        record.desired_stop = True
        if mark_stopped:
            self.unexpected_signal_restarts.pop(bot_id, None)
        process = record.process
        logger.info("Stopping child process: bot_id=%s pid=%s", bot_id, process.pid)
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("Child process did not terminate; killing: bot_id=%s pid=%s", bot_id, process.pid)
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
        logger.info("Restarting child bot: bot_id=%s", bot_id)
        self.unexpected_signal_restarts.pop(bot_id, None)
        await self.stop_bot(bot_id, mark_stopped=False)
        await self.start_bot(bot_id)

    async def restore_running_bots(self) -> None:
        bots = self.db.running_bots()
        logger.info("Restoring running child bots: count=%s", len(bots))
        for bot in bots:
            try:
                await self.start_bot(int(bot["id"]))
            except Exception as exc:
                logger.exception("Restore failed: bot_id=%s", bot["id"])
                self.db.mark_stopped(int(bot["id"]), "restore_failed")
                self.db.add_log(int(bot["id"]), "system", f"Restore failed: {exc}", self.settings.log_tail_rows)

    async def stop_all(self, mark_stopped: bool = True) -> None:
        bot_ids = list(self.active)
        logger.info("Stopping all active child bots: count=%s mark_stopped=%s", len(bot_ids), mark_stopped)
        for bot_id in bot_ids:
            await self.stop_bot(bot_id, mark_stopped=mark_stopped)
        if mark_stopped:
            self.db.set_many_stopped(bot_ids)
