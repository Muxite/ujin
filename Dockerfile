# eujin poll-engine service (REST + WebSocket).
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY eujin ./eujin
# install with the service + web extras (REST/WS API + HTTP/RSS/API roles)
RUN pip install --no-cache-dir ".[service,web]"

EXPOSE 8900

# Optionally mount a targets file and pass it as the CMD arg, e.g.
#   docker run -v $PWD/targets.yaml:/app/targets.yaml eujin api /app/targets.yaml
ENTRYPOINT ["eujin"]
CMD ["api", "--host", "0.0.0.0", "--port", "8900"]
