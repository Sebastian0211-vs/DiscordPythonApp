# Image the untrusted scripts run in. Build on the VPS with:
#   docker build -t remotepy-sandbox:latest sandbox/
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=module://mpl_autosave \
    PYTHONPATH=/opt/sandbox_lib \
    HOME=/tmp \
    MPLCONFIGDIR=/tmp/.mpl

COPY mpl_autosave.py sitecustomize.py /opt/sandbox_lib/
COPY requirements.txt /opt/requirements.txt
RUN pip install -r /opt/requirements.txt \
    && MPLCONFIGDIR=/opt/mplcache python -c "import matplotlib.pyplot" \
    && chmod -R a+rX /opt/mplcache \
    && useradd --uid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin sandbox

COPY runner.py /opt/runner.py

USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "/opt/runner.py"]
