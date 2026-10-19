FROM python:3.12-slim

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir . "psycopg[binary]"

RUN useradd -m -u 10001 aip
USER aip

EXPOSE 7700
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7700"]
