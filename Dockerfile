# Public, credential-free fixture UI. This is not the private reference service
# image and intentionally performs no migration or external-provider setup.
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FITCHECK_UI_MODE=fixture \
    FITCHECK_UI_DB_URL=sqlite:////tmp/fitcheck_product.db \
    HOME=/tmp \
    PORT=8080

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY el ./el
COPY tests/fixtures ./tests/fixtures

RUN python -m pip install --no-cache-dir . \
    && adduser --disabled-password --gecos "" --no-create-home appuser

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "exec python -m uvicorn el.product.app:app --host 0.0.0.0 --port \"$PORT\""]
