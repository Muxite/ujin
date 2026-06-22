"""Policy signals: persisted host state → polite-polling recommendations.

:class:`~ujin.adapt.site_store.SiteStore` *remembers* what we last observed about
a host (status, latency, error / 429 counts, ``Crawl-delay``). This module is the
pure, deterministic *interpretation* layer on top of it: given one
:class:`~ujin.adapt.site_store.HostRecord`, :func:`derive_signals` returns a frozen
:class:`PolicySignals` of recommendations — how long to wait, whether to cool down,
whether the host is rate-limiting us, how much to scale concurrency, and a single
0..1 ``health`` score.

It does **no I/O and no network** and never mutates anything, so the same record
always maps to the same signals — trivially unit-testable with a hand-built
``HostRecord``. :class:`SignalAdvisor` is the only stateful piece: a thin, read-only
bridge that pulls a record out of a ``SiteStore`` and hands it to
:func:`derive_signals`.

Everything here is additive and opt-in. Nothing in this module wires itself into
the scrape or poll path; it is the *input layer* that the planned strategy-feedback
and learned-rate-limit units consume.

Derivation rules (all deterministic and documented inline):

* **rate_limited** — ``True`` when ``rate_limit_count > 0`` or ``last_status == 429``.
  When set, ``recommended_interval`` is pushed up and ``concurrency_factor`` down,
  scaling with the number of observed 429s.
* **recommended_interval** — starts at ``base_interval``; rate limiting grows it
  multiplicatively (with a small absolute floor so a zero base still backs off). It
  is never below ``max(record.crawl_delay, robots_crawl_delay)`` when either is set.
* **health** — ``1 / (1 + penalty)`` where ``penalty`` rises with ``error_count``
  and observed 429s. A clean record scores exactly ``1.0``; it falls monotonically
  toward ``0`` as failures accumulate, always within ``0..1``.
* **cooldown_secs / should_cooldown** — ``0`` for a clean record; ``error_count``
  raises the cooldown linearly (capped), and any rate limiting applies a floor.
  ``should_cooldown`` is simply ``cooldown_secs > 0``.
* **concurrency_factor** — ``1.0`` for a clean record; only rate limiting reduces it
  (cooldown, not concurrency, absorbs error pressure), clamped to a sane minimum.

A clean record therefore yields ``health == 1.0``, ``should_cooldown == False``,
``concurrency_factor == 1.0`` and ``recommended_interval == base_interval``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ujin.adapt.site_store import HostRecord, SiteStore

# -- Tunable derivation constants (documented; deterministic) --------------- #
# Health: each error contributes this much penalty, each observed 429 rather more
# (a server actively rate-limiting us is a stronger negative signal than a stray
# transport error).
_ERROR_HEALTH_WEIGHT = 0.5
_RATE_HEALTH_WEIGHT = 1.0

# Cooldown: seconds added per accumulated error, capped; rate limiting applies a
# fixed floor so a 429 always earns at least this much breathing room.
_ERROR_COOLDOWN_STEP = 5.0
_MAX_COOLDOWN = 300.0
_RATE_LIMIT_COOLDOWN = 30.0

# Interval under rate limiting: grow by this fraction per observed 429, capped,
# with a small absolute floor so backing off works even from a zero base.
_RATE_LIMIT_SLOWDOWN = 0.5
_MAX_RATE_LIMIT_MULT = 4.0
_MIN_RATE_LIMIT_INTERVAL = 1.0

# Concurrency never collapses to zero; throttle toward this floor under sustained
# rate limiting.
_MIN_CONCURRENCY_FACTOR = 0.25


@dataclass(frozen=True)
class PolicySignals:
    """Immutable, derived recommendations for one host.

    * ``recommended_interval`` — seconds to wait between requests (>= any known
      crawl delay).
    * ``cooldown_secs`` — suggested pause before retrying when the host is unhealthy.
    * ``should_cooldown`` — convenience flag, ``cooldown_secs > 0``.
    * ``rate_limited`` — whether the host has been (or is) rate-limiting us.
    * ``concurrency_factor`` — multiplier in ``(0, 1]`` to scale in-flight requests.
    * ``health`` — overall health in ``0..1`` (``1.0`` == pristine).
    """

    recommended_interval: float
    cooldown_secs: float
    should_cooldown: bool
    rate_limited: bool
    concurrency_factor: float
    health: float


def derive_signals(
    record: HostRecord,
    *,
    base_interval: float = 0.0,
    robots_crawl_delay: float | None = None,
) -> PolicySignals:
    """Map one persisted ``HostRecord`` to :class:`PolicySignals`.

    Pure and deterministic: no I/O, no network, no mutation. ``base_interval`` is
    the cadence to recommend for a healthy host; ``robots_crawl_delay`` is an
    optional ``Crawl-delay`` (seconds) parsed from robots.txt — see
    :meth:`ujin.robots.RobotsPolicy.crawl_delay`, which returns ``float | None``.
    """
    # A 429 either accumulated as a counter or seen on the last response means the
    # host is rate-limiting us. Count it as at least one event for scaling.
    rate_limited = record.rate_limit_count > 0 or record.last_status == 429
    rl_events = max(record.rate_limit_count, 1 if record.last_status == 429 else 0)

    # -- health: monotonically decreasing in failures, always within 0..1 ----- #
    penalty = (
        _ERROR_HEALTH_WEIGHT * record.error_count
        + _RATE_HEALTH_WEIGHT * rl_events
    )
    health = 1.0 / (1.0 + penalty)

    # -- recommended interval: base, grown under rate limiting, floored by any
    #    known crawl delay --------------------------------------------------- #
    interval = max(0.0, base_interval)
    if rate_limited:
        mult = min(_MAX_RATE_LIMIT_MULT, 1.0 + _RATE_LIMIT_SLOWDOWN * rl_events)
        interval = max(interval * mult, _MIN_RATE_LIMIT_INTERVAL)
    crawl_floor = max(record.crawl_delay, robots_crawl_delay or 0.0)
    if crawl_floor > 0.0:
        interval = max(interval, crawl_floor)

    # -- cooldown: error pressure raises it, rate limiting floors it ---------- #
    cooldown_secs = 0.0
    if record.error_count > 0:
        cooldown_secs = min(_MAX_COOLDOWN, _ERROR_COOLDOWN_STEP * record.error_count)
    if rate_limited:
        cooldown_secs = max(cooldown_secs, _RATE_LIMIT_COOLDOWN)
    should_cooldown = cooldown_secs > 0.0

    # -- concurrency: only rate limiting throttles it ------------------------- #
    concurrency_factor = 1.0
    if rate_limited:
        concurrency_factor = max(
            _MIN_CONCURRENCY_FACTOR, 1.0 / (1.0 + rl_events)
        )

    return PolicySignals(
        recommended_interval=interval,
        cooldown_secs=cooldown_secs,
        should_cooldown=should_cooldown,
        rate_limited=rate_limited,
        concurrency_factor=concurrency_factor,
        health=health,
    )


class SignalAdvisor:
    """Read-only bridge from a :class:`SiteStore` to :class:`PolicySignals`.

    ``for_host(host)`` reads the persisted record via ``store.get(host)`` (which
    returns a zero-valued record for unknown hosts) and runs it through
    :func:`derive_signals`. It never writes to the store and is never wired into
    the default scrape/poll path — it is the opt-in input the strategy-feedback and
    learned-rate-limit units consume.
    """

    def __init__(
        self,
        store: SiteStore,
        *,
        base_interval: float = 0.0,
        robots_crawl_delay: float | None = None,
    ):
        self._store = store
        self._base_interval = base_interval
        self._robots_crawl_delay = robots_crawl_delay

    def for_host(
        self,
        host: str,
        *,
        base_interval: float | None = None,
        robots_crawl_delay: float | None = None,
    ) -> PolicySignals:
        """Return derived signals for ``host`` without mutating the store.

        ``base_interval`` / ``robots_crawl_delay`` default to the values supplied at
        construction, but either can be overridden per call.
        """
        record = self._store.get(host)
        return derive_signals(
            record,
            base_interval=(
                self._base_interval if base_interval is None else base_interval
            ),
            robots_crawl_delay=(
                self._robots_crawl_delay
                if robots_crawl_delay is None
                else robots_crawl_delay
            ),
        )
