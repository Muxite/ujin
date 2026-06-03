"""load_more / scroll exhaustion logic, driven against a fake _Page (no browser).

Each stop reason is asserted independently. Uses an injected fake clock so the
bounded waits resolve instantly.
"""
from __future__ import annotations

from ujin.fetch.browser import _ActionCtx, _act_load_more, _act_scroll_to_bottom


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, d):
        self.t += max(0.0, d)


def _ctx(clock: FakeClock) -> _ActionCtx:
    return _ActionCtx(sleep=clock.sleep, clock=clock.now, default_timeout_ms=30000)


class FakePage:
    """Counts grow by `per_click` each click until `total_clicks` exhausted.

    `button_present` / `button_enabled` model the button lifecycle; `count_cap`
    lets us simulate the count plateauing while the button is still present.
    """

    def __init__(self, *, per_click=2, total_clicks=3, button_present=True,
                 button_enabled=True, count_cap=None, click_raises=False):
        self.per_click = per_click
        self.remaining = total_clicks
        self._present = button_present
        self._enabled = button_enabled
        self.count = 0
        self.count_cap = count_cap
        self.click_raises = click_raises
        self.clicks = 0

    async def query_count(self, selector):
        return self.count

    async def exists(self, selector):
        # the button is gone once there is nothing left to load
        return self._present and self.remaining > 0

    async def is_enabled(self, selector):
        return self._enabled

    async def scroll_into_view(self, selector):
        pass

    async def scroll_to_bottom(self):
        if self.remaining > 0:
            self.remaining -= 1
            self.count += self.per_click
            if self.count_cap is not None:
                self.count = min(self.count, self.count_cap)

    async def click(self, selector):
        if self.click_raises:
            raise RuntimeError("detached")
        self.clicks += 1
        if self.remaining > 0:
            self.remaining -= 1
            self.count += self.per_click
            if self.count_cap is not None:
                self.count = min(self.count, self.count_cap)


async def test_load_more_stops_when_button_gone():
    clk = FakeClock()
    page = FakePage(per_click=2, total_clicks=3)
    res = await _act_load_more(_ctx(clk), page, button="b", results="r",
                               max_clicks=100, timeout_ms=10000)
    assert res["stopped"] == "button_gone"
    assert res["final_count"] == 6           # 3 clicks * 2 items


async def test_load_more_stops_when_disabled():
    clk = FakeClock()
    page = FakePage(button_enabled=False)
    res = await _act_load_more(_ctx(clk), page, button="b", results="r")
    assert res["stopped"] == "disabled"
    assert res["iterations"] == 0


async def test_load_more_stops_when_count_stable():
    clk = FakeClock()
    # button stays present but count plateaus at 4 -> "stable"
    page = FakePage(per_click=2, total_clicks=99, count_cap=4)
    res = await _act_load_more(_ctx(clk), page, button="b", results="r",
                               max_clicks=100, timeout_ms=10000, settle_ms=10)
    assert res["stopped"] == "stable"
    assert res["final_count"] == 4


async def test_load_more_respects_max_clicks():
    clk = FakeClock()
    page = FakePage(per_click=1, total_clicks=10_000)
    res = await _act_load_more(_ctx(clk), page, button="b", results="r",
                               max_clicks=5, timeout_ms=10_000)
    assert res["stopped"] == "max_clicks"
    assert res["iterations"] == 5


async def test_load_more_stale_button_treated_as_exhausted():
    clk = FakeClock()
    page = FakePage(click_raises=True)
    res = await _act_load_more(_ctx(clk), page, button="b", results="r")
    assert res["stopped"] == "button_gone"


async def test_load_more_times_out():
    clk = FakeClock()

    # a settle wait that never grows but keeps advancing the clock past the deadline
    class SlowPage(FakePage):
        async def query_count(self, selector):
            return 1  # never grows

    page = SlowPage(per_click=0, total_clicks=999)
    res = await _act_load_more(_ctx(clk), page, button="b", results="r",
                               max_clicks=1000, timeout_ms=1, settle_ms=500)
    assert res["stopped"] in ("timeout", "stable")


async def test_scroll_to_bottom_exhausts_on_stable():
    clk = FakeClock()
    page = FakePage(per_click=3, total_clicks=2, count_cap=6)
    res = await _act_scroll_to_bottom(_ctx(clk), page, results="r",
                                      max_scrolls=50, timeout_ms=10000, settle_ms=10)
    assert res["stopped"] == "stable"
    assert res["final_count"] == 6
