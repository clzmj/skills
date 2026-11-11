"""Controlling-terminal boundary fixture, invoked by the Bun acceptance test."""
import errno
import os
import pty
import select
import shlex
import signal
import sys
import time


def exercise(shell, directory, eof):
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(directory)
        os.execv('/bin/sh', ['sh', '-c', 'cat ' + shlex.quote(shell) + ' | sh'])
    output = b''
    sent = False
    status = None
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    data = os.read(fd, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    data = b''
                if data:
                    output += data
                if not sent and b'Select numbers/names separated by commas' in output:
                    # Defaults for hosts/scope/method/core/favorites/hooks, then
                    # decline the final confirmation. Ctrl-D tests true tty EOF.
                    os.write(fd, b'\x04' if eof else b'\n\n\n\n\n\nno\n')
                    sent = True
            ended, status = os.waitpid(pid, os.WNOHANG)
            if ended:
                break
        else:
            raise AssertionError('PTY installer did not terminate; output: ' + output[-6000:].decode(errors='replace'))
        code = os.waitstatus_to_exitcode(status)
        assert sent, output.decode(errors='replace')
        if eof:
            assert code != 0 and b'input ended' in output.lower(), output.decode(errors='replace')
        else:
            assert code == 0 and b'Cancelled' in output, output.decode(errors='replace')
    finally:
        os.close(fd)
        if status is None or not os.WIFEXITED(status):
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except ProcessLookupError:
                pass


exercise(sys.argv[1], sys.argv[2], False)
exercise(sys.argv[1], sys.argv[2], True)
print('Hosted pipeline cancel and EOF passed')
