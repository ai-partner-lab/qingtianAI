from __future__ import annotations

import fcntl
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


def read_pid(path: Path) -> Optional[int]:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return value if value > 0 else None


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Optional[Any] = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError("control-plane server is already running") from exc
        self.handle = handle
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()

    def release(self) -> None:
        if not self.handle:
            return
        try:
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.flush()
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def active_instance_pid(run_dir: Path) -> Optional[int]:
    """Return the PID recorded by the process that currently owns the lock."""
    lock_path = run_dir / "instance.lock"
    try:
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError:
        return None
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            raw = handle.read().strip()
            try:
                pid = int(raw)
            except ValueError:
                return None
            return pid if pid > 0 and pid_is_alive(pid) else None
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return None


def publish_server_pid(run_dir: Path, pid: int) -> None:
    """Atomically publish the PID only after the server has bound its socket."""
    run_dir.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=".server.pid.", dir=str(run_dir))
    temporary = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(pid))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(str(temporary), str(run_dir / "server.pid"))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def remove_server_pid(run_dir: Path, expected_pid: Optional[int] = None) -> None:
    pid_path = run_dir / "server.pid"
    if expected_pid is not None and read_pid(pid_path) != expected_pid:
        return
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def cleanup_runtime_files_if_idle(run_dir: Path) -> bool:
    """Clear stale PID/lock contents while serializing against a new server."""
    lock_path = run_dir / "instance.lock"
    try:
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError:
        return False
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            remove_server_pid(run_dir)
            handle.seek(0)
            handle.truncate()
            handle.flush()
            return True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
