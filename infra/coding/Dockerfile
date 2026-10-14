FROM node:24.10.0-bookworm-slim
ARG OPENCODE_VERSION=1.19.0
RUN apt-get update && apt-get install -y --no-install-recommends git python3 python3-venv ca-certificates build-essential procps \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g opencode-ai@${OPENCODE_VERSION}
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*
COPY workspace.py git-credential.py cancel.py /opt/swarm/
COPY --chmod=755 entrypoint.sh /opt/swarm/
RUN git config --system credential.helper '!python3 /opt/swarm/git-credential.py' \
    && git config --system credential.useHttpPath true \
    && mkdir -p /workspaces && chown node:node /workspaces
ENV HOME=/home/node
WORKDIR /workspaces
ENV OPENCODE_DISABLE_PROJECT_CONFIG=true
EXPOSE 4096
ENTRYPOINT ["/opt/swarm/entrypoint.sh"]
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
