#!/usr/bin/env python3
"""Self-contained MCP stdio smoke test for registry builders (Glama, etc.).

Spawns the canonical core entrypoint, runs the JSON-RPC handshake
(``initialize`` -> ``notifications/initialized`` -> ``tools/list``) over
newline-delimited stdio, and asserts a non-empty ``tools`` array. Prints a
one-line PASS/FAIL summary and exits 0 on success, non-zero otherwise.

Stdlib only — no third-party deps, so it runs on a fresh ``pip install .``
(core, no extras) exactly like the Dockerfile CMD does.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

PROTOCOL_VERSION = "2025-06-18"
OVERALL_TIMEOUT = 60.0  # hard watchdog; the handshake normally takes < 1s


def _server_command() -> list[str]:
    """The command a registry builder runs: the installed console script.

    Falls back to importing the module so the probe also works when run
    from the source tree before an install (e.g. local dev)."""
    exe = shutil.which("sqlite-memory-mcp")
    if exe:
        return [exe]
    return [sys.executable, "-c", "import server; server.main()"]


def _send(proc: subprocess.Popen, msg: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def _read_response(proc: subprocess.Popen, want_id: int) -> dict:
    """Read newline-delimited JSON until the reply with ``want_id`` arrives.

    A killed/exited server closes stdout -> readline() returns "" -> we
    raise. The watchdog timer guarantees this cannot block forever."""
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if line == "":
            raise RuntimeError("server closed stdout before replying")
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # ignore any non-JSON line that leaks onto stdout
        if obj.get("id") == want_id:
            return obj


def main() -> int:
    cmd = _server_command()

    # Hermetic DB so the smoke never touches a real ~/.claude/memory DB.
    db_dir = tempfile.mkdtemp(prefix="registry_smoke_")
    env = dict(os.environ)
    env["SQLITE_MEMORY_DB"] = os.path.join(db_dir, "memory.db")

    stderr_path = os.path.join(db_dir, "server_stderr.log")
    stderr_file = open(stderr_path, "w+", encoding="utf-8")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_file,
        text=True,
        env=env,
        bufsize=1,
    )

    def _watchdog() -> None:
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(OVERALL_TIMEOUT, _watchdog)
    timer.start()

    try:
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "registry-smoke", "version": "1.0"},
            },
        })
        init = _read_response(proc, 1)
        if "result" not in init:
            raise RuntimeError(f"initialize failed: {init!r}")

        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools_resp = _read_response(proc, 2)
        tools = tools_resp.get("result", {}).get("tools", [])
        if not isinstance(tools, list) or not tools:
            raise RuntimeError(f"tools/list returned no tools: {tools_resp!r}")

        server_name = init["result"].get("serverInfo", {}).get("name", "?")
        print(
            f"PASS registry_smoke: server={server_name!r} "
            f"cmd={cmd[0]!r} tools={len(tools)}"
        )
        return 0
    except Exception as exc:
        stderr_file.flush()
        stderr_file.seek(0)
        tail = stderr_file.read()[-2000:]
        print(f"FAIL registry_smoke: {exc}", file=sys.stderr)
        if tail.strip():
            print("--- server stderr (tail) ---", file=sys.stderr)
            print(tail, file=sys.stderr)
        return 1
    finally:
        timer.cancel()
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        stderr_file.close()
        shutil.rmtree(db_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
