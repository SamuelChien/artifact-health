"""Persistent parser processes with bounded requests and no source execution."""
import json
import os
import select
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path


class ParserWorkers:
    def __init__(self, count, directory):
        self.count = count
        self.directory = Path(directory)
        self.local = threading.local()
        self.processes = []
        self.lock = threading.Lock()

    def query(self, snapshot, timeout):
        process = getattr(self.local, "process", None)
        if process is None or process.poll() is not None:
            environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT") if key in os.environ}
            process = subprocess.Popen([sys.executable, "-E", "-B", str(Path(__file__).with_name("measure.py")), "--probe-stream"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       cwd=self.directory, env=environment)
            self.local.process = process
            with self.lock:
                self.processes.append(process)
        try:
            process.stdin.write((json.dumps(str(snapshot)) + "\n").encode())
            process.stdin.flush()
            deadline = time.monotonic() + timeout
            response = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    raise TimeoutError("parser request exceeded its deadline")
                chunk = os.read(process.stdout.fileno(), 1024 * 1024)
                if not chunk:
                    raise ValueError("parser process ended without a response")
                response.extend(chunk)
                if response.endswith(b"\n"):
                    return json.loads(response)
        except BaseException:
            self._close(process)
            self.local.process = None
            raise

    @staticmethod
    def _close(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def close(self):
        for process in self.processes:
            self._close(process)

    def map(self, function, items):
        """Bound outstanding work instead of enqueueing every archive file at once."""
        iterator = iter(items)
        with ThreadPoolExecutor(max_workers=self.count) as executor:
            pending = set()
            for _ in range(self.count * 2):
                item = next(iterator, None)
                if item is not None:
                    pending.add(executor.submit(function, item))
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    yield future.result()
                    item = next(iterator, None)
                    if item is not None:
                        pending.add(executor.submit(function, item))
