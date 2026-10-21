FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY prharness ./prharness
RUN pip install --no-cache-dir .

# Semgrep lives in its own venv: its dependency pins conflict with FastAPI's.
RUN python -m venv /opt/semgrep && /opt/semgrep/bin/pip install --no-cache-dir "semgrep==1.182.0"
ENV SEMGREP_BIN=/opt/semgrep/bin/semgrep

RUN useradd --create-home appuser
USER appuser

EXPOSE 8080
CMD ["uvicorn", "prharness.main:app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
