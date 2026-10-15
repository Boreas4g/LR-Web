FROM python:3.12-slim

WORKDIR /app

# Keep dependency installation independent from application source changes.
# A station can then rebuild a control-api code fix from its cached dependency
# layer even when PyPI is temporarily unreachable.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip "setuptools>=68" && \
    python - <<'PY'
import subprocess
import tomllib

with open("pyproject.toml", "rb") as handle:
    dependencies = tomllib.load(handle)["project"]["dependencies"]
subprocess.check_call(["pip", "install", "--no-cache-dir", *dependencies])
PY

COPY app ./app
# Dependencies above are already present. Disabling build isolation here keeps
# this small source-only layer buildable without another index request.
RUN pip install --no-deps --no-build-isolation .

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
