# MASEO Web App

A web application for [MASEO-Atomic](../maseo_atomic), running across four Docker containers: queue a job with your competency questions, watch the pipeline run live, and download the ontology and every step's files when it ends.

The web application is live at **[https://maseo.linkeddata.es/](https://maseo.linkeddata.es/)**, reachable from the UPM network or through the UPM VPN.

## Getting started

Download [Docker Desktop](https://www.docker.com/products/docker-desktop) for Mac or Windows. [Docker Compose](https://docs.docker.com/compose) will be automatically installed. On Linux, install [Docker Engine](https://docs.docker.com/engine/install/) with the Compose plugin. The models run in the `ollama` container, so give Docker enough memory for them (about 20 GB for a 27B model).

This solution uses Python (FastAPI) for the web app and the MASEO service, [n8n](https://n8n.io/) for the workflows, and [Ollama](https://ollama.com/) for the models.

Get the code. The `maseo` image is built from `../maseo_atomic`, so this folder must stay next to it:

```shell
git clone https://github.com/OEG-Clark/masoe.git
cd masoe/src/maseo_atomic_n8n
```

Create the `.env` file from the example, open it, and set `N8N_ENCRYPTION_KEY` to a long random string (for example the output of `openssl rand -hex 24`). n8n encrypts its data with this key, so keep it once it is set:

```shell
cp .env.example .env
```

Run in this directory to build and run the app:

```shell
docker compose up -d --build
```

Pull at least one model for the form:

```shell
docker compose exec ollama ollama pull qwen3.6:27b
```

The first time, import and publish the two n8n workflows:

```shell
docker compose cp maseo_worker_workflow.json n8n:/tmp/maseo_worker_workflow.json
docker compose cp maseo_web_workflow.json n8n:/tmp/maseo_web_workflow.json
docker compose exec n8n n8n import:workflow --input=/tmp/maseo_worker_workflow.json
docker compose exec n8n n8n import:workflow --input=/tmp/maseo_web_workflow.json
docker compose exec n8n n8n publish:workflow --id=MaseoAtomicWorker
docker compose exec n8n n8n publish:workflow --id=MaseoAtomicWebApi
docker compose restart n8n
```

The web app will be running at [http://localhost:8080](http://localhost:8080) (on a server, `http://<server IP>:8080`).

## Checking that it runs

All four containers should be `Up`:

```shell
docker compose ps
```

[http://localhost:8080/healthz](http://localhost:8080/healthz) answers `{"ok":true}` when the whole chain works, from the web app through n8n to the MASEO service (give n8n a few seconds after a start or restart). The header of the page shows `Ollama · N models` and the state of the queue.

The logs show what each container is doing:

```shell
docker compose logs -f maseo webapp
```

After a start, they look like this:

```
maseo-1   | maseo atomic service on http://0.0.0.0:8030
maseo-1   |   maseo_atomic: /app/maseo_atomic
maseo-1   |   work dir: /data/outputs
maseo-1   |   ollama: http://ollama:11434
maseo-1   |   n8n worker: http://n8n:5678/webhook/maseo-worker  (0 job(s) queued)
maseo-1   | Processing request of type ListToolsRequest      (five times, one per MCP tool server)
maseo-1   |   MCP tools ready: cq_coverage, hermit_consistency, oops_scan, syntax_check, themis_test
webapp-1  | MASEO web app on http://0.0.0.0:8080/  ->  n8n http://n8n:5678/webhook/maseo-api  (password off)
```

To try it end to end, open **＋ New job**, type two short competency questions, switch the optional agents off and press **Add to queue**. On a machine without a GPU this small job takes a few minutes to half an hour. The job page shows every step as it runs and ends with `ALL CHECKS PASSED` or the checks that failed, and **Download all (zip)** gives its files.

## Architecture

`docker-compose.yml` runs four containers in one Compose project (`maseo`):

| Container | Image | Responsibility | Reachable from |
|-----------|-------|----------------|----------------|
| `webapp` | `maseo/webapp` (built from `webapp/Dockerfile`) | The web app (FastAPI): makes every page and sends every action to n8n | Port **8080** of the server |
| `n8n` | `n8nio/n8n:2.38.2` | Runs the two workflows below | Inside the Compose network only |
| `maseo` | `maseo/atomic-service` (built from `Dockerfile`) | `maseo_service.py`: the job queue, one endpoint per graph node, running `../maseo_atomic`'s agents and its five MCP tool servers (Java included) | Inside the Compose network only |
| `ollama` | `ollama/ollama` | Serves the models | The server itself (`127.0.0.1:11434`) |

Here are the n8n workflows:

| Workflow | File | Responsibility |
|----------|------|----------------|
| `MASEO web app + API` | `maseo_web_workflow.json` | The API webhooks the web app calls (`/webhook/maseo-api/*`: info, jobs, job, submit, cancel, delete, download) |
| `MASEO worker (agent_graph)` | `maseo_worker_workflow.json` | MASEO-Atomic's graph drawn in n8n: claims the next job and runs its graph nodes one by one, one execution per job |

The jobs are kept in `outputs/`, n8n's data in the volume `maseo_n8n_data` and the models in the volume `maseo_ollama`.

## Maintenance

| To | Run |
|----|-----|
| stop and start the app | `docker compose stop`, `docker compose up -d` |
| apply a code change | `docker compose up -d --build` (and import the workflows again if a workflow file changed) |
| add or remove a model | `docker compose exec ollama ollama pull <model>`, `docker compose exec ollama ollama rm <model>` |
| update n8n or Ollama | `docker compose pull n8n ollama`, then `docker compose up -d` |
| back up the app | copy `.env` and the `outputs/` folder |

Rebuilding or restarting the `maseo` container stops the running job, so do it when the queue is idle. Do not run `docker compose down -v`: it deletes the volumes with the models and n8n's data.

## Notes

The other keys in `.env` are optional: `TZ` (time zone of the run ids), `MASEO_WEB_BIND` (the address the web app listens on, `0.0.0.0` by default), `MASEO_WEB_PASSWORD` (a password for the web app) and `N8N_VERSION`. Settings that are not on the form, such as the prompts, `tool_retries` and `loop_rounds`, come from `../maseo_atomic/config.yaml`. For a thinking model, `reasoning_models.yaml` says what its Reasoning switch offers.

Jobs run one at a time. On a server without a GPU, a job takes from minutes to many hours, depending on the number of questions, the agents and the reasoning. Every container has `restart: always`, so the app comes back by itself after a reboot as long as Docker starts with the system (`sudo systemctl enable docker` on Linux).

The app uses the ports 8080 (the web app) and 11434 (Ollama, local to the machine). If Ollama is already installed and running on the machine, stop it first (`sudo systemctl disable --now ollama` on Linux), or `docker compose up` cannot start the `ollama` container. If the page shows a red banner saying a workflow is not published, or a job stays first in the queue without running, import the workflows again. If the model list is empty, check `docker compose ps ollama`.
