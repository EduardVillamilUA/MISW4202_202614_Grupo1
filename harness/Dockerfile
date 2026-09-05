FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY harness.py ejecutar_experimento.py analizar_resultados.py .
# No CMD por defecto: se invoca con "docker compose run --rm harness python ejecutar_experimento.py ..."
