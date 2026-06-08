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
