import threading

from doupool.login.browser import wait_for_identity
from doupool.login.detector import DoubaoIdentity


class EventPumpingPage:
    def __init__(self, ready, identities):
        self.ready = ready
        self.identities = identities
        self.wait_calls = 0

    def is_closed(self):
        return False

    def wait_for_timeout(self, milliseconds):
        assert milliseconds == 250
        self.wait_calls += 1
        self.identities.append(DoubaoIdentity("user-1", "莲韵"))
        self.ready.set()


def test_wait_loop_pumps_playwright_events():
    ready = threading.Event()
    identities = []
    page = EventPumpingPage(ready, identities)

    identity = wait_for_identity(page, ready, identities, threading.Event())

    assert page.wait_calls == 1
    assert identity.user_id == "user-1"
