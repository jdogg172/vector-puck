FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 HOME=/tmp
WORKDIR /app
COPY pyproject.toml /app/
COPY requirements.lock /app/
COPY src /app/src
RUN python -m pip install --no-cache-dir -c requirements.lock .
USER 1000:1000
ENTRYPOINT ["vector-puck"]
CMD ["--config", "/config/local.json", "--state", "/state"]
