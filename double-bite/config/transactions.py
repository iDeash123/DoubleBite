import logging
from collections.abc import Callable
from functools import partial
from typing import Any

from django.db import transaction

logger = logging.getLogger('config')


def schedule_on_commit(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Schedule a side-effect callback to run only after the current transaction commits.

    Project standard: inside any ``with transaction.atomic():`` block, direct calls
    to ``send_templated_email``, ``stripe.*``, or ``Mistral(...)`` are prohibited;
    use ``schedule_on_commit(...)`` so external I/O executes only after commit and
    failures in post-commit side effects are logged with traceback without rolling
    back committed payments or orders.
    """
    if not callable(callback):
        raise TypeError('callback must be callable')

    bound_callback = partial(callback, *args, **kwargs)
    callback_name = getattr(callback, '__qualname__', getattr(callback, '__name__', repr(callback)))

    def _run_safely() -> None:
        try:
            bound_callback()
        except Exception:
            logger.exception('Post-commit callback %s failed', callback_name)

    transaction.on_commit(_run_safely)


run_on_commit = schedule_on_commit
