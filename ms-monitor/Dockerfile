FROM python:3.11-slim

WORKDIR /app

# curl es necesario para el healthcheck de Docker Compose (GET /status)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Salida sin buffer, para que los logs aparezcan en tiempo real en docker-compose logs
ENV PYTHONUNBUFFERED=1

EXPOSE 6000

CMD ["python", "app.py"]
