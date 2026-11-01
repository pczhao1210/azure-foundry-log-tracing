import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "start.sh"
APP = "backend.app:create_app"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("ss") is None,
    reason="start.sh process detection is exercised on Linux with ss",
)

# Fake listener: binds an ephemeral port, prints it, and optionally ignores SIGTERM.
LISTENER = """
import signal, socket, sys, time
if sys.argv[1] == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
server = socket.socket()
server.bind(("127.0.0.1", 0))
server.listen()
print(server.getsockname()[1], flush=True)
time.sleep(120)
"""
UV_SHIM = """\
case "$*" in
  *Settings*) printf '%s\\n' "$FAKE_DATABASE" ;;
  *) printf 'uv %s\\n' "$*" >> "$CALL_LOG" ;;
esac
"""
NPM_SHIM = """\
printf 'npm %s\\n' "$*" >> "$CALL_LOG"
exit "${NPM_EXIT:-0}"
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Lab:
    root: Path
    database_setting: str = ".data/lab.sqlite3"
    processes: list[subprocess.Popen[str]] = field(default_factory=list)

    @property
    def src(self) -> Path:
        return self.root / "frontend" / "src" / "main.tsx"

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "CALL_LOG": str(self.root / "calls.log"),
            "FAKE_DATABASE": self.database_setting,
            **env,
        }
        return subprocess.run(
            [str(self.root / "start.sh"), *args],
            cwd=self.root.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> list[str]:
        log = self.root / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def started(self) -> bool:
        return any(call.startswith("uv run --frozen uvicorn") for call in self.calls())

    def listener(
        self, marker: str = APP, cwd: Path | None = None, ignore_term: bool = False
    ) -> tuple[subprocess.Popen[str], int]:
        proc = subprocess.Popen(
            [sys.executable, "-c", LISTENER, "ignore-term" if ignore_term else "normal", marker],
            cwd=cwd or self.root,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.processes.append(proc)
        assert proc.stdout is not None
        return proc, int(proc.stdout.readline())

    def database_files(self) -> list[Path]:
        base = self.root / ".data" / "lab.sqlite3"
        files = [base, *(base.with_name(f"{base.name}{suffix}") for suffix in ("-wal", "-shm"))]
        files.append(base.with_name(f"{base.name}-journal"))
        for file in files:
            file.write_text("data")
        return files

    def make_stale(self) -> None:
        later = time.time() + 60
        os.utime(self.src, (later, later))


@pytest.fixture
def lab(tmp_path: Path):
    root = (tmp_path / "lab").resolve()
    for folder in ("bin", ".data", "frontend/src", "frontend/dist", "frontend/node_modules"):
        (root / folder).mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "start.sh")
    for name, body in (("uv", UV_SHIM), ("npm", NPM_SHIM)):
        shim = root / "bin" / name
        shim.write_text(f"#!/usr/bin/env bash\n{body}")
        shim.chmod(0o755)
    (root / "frontend" / "src" / "main.tsx").write_text("")
    (root / "frontend" / "dist" / "index.html").write_text("")
    environment = Lab(root)
    yield environment
    for proc in environment.processes:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()


def test_help_and_invalid_arguments(lab: Lab):
    helped = lab.run("--help")
    assert helped.returncode == 0 and "--new" in helped.stdout and "new" in helped.stdout
    assert "--stop" in helped.stdout and "--start" in helped.stdout
    bad = (["bogus"], ["--port", "abc"], ["--port", "0"], ["--port", "70000"])
    conflicts = (["stop", "new"], ["--new", "--stop"], ["start", "stop"], ["--stop", "--start"])
    for args in (*bad, *conflicts, ["--host", "-x"], ["--port"]):
        result = lab.run(*args)
        assert result.returncode == 2, args
        assert "用法" in result.stderr
    assert lab.calls() == []


@pytest.mark.parametrize("args", [(), ("start",), ("--start",), ("start", "--start")])
def test_start_is_the_default_action(lab: Lab, args: tuple[str, ...]):
    port = free_port()
    assert lab.run(*args, "--port", str(port)).returncode == 0
    assert lab.calls() == [
        f"uv run --frozen uvicorn {APP} --factory --host 127.0.0.1 --port {port}"
    ]


@pytest.mark.parametrize("flag", ["stop", "--stop"])
def test_stop_stops_the_backend_without_restarting(lab: Lab, flag: str):
    lab.make_stale()
    files = lab.database_files()
    proc, port = lab.listener()
    result = lab.run(flag, "--port", str(port))
    assert result.returncode == 0, result.stderr
    assert proc.wait(timeout=5) == -signal.SIGTERM
    assert f"PID {proc.pid}" in result.stdout
    assert all(file.exists() for file in files)
    assert lab.calls() == []


def test_stop_escalates_to_sigkill(lab: Lab):
    proc, port = lab.listener(ignore_term=True)
    result = lab.run("stop", "--port", str(port), STOP_TIMEOUT="1")
    assert result.returncode == 0, result.stderr
    assert proc.wait(timeout=5) == -signal.SIGKILL
    assert "强制结束" in result.stderr


def test_stop_without_a_running_backend_succeeds(lab: Lab):
    result = lab.run("stop", "--port", str(free_port()))
    assert result.returncode == 0, result.stderr
    assert "无需停止" in result.stdout
    assert lab.calls() == []


def test_stop_never_kills_a_foreign_listener(lab: Lab):
    proc, port = lab.listener(marker="unrelated-service")
    result = lab.run("stop", "--port", str(port))
    assert result.returncode == 0, result.stderr
    assert proc.poll() is None
    assert "无需停止" in result.stdout
    assert f"PID {proc.pid}" in result.stderr
    assert lab.calls() == []


def test_port_with_a_leading_zero_is_decimal_not_octal(lab: Lab):
    assert lab.run("--port", "08").returncode == 0
    assert lab.calls() == [f"uv run --frozen uvicorn {APP} --factory --host 127.0.0.1 --port 8"]


def test_starts_uvicorn_with_the_requested_address(lab: Lab):
    port = free_port()
    assert lab.run("--port", str(port), "--host", "127.0.0.1").returncode == 0
    assert lab.run(f"--port={port}", "--host=127.0.0.1").returncode == 0
    command = f"uv run --frozen uvicorn {APP} --factory --host 127.0.0.1 --port {port}"
    assert lab.calls() == [command, command]


def test_stops_leftover_backend_then_starts(lab: Lab):
    proc, port = lab.listener()
    result = lab.run("--port", str(port))
    assert result.returncode == 0, result.stderr
    assert proc.wait(timeout=5) == -signal.SIGTERM
    assert f"PID {proc.pid}" in result.stdout
    assert lab.started()


def test_escalates_to_sigkill_when_leftover_ignores_sigterm(lab: Lab):
    proc, port = lab.listener(ignore_term=True)
    result = lab.run("--port", str(port), STOP_TIMEOUT="1")
    assert result.returncode == 0, result.stderr
    assert proc.wait(timeout=5) == -signal.SIGKILL
    assert "强制结束" in result.stderr
    assert lab.started()


def test_never_kills_a_foreign_listener(lab: Lab):
    proc, port = lab.listener(marker="unrelated-service")
    result = lab.run("--port", str(port))
    assert result.returncode == 1
    assert proc.poll() is None
    assert f"PID {proc.pid}" in result.stderr
    assert not lab.started()


def test_never_kills_when_the_owner_cannot_be_identified(lab: Lab):
    # ss hides the owner of sockets that belong to other users; the script must not guess.
    shim = lab.root / "bin" / "ss"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'State Recv-Q Send-Q Local Peer Process\\n'\n"
        "printf 'LISTEN 0 128 127.0.0.1:1 0.0.0.0:*\\n'\n"
    )
    shim.chmod(0o755)
    result = lab.run("--port", str(free_port()))
    assert result.returncode == 1
    assert "无法识别占用进程" in result.stderr
    assert not lab.started()


def test_leaves_the_same_app_from_another_directory_alone(lab: Lab, tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc, port = lab.listener(cwd=elsewhere)
    result = lab.run("--port", str(port))
    assert result.returncode == 1
    assert proc.poll() is None
    assert not lab.started()


@pytest.mark.parametrize("flag", ["new", "--new"])
def test_new_wipes_the_database_and_its_sidecars(lab: Lab, flag: str):
    files = lab.database_files()
    keep = lab.root / ".data" / "keep.txt"
    keep.write_text("unrelated")
    result = lab.run(flag, "--port", str(free_port()))
    assert result.returncode == 0, result.stderr
    assert not any(file.exists() for file in files)
    assert keep.exists()
    assert lab.started()


def test_database_is_kept_without_new(lab: Lab):
    files = lab.database_files()
    assert lab.run("--port", str(free_port())).returncode == 0
    assert all(file.exists() for file in files)


def test_new_without_a_database_still_starts(lab: Lab):
    result = lab.run("new", "--port", str(free_port()))
    assert result.returncode == 0, result.stderr
    assert "无需清空" in result.stdout
    assert lab.started()


def test_new_refuses_a_directory_as_database(lab: Lab):
    lab.database_setting = ".data"
    result = lab.run("new", "--port", str(free_port()))
    assert result.returncode == 1
    assert "目录" in result.stderr
    assert not lab.started()


def test_new_keeps_the_database_when_the_port_stays_busy(lab: Lab):
    files = lab.database_files()
    proc, port = lab.listener(marker="unrelated-service")
    result = lab.run("new", "--port", str(port))
    assert result.returncode == 1
    assert proc.poll() is None
    assert all(file.exists() for file in files)
    assert not lab.started()


def test_builds_the_frontend_only_when_missing_or_stale(lab: Lab):
    port = str(free_port())
    assert lab.run("--port", port).returncode == 0
    assert not any(call.startswith("npm") for call in lab.calls())

    lab.make_stale()
    assert lab.run("--port", port).returncode == 0
    assert [call for call in lab.calls() if call.startswith("npm")] == [
        "npm run build --prefix frontend"
    ]

    shutil.rmtree(lab.root / "frontend" / "dist")
    shutil.rmtree(lab.root / "frontend" / "node_modules")
    assert lab.run("--port", port).returncode == 0
    assert [call for call in lab.calls() if call.startswith("npm")][1:] == [
        "npm ci --prefix frontend",
        "npm run build --prefix frontend",
    ]


def test_failed_build_leaves_the_running_backend_alone(lab: Lab):
    lab.make_stale()
    proc, port = lab.listener()
    result = lab.run("--port", str(port), NPM_EXIT="1")
    assert result.returncode == 1
    assert "前端构建失败" in result.stderr
    assert proc.poll() is None
    assert not lab.started()
