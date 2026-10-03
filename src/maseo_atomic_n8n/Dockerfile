# The MASEO atomic service for the n8n web app: the job queue and
# maseo_atomic's own code
# (agent_graph.py, agent.py, config.yaml, models.py, the five MCP servers,
# HermiT.jar, themis.jar) behind maseo_service.py, one endpoint per graph node.
# Build context is src/ (docker-compose.yml: `context: ..`), because the image
# takes the pipeline code from src/maseo_atomic; Dockerfile.dockerignore keeps
# the context to maseo_atomic and this service.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends default-jre-headless ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY maseo_atomic/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /app
COPY maseo_atomic /app/maseo_atomic
COPY maseo_atomic_n8n/maseo_service.py /app/maseo_service.py
COPY maseo_atomic_n8n/reasoning_models.yaml /app/reasoning_models.yaml
ENV MASEO_ATOMIC=/app/maseo_atomic \
    MASEO_WORK_DIR=/data/outputs \
    MASEO_PORT=8030 \
    OLLAMA_HOST=http://ollama:11434 \
    MASEO_WORKER_URL=http://n8n:5678/webhook/maseo-worker \
    PYTHONUNBUFFERED=1
EXPOSE 8030
CMD ["python", "/app/maseo_service.py"]
