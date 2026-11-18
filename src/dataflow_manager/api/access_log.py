"""Remove untrusted request targets from Uvicorn's ordinary access records."""

import logging
import threading


class _SafeAccessFilter(logging.Filter):
    def filter(self, record):
        args = record.args
        status = args[4] if isinstance(args, tuple) and len(args) == 5 else 500
        if type(status) is not int or not 100 <= status <= 599:
            status = 500
        # Preserve Uvicorn AccessFormatter's five-argument structure while
        # discarding path, query, method and client-supplied values.
        record.msg = '%s - "%s %s HTTP/%s" %d'
        record.args = ("-", "REQUEST", "/[redacted]", "1.1", status)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


_filter = _SafeAccessFilter()
_lock = threading.Lock()
_owners = 0


def acquire_access_filter():
    global _owners
    with _lock:
        logger = logging.getLogger("uvicorn.access")
        if _owners == 0:
            logger.addFilter(_filter)
        _owners += 1


def release_access_filter():
    global _owners
    with _lock:
        if _owners:
            _owners -= 1
            if _owners == 0:
                logging.getLogger("uvicorn.access").removeFilter(_filter)
