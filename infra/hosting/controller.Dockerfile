FROM docker:28-cli AS dockercli
FROM python:3.12-slim
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /root/swarm/app
COPY . /opt/swarm-source
RUN pip install --no-cache-dir /opt/swarm-source
STOPSIGNAL SIGINT
ENTRYPOINT ["agent-company", "start"]
