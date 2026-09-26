FROM python:3.11-slim

WORKDIR /app

# curl es necesario para el healthcheck de Docker Compose (GET /salud)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Directorio donde se monta el volumen persistente del archivo SQLite.
RUN mkdir -p /app/data

EXPOSE 8004

CMD ["python", "app.py"]
