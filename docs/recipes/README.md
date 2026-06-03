# ujin recipe cookbook

Task-oriented walkthroughs. Each is a complete, copy-pasteable job or scrape call.

| Recipe | What it shows |
|--------|---------------|
| [load-more-academic-profile.md](load-more-academic-profile.md) | Harvest **every** publication behind a "Load more" button (the Sidney Fels case). |
| [feed-an-llm-with-chunking.md](feed-an-llm-with-chunking.md) | Hand a large result set to an LLM in digestible chunks (`chunk` transform + paginated `/scrape`). |
| [paginated-api-harvest.md](paginated-api-harvest.md) | Pull a big link-set N-at-a-time with `page_size` + `cursor`. |
| [change-watching-browser.md](change-watching-browser.md) | Watch a JS-rendered region for change and only emit what's new. |
| [login-session-scraping.md](login-session-scraping.md) | Log in with `fill`/`press`/`click`, then scrape gated pages. |
| [custom-actions-plugin.md](custom-actions-plugin.md) | Add a custom recipe step with `@register.action`. |

Background: [../BROWSER.md](../BROWSER.md) (browser layer), [../JOBS.md](../JOBS.md)
(jobs/transforms/sinks), [../PLUGINS.md](../PLUGINS.md) (plugin authoring).
