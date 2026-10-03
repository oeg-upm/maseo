# The MASEO web app (FastAPI): makes the pages (templates/, static/) and
# gets the data from the n8n workflow "MASEO web app + API".
# Build context is src/maseo_atomic_n8n (docker-compose.yml).
FROM python:3.12-slim
WORKDIR /app
COPY webapp/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY webapp /app/webapp
ENV MASEO_N8N_URL=http://n8n:5678 \
    PORT=8080 \
    PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "/app/webapp/app.py"]
