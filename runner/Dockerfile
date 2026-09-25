FROM node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS node
FROM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3
COPY --from=node /usr/local/ /usr/local/

ARG RUNNER_VERSION=2.337.0
ARG RUNNER_SHA256=70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613
ARG CODEX_VERSION=0.156.1
ARG GH_VERSION=2.101.0
ARG GH_SHA256=9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git python3 python3-venv sudo procps \
    libicu74 libssl3t64 libkrb5-3 zlib1g liblttng-ust1t64 libunwind8 \
    iproute2 util-linux tini && rm -rf /var/lib/apt/lists/*
RUN curl --fail --silent --show-error --location --retry 3 \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
      -o /tmp/gh.tar.gz && \
    echo "${GH_SHA256}  /tmp/gh.tar.gz" | sha256sum --check && \
    tar -xzf /tmp/gh.tar.gz -C /tmp && \
    install -m 755 "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh && \
    rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_amd64"
RUN npm install -g @openai/codex@${CODEX_VERSION} && npm cache clean --force
RUN mkdir -p /opt/actions-runner && \
    curl --fail --location --retry 3 \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
      -o /tmp/runner.tar.gz && \
    echo "${RUNNER_SHA256}  /tmp/runner.tar.gz" | sha256sum --check && \
    tar -xzf /tmp/runner.tar.gz -C /opt/actions-runner && rm /tmp/runner.tar.gz
RUN useradd --uid 1101 --create-home --shell /bin/bash nexkit-codex && \
    mkdir -p /var/lib/nexkit/codex /var/log/nexkit /etc/nexkit && \
    chown nexkit-codex:nexkit-codex /var/lib/nexkit/codex && \
    chmod 700 /var/lib/nexkit/codex /var/log/nexkit
COPY job_hook.py started.sh completed.sh entrypoint.py /opt/nexkit/
COPY parent-git.sh /opt/nexkit/parent-bin/git
RUN chmod 755 /opt/nexkit/started.sh /opt/nexkit/completed.sh && \
    chmod 755 /opt/nexkit/parent-bin/git && \
    chmod -R go-w /opt/nexkit /opt/actions-runner
ENV RUNNER_ALLOW_RUNASROOT=1 \
    ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/nexkit/started.sh \
    ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/opt/nexkit/completed.sh
WORKDIR /opt/actions-runner
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/opt/nexkit/entrypoint.py"]
