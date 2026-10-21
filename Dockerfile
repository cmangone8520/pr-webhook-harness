FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY prharness ./prharness
RUN pip install --no-cache-dir .

RUN useradd --create-home appuser
USER appuser

EXPOSE 8080
CMD ["uvicorn", "prharness.main:app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
