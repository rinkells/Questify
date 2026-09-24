import logging


logger = logging.getLogger(__name__)


class EventDispatcher:
    def __init__(self):
        self._handlers = []

    def register(self, event_class, handler):
        self._handlers.append((event_class, handler))

    def dispatch(self, event):
        for event_class, handler in self._handlers:
            if not isinstance(event, event_class):
                continue
            try:
                handler(event)
            except Exception:
                logger.exception(
                    'Event handler %r failed for %s',
                    handler,
                    type(event).__name__,
                )

dispatcher = EventDispatcher()
