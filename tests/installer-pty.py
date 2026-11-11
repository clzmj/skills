"""Controlling-terminal boundary fixture, invoked by the Bun acceptance test."""
import errno
import os
import pty
import select
import shlex
import signal
import sys
import time


def exercise(shell, directory, mode):
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(directory)
        command = 'cat ' + shlex.quote(shell) + ' | sh'
        if mode == 'yes':
            command += ' -s -- --yes --agents codex --core-only --no-hooks'
        os.execv('/bin/sh', ['sh', '-c', command])
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
                    os.write(fd, b'\x04' if mode == 'eof' else b'\n\n\n\n\n\n' + (b'yes\n' if mode == 'accept' else b'no\n'))
                    sent = True
            ended, child_status = os.waitpid(pid, os.WNOHANG)
            if ended:
                status = child_status
                break
        else:
            raise AssertionError('PTY installer did not terminate; output: ' + output[-6000:].decode(errors='replace'))
        code = os.waitstatus_to_exitcode(status)
        assert sent or mode == 'yes', output.decode(errors='replace')
        if mode == 'eof':
            assert code != 0 and b'input ended' in output.lower(), output.decode(errors='replace')
        elif mode == 'cancel':
            assert code == 0 and b'Cancelled' in output, output.decode(errors='replace')
        else:
            assert code == 0 and b'Summary:' in output and b'0 failed' in output, output.decode(errors='replace')
    finally:
        os.close(fd)
        if status is None or not os.WIFEXITED(status):
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except ProcessLookupError:
                pass


mode = sys.argv[3] if len(sys.argv) > 3 else 'boundary'
if mode == 'boundary':
    exercise(sys.argv[1], sys.argv[2], 'cancel')
    exercise(sys.argv[1], sys.argv[2], 'eof')
    print('Hosted pipeline cancel and EOF passed')
else:
    assert mode in ('accept', 'yes'), mode
    exercise(sys.argv[1], sys.argv[2], mode)
    print('Hosted pipeline installation passed: ' + mode)
