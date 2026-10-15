"""The MCP stdio reader must not block in a read on fd 0 (Windows).

A native library that touches fd 0 while it loads (scipy's OpenBLAS ships the
gfortran runtime) otherwise waits until the client sends its next message.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHILD = """
import os, stat, sys
import unified_server
unified_server._serve_stdio_from_private_stdin()
line = sys.stdin.readline().strip()
fd0 = os.fstat(0).st_mode
print(line, stat.S_ISCHR(fd0), stat.S_ISFIFO(fd0), sep="|")
"""


def test_stdio_reads_a_private_stdin_and_fd0_points_at_nul_on_windows():
    run = subprocess.run(
        [sys.executable, "-c", CHILD],
        cwd=ROOT,
        input="hello\n",
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    line, fd0_is_char_device, fd0_is_pipe = (
        run.stdout.strip().splitlines()[-1].split("|")
    )
    # The transport still receives the client's bytes...
    assert line == "hello"
    if sys.platform == "win32":
        # ...but no longer through fd 0, which is now the NUL device.
        assert (fd0_is_char_device, fd0_is_pipe) == ("True", "False")
    else:
        assert (fd0_is_char_device, fd0_is_pipe) == ("False", "True")
