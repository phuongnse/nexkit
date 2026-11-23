# The engine as a container: `nexkit-engine serve` on port 8080, for a host that runs the
# engine beside its own processes (docs/engine.md#the-parts). The release workflow builds it
# from the release's commit; its Python is the one `nexkit/__init__.py` pins.
ARG PYTHON=3.12
FROM python:${PYTHON}-slim

LABEL org.opencontainers.image.source="https://github.com/phuongnse/nexkit" \
      org.opencontainers.image.description="NexKit's engine, answering its calls over HTTP" \
      org.opencontainers.image.licenses="MIT"

COPY pyproject.toml README.md LICENSE /src/
COPY nexkit /src/nexkit
RUN pip install --no-cache-dir --root-user-action=ignore /src && rm -rf /src

USER nobody
EXPOSE 8080
ENTRYPOINT ["nexkit-engine", "serve", "--host", "0.0.0.0", "--port", "8080"]
