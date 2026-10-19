FROM node:22-slim

RUN useradd -m -u 10001 runner

RUN apt-get update && apt-get install -y --no-install-recommends git python3 ca-certificates && rm -rf /var/lib/apt/lists/*

ENV OPENER=/workspaces
RUN mkdir -p /workspaces

RUN npm install -g opencode-ai@1.19.3

WORKDIR /app
COPY --chmod=0555 runner.py /app/runner.py
COPY --chmod=0555 entrypoint.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh && chown -R runner:runner /workspaces

USER runner
WORKDIR /workspaces

ENTRYPOINT ["/entrypoint.sh"]
