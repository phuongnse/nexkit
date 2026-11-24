# The engine as a container: `nexkit-engine serve` on port 8080, for a host that runs the
# engine beside its own processes (docs/engine.md#the-parts). release.yml builds it from the
# release's commit, with the base image that nexkit/__init__.py pins in PYTHON_IMAGE. The
# image runs the package from its source, as bin/nexkit-engine does, so its build installs
# nothing.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/phuongnse/nexkit" \
      org.opencontainers.image.description="NexKit's engine, answering its calls over HTTP" \
      org.opencontainers.image.licenses="MIT"

COPY LICENSE /opt/nexkit/
COPY bin/nexkit-engine /opt/nexkit/bin/
COPY nexkit /opt/nexkit/nexkit

ENV PYTHONDONTWRITEBYTECODE=1
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["python", "/opt/nexkit/bin/nexkit-engine", "serve", "--host", "0.0.0.0", "--port", "8080"]
