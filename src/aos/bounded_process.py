from contextlib import ExitStack
import math
import os
import selectors
import subprocess
import time


def run_bounded(arguments, *, input: bytes, env: dict, timeout: float = 120, max_output: int = 65536):
    if (not isinstance(arguments, (list, tuple)) or not arguments
            or any(not isinstance(argument, str) or '\x00' in argument for argument in arguments)
            or not arguments[0]):
        raise ValueError('invalid_process_arguments')
    if not isinstance(input, bytes) or len(input) > 1048576:
        raise ValueError('invalid_process_input')
    if (not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                       or not key or '=' in key or '\x00' in key or '\x00' in value
                                       for key, value in env.items())):
        raise ValueError('invalid_process_environment')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('invalid_process_timeout')
    if type(max_output) is not int or max_output <= 0:
        raise ValueError('invalid_process_output_limit')
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               shell=False, env=env, bufsize=0, close_fds=True)
    selector = None
    stdout = bytearray()
    stderr = bytearray()
    position = 0
    try:
        selector = selectors.DefaultSelector()
        for stream, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if input:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
        else:
            process.stdin.close()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout)
            for key, _events in selector.select(remaining):
                stream = key.fileobj
                if key.data == 'stdin':
                    try:
                        position += os.write(stream.fileno(), input[position:position + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        position = len(input)
                    if position == len(input):
                        selector.unregister(stream)
                        stream.close()
                    continue
                try:
                    chunk = os.read(stream.fileno(), min(65536, max_output - len(stdout) - len(stderr) + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if len(stdout) + len(stderr) + len(chunk) > max_output:
                    raise ValueError('process_output_limit')
                (stdout if key.data == 'stdout' else stderr).extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(arguments, timeout)
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(arguments, timeout) from None
        return subprocess.CompletedProcess(arguments, returncode, bytes(stdout), bytes(stderr))
    finally:
        try:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            process.wait()
        finally:
            with ExitStack() as cleanup:
                for stream in (process.stdin, process.stdout, process.stderr):
                    cleanup.callback(stream.close)
                if selector is not None:
                    cleanup.callback(selector.close)
