# ujin plugins — upload-code extension

When a built-in source/transform/sink doesn't cover your case, drop a Python file
into the plugin directory and ujin will load it and expose what it registers as
new `plugin:<name>` kinds usable in any job.

> Want a first-class, reusable kind instead of a `plugin:*` drop-in (e.g. a
> sibling project growing ujin in-tree)? See [CAPABILITIES.md](CAPABILITIES.md).

> **Trust model:** plugins run **in-process with no sandbox** — arbitrary code
> execution by design. Only mount code you trust. If the control plane is
> network-exposed, gate it with `UJIN_API_KEY` (see [JOBS.md](JOBS.md#auth-optional)).

## Where plugins live

`UJIN_PLUGINS_DIR` (default `/plugins`). In the container it's a mounted volume:

```yaml
# docker-compose.yml (ujin-jobs)
volumes:
  - ./plugins:/plugins
```

ujin imports every top-level `*.py` file and every package directory (one with
`__init__.py`) under it. A file that fails to import is logged and skipped — it
never aborts startup or other plugins. Names beginning with `.` or `_` are ignored.

## Writing a plugin

Register factories with the global `register`. A factory takes the job's `config`
dict and returns the object:

```python
# /plugins/my.py
from ujin import register
from ujin.poll.base import PollResult

@register.source("my_api")          # usable as  "kind": "plugin:my_api"
def make_source(cfg):
    class _Src:
        key = "my_api"
        async def poll(self, prev: PollResult | None) -> PollResult:
            # ... fetch, compute a fingerprint for change detection ...
            return PollResult(ok=True, changed=True, fingerprint="...", payload=...)
    return _Src()

@register.transform("redact")       # usable as  "kind": "plugin:redact"
def make_transform(cfg):
    class _T:
        async def apply(self, event: dict) -> dict | None:
            event["payload"] = {"redacted": True}
            return event            # return None to drop the event
    return _T()

@register.sink("my_db")             # usable as  "kind": "plugin:my_db"
def make_sink(cfg):
    class _S:
        async def emit(self, event: dict) -> None:
            ...                     # write event somewhere
    return _S()
```

### Contracts
- **action** → `@register.action` adds a custom browser recipe step; the factory
  returns an `async handler(page, **params)` driven against the live page. See
  [recipes/custom-actions-plugin.md](recipes/custom-actions-plugin.md).
- **source** → an object with `key: str` and `async poll(prev) -> PollResult`.
  Set `PollResult.fingerprint` so change detection works; `payload` is whatever
  you want downstream.
- **transform** → `async apply(event: dict) -> dict | list[dict] | None` (None drops
  the event; a list fans out into several downstream events, like `chunk`).
- **sink** → `async emit(event: dict) -> None`.

### Needing ambient services
A factory may take a second parameter, a `BuildContext`, to reach the shared
scrape backend, the broadcast hub, or the jobstore:

```python
@register.sink("broadcast")
def make(cfg, ctx):                 # ctx: ujin.registry.BuildContext
    hub = ctx.hub                   # also: ctx.store, ctx.scrape_service
    class _S:
        async def emit(self, event): await hub.broadcast_event(event)
    return _S()
```

## Loading & reloading

- On startup the jobs app loads all plugins, then reloads persisted jobs (so jobs
  referencing `plugin:*` kinds resolve).
- `POST /plugins/reload` clears plugin-contributed kinds and re-imports the
  directory (edited files re-execute). Response: `{loaded: [...], failed: [...]}`.
  Already-running jobs keep their built pipeline objects; only newly created or
  restarted jobs pick up the reloaded factories.
- `GET /health` and `GET /metrics` report `{loaded, failed}`; `GET /kinds` lists
  all available kinds.

## Example

`examples/plugins/hello_sink.py` registers a `hello` sink and a `ticker` source.
Mount it, `POST /plugins/reload`, then create a job with
`{"source":{"kind":"plugin:ticker"}, "sinks":[{"kind":"plugin:hello"}]}`.

## `scorer` plugins also work on the scrape app (`:8901`)

Everything above is written from the jobs control plane's point of view, but
plugin loading isn't jobs-specific — `ujin.scrape.app.create_scrape_app` loads
the same `UJIN_PLUGINS_DIR` at startup and exposes `{loaded, failed}` on its
own `GET /health`, plus the same `POST /plugins/reload`. The one capability
the scrape app actually *consumes* from a plugin today is `scorer`:

```python
# /plugins/my_scorer.py
from ujin import register

@register.scorer("my_scorer")            # usable via scorer_kind="my_scorer"
def make_scorer(cfg: dict):
    class _Scorer:
        def score_links(self, links, *, base_url):
            ...                           # annotate link._tier / ._breaking_score
        def poll_hint(self, links, *, base_url, prior_unchanged):
            return None                   # or a float seconds hint
    return _Scorer()
```

Point `ScrapeConfig.scorer_kind` (or `UJIN_SCORER_KIND`) at the registered
name and the app resolves it — no ujin-tree edit, no `plugin:` prefix required
(bare names resolve the same as `plugin:`-prefixed ones). An unresolvable
`scorer_kind` (typo, or the plugin that would supply it failed to load) is
logged and reported on `GET /health` as `scorer_error`; the app still starts
and scores with the neutral `NullScorer` rather than refusing to serve. See
[API.md](API.md#scrape-service-8901) for the full config knob and
`ujin.trends.wiring.wire_breaking_scorer` for how the built-in `BreakingScorer`
(`UJIN_BREAKING_SCORER=1`) is wired the same way, just without a plugin.
