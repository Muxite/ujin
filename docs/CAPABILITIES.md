# ujin capabilities — extending the building blocks

A [workflow](WORKFLOWS.md) is assembled from **capabilities**: the
`source` / `transform` / `sink` (and `scorer` / `action`) kinds in the registry.
ujin is meant to grow — when a workflow needs something the built-ins don't cover,
you add a capability. There are two paths, depending on who owns the code.

## Path 1 — drop a plugin (operator, no rebuild)

Mount a Python file into `/plugins`; its `@register.*` factories become
`plugin:<name>` kinds. Best for deployment-specific or one-off capabilities. Full
guide: [PLUGINS.md](PLUGINS.md).

## Path 2 — edit ujin in-tree (sibling projects)

Projects in active development are expected to **edit ujin directly** to add
first-class, reusable capabilities (no `plugin:` prefix). This is the path when a
capability is general enough that workflows everywhere should be able to name it.

### Install editable

```bash
pip install -e '.[jobs]'        # ujin importable + your edits live immediately
```

A sibling repo can depend on ujin as an editable checkout and commit capability
changes upstream as it discovers what its workflows need.

### Add a built-in kind

Built-ins are wired in `_install_builtins()` in
[`ujin/registry.py`](../ujin/registry.py). Add a factory and register it:

```python
# ujin/registry.py, inside _install_builtins(reg)
def _src_myfeed(cfg):
    from ujin.poll.myfeed import MyFeedPollable      # lazy import keeps core light
    return MyFeedPollable(cfg["url"])

reg.register_builtin("source", "myfeed", _src_myfeed)   # usable as kind: myfeed
```

Transforms and sinks follow their existing maps —
`BUILTIN_TRANSFORMS` in [`ujin/jobs/transforms.py`](../ujin/jobs/transforms.py)
and `BUILTIN_SINKS` in [`ujin/jobs/sinks.py`](../ujin/jobs/sinks.py) — so adding a
class there is usually enough; `_install_builtins` registers every entry.

### The contracts

| Capability | Object the factory returns |
|---|---|
| **source** | `key: str` + `async poll(prev: PollResult \| None) -> PollResult`. Set `fingerprint` for change detection; `payload` is whatever downstream wants (and what `GET /jobs/{id}/content` hands out). |
| **transform** | `async apply(event: dict) -> dict \| list[dict] \| None` (`None` drops; a list fans out). |
| **sink** | `async emit(event: dict) -> None`. |
| **action** | browser recipe step: `async handler(page, **params)`. |

A factory taking a **second parameter** receives a `BuildContext`
(`ctx.scrape_service`, `ctx.hub`, `ctx.store`) — that's how `scrape`/`browser`
sources and the `ws`/`sqlite` sinks reach shared services
([registry.py](../ujin/registry.py) `build_*` / `BuildContext`).

### Keep it landable

- **Lazy-import** heavy deps inside the factory so the pure-python core stays
  dependency-light (every built-in source does this).
- Add a test mirroring `tests/test_jobs_app.py` / `tests/test_workflows_dir.py`.
- New kinds show up automatically in `GET /kinds` and are usable from any
  workflow file or `POST /jobs` body.
