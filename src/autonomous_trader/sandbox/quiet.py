"""A dedicated, single-threaded preflight process must never emit SDK diagnostics."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import logging


class Discard:
    def write(self, text):
        return len(text)

    def flush(self):
        pass


@contextmanager
def quiet_sdk():
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(Discard()), redirect_stderr(Discard()):
            yield
    finally:
        logging.disable(previous)
