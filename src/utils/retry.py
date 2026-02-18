import time
import functools
from utils.logger import get_logger

log = get_logger(__name__)


def retry(
    max_retries: int = 3,
    backoff: float = 2.0,
    initial_delay: float = 1.0,
    exceptions: tuple = (Exception,),
):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exc = None
            for attempt in range(1, max_retries + 2):  # +2 = intento original + retries
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt > max_retries:
                        break
                    log.warning(
                        "%s intento %d/%d falló: %s → reintentando en %.1fs",
                        func.__name__, attempt, max_retries + 1, e, delay,
                    )
                    time.sleep(delay)
                    delay *= backoff
            log.error(
                "%s agotó %d intentos. Último error: %s",
                func.__name__, max_retries + 1, last_exc,
            )
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator
