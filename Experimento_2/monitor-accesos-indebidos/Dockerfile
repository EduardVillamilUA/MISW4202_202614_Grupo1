FROM python:3.11-slim

WORKDIR /app

# curl es necesario para el healthcheck de Docker Compose (GET /salud)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8002

CMD ["python", "app.py"]
