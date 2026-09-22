"""Isolated runner for generated code and trusted tests.

Only the Docker runner executes candidate code. The container runs with the network disabled, as a
non-root user, with CPU/memory/pid limits, no credentials, no Docker socket, the candidate mounted
read-only at /work/candidate and trusted tests mounted read-only at /work/trusted_tests. Subprocess path
filtering is never treated as a sandbox; if Docker is unavailable, execution is refused and reported.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import TRUSTED_TESTS_DIR
from .policy import fixed_command, worker_env
from .util import truncate


@dataclass
class RunResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    container_id: str
    tool_versions: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_dict(self) -> dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "container_id": self.container_id,
            "tool_versions": self.tool_versions,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class RunnerUnavailable(RuntimeError):
    pass


class DockerRunner:
    def __init__(self, image: str, trusted_tests_dir: Path = TRUSTED_TESTS_DIR, timeout_s: float = 120.0, max_output: int = 200_000):
        self.image = image
        self.trusted_tests_dir = Path(trusted_tests_dir).resolve()
        self.timeout_s = timeout_s
        self.max_output = max_output
        self._active: dict[str, subprocess.Popen] = {}

    def check_available(self) -> str:
        if not shutil.which("docker"):
            raise RunnerUnavailable("docker CLI not found; generated code execution is blocked")
        p = subprocess.run(["docker", "image", "inspect", self.image, "--format", "{{.Id}}"], capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            raise RunnerUnavailable(f"worker image {self.image} not built; run `make worker-image`")
        return p.stdout.strip()

    def docker_args(
        self,
        candidate_dir: Path,
        name: str,
        stage: str = "A",
        extra_env: dict[str, str] | None = None,
        rw_mounts: dict[Path, str] | None = None,
    ) -> list[str]:
        env = worker_env({"FORGEFLOW_STAGE": stage, **(extra_env or {})})
        args = [
            "docker", "run", "--rm", "--name", name,
            "--network", "none",
            "--user", "65534:65534",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", "512m", "--cpus", "1", "--pids-limit", "256",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=128m,mode=1777",
            "-v", f"{Path(candidate_dir).resolve()}:/work/candidate:ro",
            "-v", f"{self.trusted_tests_dir}:/work/trusted_tests:ro",
            "-w", "/work/candidate",
        ]  # fmt: skip
        for host, guest in (rw_mounts or {}).items():
            args += ["-v", f"{Path(host).resolve()}:{guest}:rw"]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        args.append(self.image)
        return args

    def run(
        self,
        candidate_dir: Path,
        command_name: str,
        stage: str = "A",
        timeout_s: float | None = None,
        on_start=None,
        extra_env: dict[str, str] | None = None,
        rw_mounts: dict[Path, str] | None = None,
    ) -> RunResult:
        self.check_available()
        cmd = fixed_command(command_name)
        name = f"forgeflow-{uuid.uuid4().hex[:12]}"
        args = self.docker_args(candidate_dir, name, stage=stage, extra_env=extra_env, rw_mounts=rw_mounts) + cmd
        timeout = timeout_s or self.timeout_s
        started = time.monotonic()
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")},  # docker CLI never sees provider credentials
            start_new_session=True,
        )
        self._active[name] = proc
        if on_start:
            on_start(name, proc.pid)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.kill(name)
            out, err = proc.communicate()
        finally:
            self._active.pop(name, None)
        return RunResult(
            command=" ".join(cmd),
            exit_code=proc.returncode if not timed_out else 124,
            stdout=truncate(out or "", self.max_output),
            stderr=truncate(err or "", self.max_output),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            container_id=name,
            tool_versions=self.tool_versions(),
        )

    def run_migration(self, candidate_dir: Path, demo_dir: Path, timeout_s: float | None = None, on_start=None) -> RunResult:
        """Apply the candidate's schema migration to the copied demo DB via the trusted migration tool."""
        return self.run(
            candidate_dir, "migrate", stage="C", timeout_s=timeout_s, on_start=on_start, rw_mounts={Path(demo_dir): "/work/demo"}
        )

    def kill(self, name: str) -> None:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=30)
        proc = self._active.get(name)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass

    def kill_all(self) -> list[str]:
        names = list(self._active)
        for n in names:
            self.kill(n)
        return names

    def tool_versions(self) -> dict:
        try:
            p = subprocess.run(
                ["docker", "image", "inspect", self.image, "--format", '{{index .Config.Labels "forgeflow.tools"}}'],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {"image": self.image, "tools": p.stdout.strip()}
        except (OSError, subprocess.SubprocessError):
            return {"image": self.image}
