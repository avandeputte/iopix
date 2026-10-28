"""What happens after a write, without the writers knowing who listens. The server (core) subscribes at start-up:
the index is rebuilt right away and the change watcher is woken. A leaf module: anything may import it."""
import logging
import threading

_lock = threading.Lock()
_handlers: dict = {}  # event name -> [callables]
log = logging.getLogger("app.events")


def on(event: str, fn):
    """Call fn (no arguments) whenever `event` fires."""
    with _lock:
        _handlers.setdefault(event, []).append(fn)


def fire(event: str):
    """Run every handler of `event`; one failing does not stop the others (the cause goes to the log)."""
    with _lock:
        fns = list(_handlers.get(event, ()))
    for fn in fns:
        try:
            fn()
        except Exception as e:
            log.warning("%s handler %s failed: %s", event, getattr(fn, "__name__", fn), e)


def after_write():
    """The viewer wrote into digiKam's database (or the library tables): listeners rebuild the index now rather
    than wait for the change to be noticed."""
    fire("after_write")


def kick():
    """Something changed that the change watcher should look at right away."""
    fire("kick")
