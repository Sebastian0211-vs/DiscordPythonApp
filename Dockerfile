# Image for the bot itself (not the sandbox). It talks to the host Docker daemon
# through the mounted socket to start sandbox containers.
FROM python:3.14-slim

# Docker CLI only (no daemon), copied from the official image.
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

CMD ["remotepy-bot"]
