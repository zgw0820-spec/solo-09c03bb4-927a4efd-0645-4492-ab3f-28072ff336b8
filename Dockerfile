# Runtime image for the values-cleanup + rollout-planning service.
# Helm 3 is installed because the service shells out to `helm template`.
FROM python:3.11-slim

ARG HELM_VERSION=v3.14.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates tar \
    && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
        | tar -xzf - -C /usr/local/bin --strip-components=1 linux-amd64/helm \
    && chmod +x /usr/local/bin/helm \
    && apt-get purge -y --auto-remove curl tar \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY app /srv/app
# The "mounted directory" with the two charts and the shared overrides.
COPY mounted-charts /workspace/mounted-charts
COPY overrides /workspace/overrides

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
