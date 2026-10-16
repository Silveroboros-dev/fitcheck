# Public, credential-free fixture UI. The launcher migrates its disposable
# local database and starts web plus worker; it performs no provider setup.
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FITCHECK_UI_MODE=fixture \
    HOME=/tmp \
    PORT=8080

WORKDIR /app

COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY el ./el
COPY migrations ./migrations

RUN python -m pip install --no-cache-dir . \
    && adduser --disabled-password --gecos "" --no-create-home appuser

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "exec fitcheck-demo --db /tmp/fitcheck-demo.sqlite --host 0.0.0.0 --port \"$PORT\""]
