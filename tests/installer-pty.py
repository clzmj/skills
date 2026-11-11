"""Raw-key PTY boundary checks; no network, host config, or line-answer mocks."""
import errno
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import shlex
import signal
import struct
import sys
import termios
import time

ANSI = re.compile(rb'\x1b\[[0-?]*[ -/]*[@-~]')
def diagnostic(output):
    return ANSI.sub(b'', output[-7000:]).decode(errors='replace').replace('\r', '')


ENTER, DOWN, END, HOME = b'\r', b'\x1b[B', b'\x1b[F', b'\x1b[H'


class Session:
    def __init__(self, shell, directory, mode):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(directory)
            command = 'cat ' + shlex.quote(shell) + ' | sh'
            if mode == 'yes':
                command += ' -s -- --yes --agents codex --core-only --no-hooks'
            os.execv('/bin/sh', ['sh', '-c', command])
        self.rows, self.columns = (16, 62) if mode in ('catalog', 'backpressure') else (24, 90)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', self.rows, self.columns, 0, 0))
        self.output = b''
        self.mark = 0
        self.status = None

    def read(self):
        if select.select([self.fd], [], [], 0.03)[0]:
            try:
                data = os.read(self.fd, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                data = b''
            self.output += data
        ended, status = os.waitpid(self.pid, os.WNOHANG) if self.status is None else (0, None)
        if ended:
            self.status = status

    def expect(self, text, timeout=12):
        token = text.encode() if isinstance(text, str) else text
        deadline = time.monotonic() + timeout
        while token not in self.output[self.mark:]:
            self.read()
            if self.status is not None or time.monotonic() > deadline:
                raise AssertionError('Expected ' + repr(text) + '; output: ' + diagnostic(self.output))
        # Drain the rest of the single rendered frame before assertions/keys.
        self.read()
        return self.frame()

    def frame(self):
        frame = self.output.split(b'\x1b[H\x1b[2J')[-1]
        return ANSI.sub(b'', frame).decode(errors='replace')

    def send(self, keys):
        self.mark = len(self.output)
        os.write(self.fd, keys)

    def settle(self):
        deadline, quiet = time.monotonic() + 3, time.monotonic()
        while time.monotonic() < deadline:
            before = len(self.output)
            self.read()
            if len(self.output) != before:
                quiet = time.monotonic()
            elif time.monotonic() - quiet > 0.15:
                return self.frame()
        raise AssertionError('Selector never settled after key burst')

    def page(self, title, keys=ENTER):
        self.expect(title)
        self.send(keys)

    def finish(self, expected_success=True):
        deadline = time.monotonic() + 12
        while self.status is None and time.monotonic() < deadline:
            self.read()
        assert self.status is not None, diagnostic(self.output)
        code = os.waitstatus_to_exitcode(self.status)
        assert (code == 0) == expected_success, (code, diagnostic(self.output))
        if b'\x1b[?1049h' in self.output:
            assert self.output.rfind(b'\x1b[?25h') > self.output.rfind(b'\x1b[?25l'), 'Cursor not restored'
            assert self.output.rfind(b'\x1b[?1049l') > self.output.rfind(b'\x1b[?1049h'), 'Alternate screen not restored'
            flags = termios.tcgetattr(self.fd)[3]
            assert flags & termios.ICANON and flags & termios.ECHO, 'Terminal left in raw/no-echo mode'
        # Each full page is bounded even when list content exceeds the viewport.
        for frame in self.output.split(b'\x1b[H\x1b[2J')[1:]:
            frame = frame.split(b'\x1b[?1049l')[0]
            assert len(frame.split(b'\r\n')) <= self.rows, 'Page scrolls beyond viewport'
        return code

    def close(self):
        if self.status is None:
            try:
                os.killpg(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            except ProcessLookupError:
                pass
        os.close(self.fd)


def core_pages(session):
    session.page('Agents', b'a' + ENTER)
    session.page('Installation scope')
    session.page('Installation method')
    session.page('Core skills', b'a' + ENTER)
    session.page('Favorite packs', b'n' + ENTER)
    session.page('Hooks', b'n')
    session.expect('Review installation')


def progress(session, cancelled=False, external_signal=False):
    session.expect('Installing')
    active = Path(os.environ['FIXTURE_ACTIVE'])
    deadline = time.monotonic() + 8
    frames = set()
    while time.monotonic() < deadline:
        session.read()
        if active.exists() and active.read_text().isdigit():
            frame = session.frame()
            if 'elapsed' in frame and re.search(r'\d+/\d+', frame):
                frames.add(frame)
            if cancelled and frames:
                child = int(active.read_text())
                if external_signal:
                    os.kill(int(Path(os.environ['FIXTURE_INITIALIZER_PID']).read_text()), signal.SIGTERM)
                else:
                    session.send(b'\x03')
                started = time.monotonic()
                session.finish(False)
                assert time.monotonic() - started < 2, 'Active operation cancellation took more than two seconds'
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    descendant = Path(os.environ['FIXTURE_GRANDCHILD'])
                    if descendant.exists():
                        try:
                            os.kill(int(descendant.read_text()), 0)
                        except ProcessLookupError:
                            return
                        raise AssertionError('Grandchild still running after cancellation')
                    return
                raise AssertionError('Child still running after cancellation')
        if any(title in session.frame() for title in ['Installation complete', 'Installation needs attention', 'Installation failed']):
            break
        if session.status is not None:
            break
    assert len(frames) >= 2, 'Progress did not repaint while a real child was active'
    assert b'FIXTURE_CHILD_LOG_SENTINEL' not in re.split(b'Installation needs attention|Installation failed', session.output)[0], 'Child logs destroyed page layout'


def catalog_pages(session, cancel_discovery=False, burst=False):
    frame = session.expect('Agents')
    assert '2 selected' in frame
    session.send(b'n')
    assert '0 selected' in session.expect('Agents')
    session.send(ENTER)
    assert 'Select at least' in session.expect('Agents'), 'Empty agents should stay on the selector with an inline explanation'
    session.send(b'a')
    assert '2 selected' in session.expect('Agents')
    session.send(ENTER)
    session.page('Installation scope', DOWN + ENTER)
    # Browse a real existing child directory, without entering any path string.
    session.page('Project directory', DOWN + DOWN + ENTER)
    session.page('Project directory', ENTER)
    session.page('Installation method', END + ENTER)
    session.page('Core skills', b' ')
    assert '1 selected' in session.expect('Core skills')
    session.send(b'a' + ENTER)
    session.page('Favorite packs', END + b' ' + ENTER)
    if cancel_discovery:
        session.expect('Discovering mattpocock/skills')
        started = time.monotonic()
        session.send(b'\x03')
        session.finish(False)
        assert time.monotonic() - started < 2, 'Discovery cancellation took more than two seconds'
        return
    session.expect('Individual skills: mattpocock/skills')
    if burst:
        # Keep the PTY consumer stopped while hundreds of full-page redraws
        # fill the output buffer. A nonblocking short-write bug loses footers.
        session.send((DOWN + b'\x1b[A') * 80)
        time.sleep(0.5)
        frame = session.settle()
        assert 'Individual skills: mattpocock/skills' in frame
        assert 'Ctrl-C Cancel' in frame, 'Footer was dropped under real PTY backpressure'
    session.send(END)
    frame = session.expect('lesson-39')
    assert '39/39' in frame and '39 selected' in frame
    session.send(HOME)
    session.expect('lesson-01')
    session.send(b'\x1b[6~')
    session.expect('Individual skills: mattpocock/skills')
    session.send(b'n')
    assert '0 selected' in session.expect('Individual skills: mattpocock/skills')
    session.send(b'a' + ENTER)
    session.expect('Review installation')
    # Back returns the completed catalog selection, preserving all 39 choices.
    session.send(b'\x1b')
    assert '39 selected' in session.expect('Individual skills: mattpocock/skills')
    session.send(ENTER)
    session.expect('Review installation')
    session.send(b'y')
    progress(session)
    session.page('Installation complete')
    session.finish()


def exercise(shell, directory, mode):
    session = Session(shell, directory, mode)
    try:
        if mode == 'yes':
            session.expect('Summary:')
            session.finish()
        elif mode in ('eof', 'interrupt', 'signal', 'back-cancel'):
            session.expect('Agents')
            assert not termios.tcgetattr(session.fd)[3] & termios.ICANON, 'Selector never entered raw mode'
            if mode == 'signal':
                os.kill(int(Path(os.environ['FIXTURE_INITIALIZER_PID']).read_text()), signal.SIGTERM)
            elif mode == 'back-cancel':
                # A split arrow sequence must move rather than behave as Esc.
                session.send(b'\x1b[')
                time.sleep(0.01)
                os.write(session.fd, b'B')
                session.expect('Agents')
                session.send(b'\x1b')
                session.expect('Agents')
                session.send(b'\x03')
            else:
                session.send(b'\x04' if mode == 'eof' else b'\x03')
            session.finish(False)
        elif mode in ('catalog', 'discovery-cancel', 'backpressure'):
            catalog_pages(session, mode == 'discovery-cancel', mode == 'backpressure')
        elif mode == 'real-project':
            # One-off official Skills CLI proof uses the same raw-key driver;
            # routine tests continue to use only disposable command fixtures.
            session.page('Agents', b'a' + ENTER)
            session.page('Installation scope', DOWN + ENTER)
            session.page('Project directory')
            session.page('Installation method')
            session.page('Core skills', b'a' + ENTER)
            session.page('Favorite packs', b'n' + ENTER)
            session.page('Review installation', b'y')
            frame = session.expect('Installation complete', timeout=60)
            assert 'Changed path:' not in frame, 'Interactive success should show a concise result'
            if os.environ.get('FIXTURE_CAPTURE_COMPLETION'):
                print('Official CLI completion frame:\n' + frame)
            session.send(ENTER)
            session.finish()
        else:
            core_pages(session)
            session.send(b'n' if mode == 'reject' else b'y')
            if mode == 'reject':
                session.page('Installation cancelled')
                session.finish()
            else:
                progress(session, mode in ('progress-cancel', 'tree-cancel', 'stubborn-tree-cancel', 'signal-tree'), mode == 'signal-tree')
                if mode not in ('progress-cancel', 'tree-cancel', 'stubborn-tree-cancel', 'signal-tree'):
                    session.page('Installation failed' if mode == 'failure' else 'Installation complete')
                    session.finish(mode != 'failure')
        print('PTY scenario passed: ' + mode)
    finally:
        session.close()
        # Clean only PIDs explicitly recorded by this disposable fixture, even
        # when the test exposed an unreaped descendant holding inherited pipes.
        for variable in ['FIXTURE_ACTIVE', 'FIXTURE_GRANDCHILD']:
            marker = Path(os.environ[variable])
            if marker.exists() and marker.read_text().isdigit():
                try:
                    os.kill(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


exercise(sys.argv[1], sys.argv[2], sys.argv[3])
