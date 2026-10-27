# Imagen única del simulador: el mismo binario corre el generador, la API y la CLI `sim`.
ARG IMAGEN_PYTHON=python:3.11-slim
FROM ${IMAGEN_PYTHON}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SIM_RAIZ=/app

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# El secreto opcional `ca_proxy` sirve solo si construyes detrás de un proxy que re-firma TLS.
RUN --mount=type=secret,id=ca_proxy,required=false \
    if [ -f /run/secrets/ca_proxy ]; then export PIP_CERT=/run/secrets/ca_proxy; fi; \
    pip install . && rm -rf /root/.cache
COPY datos ./datos
COPY config ./config

ENTRYPOINT ["sim"]
CMD ["generador"]
