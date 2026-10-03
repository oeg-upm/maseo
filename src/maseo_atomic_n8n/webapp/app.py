#!/usr/bin/env python3
"""The MASEO web app: a FastAPI process that makes every page itself and
uses n8n as its backend. The page is HTML and CSS (templates/index.html,
static/style.css); forms and links do the actions. static/live.js only
keeps a page live: it asks for the same page again every few seconds and
swaps in what changed, and it makes the running times tick. Without
JavaScript the page reloads itself instead.

  GET  /                      the queue and one job (?job=<id>), live
  GET  /new                   the New job form
  POST /submit                queue a job           -> n8n POST /webhook/maseo-api/submit
  POST /jobs/<id>/cancel      cancel / stop a job   -> n8n POST /webhook/maseo-api/cancel
  POST /jobs/<id>/delete      delete a finished job -> n8n POST /webhook/maseo-api/delete
  GET  /download/<id>[/<file>] the zip, or one file -> n8n GET  /webhook/maseo-api/download
  GET  /theme?to=dark|light   dark mode on / off (kept in a cookie)
  GET  /healthz               {"ok": true} when n8n answers

The page itself reads n8n's GET /webhook/maseo-api/{info,jobs,job}. The
browser never talks to n8n, so n8n needs no open port. With a password
set, the browser asks for it before anything is shown (any user name).

  pip install -r requirements.txt
  python3 app.py [--port 8080] [--n8n http://localhost:5678] [--password SECRET]

Environment instead of options: MASEO_N8N_URL, MASEO_N8N_API_PATH
(/webhook/maseo-api), MASEO_WEB_PASSWORD, PORT, MASEO_WEB_POLL (seconds
between updates while a job runs, 3), MASEO_WEB_REFRESH (the same without
JavaScript, 4).
"""
import argparse
import asyncio
import json
import os
import re
import secrets
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

import views

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = {
    "n8n": (os.environ.get("MASEO_N8N_URL") or "http://localhost:5678").rstrip("/"),
    "api_path": "/" + (os.environ.get("MASEO_N8N_API_PATH") or "/webhook/maseo-api").strip("/"),
    "password": os.environ.get("MASEO_WEB_PASSWORD") or "",
    # seconds between updates of the queue page: static/live.js asks for the
    # page again (poll); without JavaScript the page reloads itself (refresh)
    "poll": max(1, int(os.environ.get("MASEO_WEB_POLL") or 3)),      # a job is running or waiting
    "poll_idle": 15,                                                  # nothing running
    "refresh": max(2, int(os.environ.get("MASEO_WEB_REFRESH") or 4)),
    "refresh_idle": 20,
}
MAX_UPLOAD = 5 * 1024 * 1024
UPLOAD_TYPES = (".json", ".txt", ".csv")
AGENT_NAMES = [a for a, _ in views.AGENTS]
INFO_TTL = 30          # seconds the model list is kept (it asks Ollama)
FLASH = "maseo_flash"
THEME = "maseo_theme"
TEMPLATES = Jinja2Templates(directory=os.path.join(HERE, "templates"))
CSS = os.path.join(HERE, "static", "style.css")
JS = os.path.join(HERE, "static", "live.js")


class Backend(Exception):
    """n8n (or the service behind it) cannot answer: shown as a banner."""


@asynccontextmanager
async def lifespan(app):
    # generous read timeout: a zip of a big job takes n8n a while to build
    app.state.client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=600.0))
    app.state.info = (0.0, None)
    yield
    await app.state.client.aclose()


app = FastAPI(title="MASEO web app", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
basic = HTTPBasic(auto_error=False)


def login(credentials: HTTPBasicCredentials = Depends(basic)):
    """With a password set: HTTP Basic, any user name, that password."""
    password = CONFIG["password"]
    if not password:
        return
    if credentials is None or not secrets.compare_digest(credentials.password.encode(), password.encode()):
        raise HTTPException(401, "MASEO web app: wrong or missing password",
                            headers={"WWW-Authenticate": 'Basic realm="MASEO web app"'})


# ------------------------------------------------------------------ n8n ---
def api_url(route):
    return f"{CONFIG['n8n']}{CONFIG['api_path']}/{route}"


async def n8n(request, route, method="GET", params=None, body=None):
    """One call to the n8n workflow "MASEO web app + API"; its JSON."""
    try:
        r = await request.app.state.client.request(method, api_url(route), params=params, json=body, timeout=60)
    except httpx.HTTPError as exc:
        raise Backend(f"The web app cannot reach n8n at {CONFIG['n8n']} ({exc.__class__.__name__}). "
                      "Is n8n running?") from None
    if r.status_code == 404 and "not registered" in r.text.lower():
        raise Backend('The n8n workflow "MASEO web app + API" is not published '
                      f"(webhook maseo-api/{route} is not registered).")
    try:
        data = r.json()
    except ValueError:
        raise Backend(f"n8n answered {r.status_code} to maseo-api/{route}: {r.text[:200]}") from None
    if r.status_code >= 500 and isinstance(data, dict) and not data.get("error"):
        raise Backend(f"n8n could not answer maseo-api/{route} ({data.get('message') or r.status_code}). "
                      "Is the maseo service running?")
    return data if isinstance(data, dict) else {"error": f"unexpected answer from maseo-api/{route}"}


async def get_info(request):
    """Models, reasoning, defaults (kept INFO_TTL seconds)."""
    t, info = request.app.state.info
    if info is not None and time.time() - t < INFO_TTL:
        return info
    info = await n8n(request, "info")
    if info.get("error"):
        raise Backend(f"The maseo service: {info['error']}")
    request.app.state.info = (time.time(), info)
    return info


def clean_error(text):
    return re.sub(r"^\w+(Error|Exception): ", "", str(text or "")).strip()


# ------------------------------------------------------ page state, links --
def base_path(request):
    return (request.scope.get("root_path") or "").rstrip("/") + "/"


def page_links(request, state, sel=None):
    """The link makers the template uses; state = what the address holds
    (job, open steps, CQs shown, confirm, live), sel = the job shown."""
    base = base_path(request)

    def url(**changes):
        s = {**state, **changes}
        q = []
        if s.get("job"):
            q.append(("job", s["job"]))
        if s.get("open"):
            q.append(("open", ".".join(str(n) for n in sorted(s["open"]))))
        if s.get("cqs"):
            q.append(("cqs", "1"))
        if s.get("confirm"):
            q.append(("confirm", s["confirm"]))
        if s.get("all"):
            q.append(("all", "1"))
        if s.get("live") == 0:
            q.append(("live", "0"))
        return base + ("?" + urllib.parse.urlencode(q) if q else "")

    def step_url(no):
        return url(job=sel, open=set(state.get("open") or ()) ^ {no}) + f"#s{no}"

    def dl(job_id, file=None):
        path = f"{base}download/{urllib.parse.quote(job_id, safe='')}"
        return path + (f"/{urllib.parse.quote(file, safe='')}" if file else "")
    return url, step_url, dl


def theme_link(request, theme):
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if request.method != "GET":
        here = base_path(request) + "new"
    return base_path(request) + "theme?" + urllib.parse.urlencode(
        {"to": "light" if theme == "dark" else "dark", "back": here})


def read_flash(request):
    raw = request.cookies.get(FLASH)
    if not raw:
        return None
    try:
        kind, text = json.loads(urllib.parse.unquote(raw))
        return (kind, str(text))
    except (ValueError, TypeError):
        return None


def redirect(to, flash=None):
    r = RedirectResponse(to, status_code=303)
    if flash:
        r.set_cookie(FLASH, urllib.parse.quote(json.dumps(flash)), max_age=60, httponly=True, samesite="lax")
    return r


def parse_open(value):
    return frozenset(int(x) for x in re.findall(r"\d+", value or "") if len(x) < 6)


# ----------------------------------------------------------------- pages --
async def render(request, tab, state=None, form=None, status=200):
    state = dict(state or {})
    banner, info, overview, d = "", None, None, None

    async def safe(coro):
        try:
            return await coro, ""
        except Backend as exc:
            return None, str(exc)
    (info, e1), (overview, e2) = await asyncio.gather(safe(get_info(request)), safe(n8n(request, "jobs")))
    banner = e2 or e1
    if overview is not None and overview.get("error"):
        banner, overview = f"The maseo service: {clean_error(overview['error'])}", None
    jobs = (overview or {}).get("jobs") or []
    now = (overview or {}).get("now") or time.time()

    sel = None
    if tab == "queue" and jobs:
        ids = [j["id"] for j in jobs]
        if state.get("job") in ids:
            sel = state["job"]
        else:          # nothing picked (or it is gone): follow the running job
            state.update(job=None, open=None, confirm=None)
            sel = next((j["id"] for j in jobs if j["status"] == "running"), ids[0])
        try:
            job = await n8n(request, "job", params={"id": sel})
            if job.get("id"):
                d = views.detail(job, now, info)
        except Backend as exc:
            banner = banner or str(exc)
    if d is None or state.get("confirm") not in ("cancel", "delete") \
            or (state["confirm"] == "cancel") != d["active"]:
        state["confirm"] = None

    live = state.get("live") != 0
    busy = any(j["status"] in views.ACTIVE for j in jobs) or bool(d and d["settling"])
    refresh = poll = None
    if tab == "queue" and live and not state.get("confirm"):
        refresh = CONFIG["refresh"] if busy or banner else CONFIG["refresh_idle"]
        poll = CONFIG["poll"] if busy or banner else CONFIG["poll_idle"]

    theme = "dark" if request.cookies.get(THEME) == "dark" else "light"
    url, step_url, dl = page_links(request, state, sel)
    running = (overview or {}).get("running")
    names = (info or {}).get("ollama_models")
    ctx = {
        "tab": tab, "base": base_path(request), "theme": theme, "theme_url": theme_link(request, theme),
        "refresh": refresh, "live": live, "banner": banner, "flash": read_flash(request),
        "css_version": int(os.path.getmtime(CSS)), "js_version": int(os.path.getmtime(JS)),
        "poll": poll, "now": round(now, 3),
        "chips": {
            "model": (("ok", f"Ollama · {views.plural(len(names), 'model')}") if names else
                      ("bad", "Ollama unreachable")) if info is not None else ("", "Ollama …"),
            "queue": (("run" if running else "ok",
                       f"{'1 running' if running else 'idle'} · {overview.get('queued', 0)} waiting")
                      if overview is not None else ("", "Queue …")),
        },
        "q": views.queue(jobs, now, bool(state.get("all")), sel), "sel": sel, "d": d,
        "confirm": state.get("confirm"), "open_steps": state.get("open") or frozenset(),
        "cqs_open": bool(state.get("cqs")),
        "url": url, "step_url": step_url, "dl": dl,
        "queue_url": url(confirm=None) if tab == "queue" else base_path(request),
    }
    if tab == "new":
        default = (info or {}).get("default_model")
        form = form or {"domain": "", "src": "text", "questions": "", "model": default,
                        "reason": False, "effort": "medium", "agents": list(AGENT_NAMES)}
        form["domain_clean"] = views.clean_name(form.get("domain"))
        form["text_count"] = views.count_cqs(form.get("questions")) if form.get("error") else 0
        ctx.update(form=form, models=views.form_models(info), agents=views.AGENTS,
                   effort_levels=views.EFFORT_LEVELS, stamp_now=datetime.now().strftime("%Y%m%d-%H%M%S"))
    resp = TEMPLATES.TemplateResponse(request, "index.html", ctx, status_code=status,
                                      headers={"Cache-Control": "no-store"})
    if ctx["flash"]:
        resp.delete_cookie(FLASH)          # a message is shown once
    return resp


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(login)])
async def page(request: Request, job: str = "", open: str = "", cqs: str = "", confirm: str = "",
               live: str = "", all: str = ""):
    return await render(request, "queue", {"job": job or None, "open": parse_open(open), "cqs": cqs == "1",
                                           "confirm": confirm or None, "live": 0 if live == "0" else None,
                                           "all": all == "1"})


@app.get("/new", response_class=HTMLResponse, dependencies=[Depends(login)])
async def new_job(request: Request):
    return await render(request, "new")


@app.post("/submit", dependencies=[Depends(login)])
async def submit(request: Request):
    f = await request.form()
    text = lambda k, d="": str(f.get(k) or d) if not hasattr(f.get(k), "read") else d
    form = {"domain": text("domain").strip(), "src": "file" if text("src") == "file" else "text",
            "questions": text("questions"), "model": text("model"), "reason": text("reason") == "on",
            "effort": text("effort", "medium"), "agents": [a for a in f.getlist("agents") if a in AGENT_NAMES],
            "kept_name": text("kept_name"), "kept_text": text("kept_text"), "error": ""}
    file_name = file_text = ""
    upload = f.get("cq_file")
    if form["src"] == "file":
        if upload is not None and hasattr(upload, "read") and upload.filename:
            name = os.path.basename(upload.filename)
            data = await upload.read(MAX_UPLOAD + 1)
            if not name.lower().endswith(UPLOAD_TYPES):
                form["error"] = f"{name}: use a .json, .txt or .csv file"
            elif len(data) > MAX_UPLOAD:
                form["error"] = f"{name} is larger than 5 MB"
            else:
                file_name, file_text = name, data.decode("utf-8-sig", errors="replace")
                if not views.count_cqs(file_text):
                    form["error"] = f"{name}: no competency questions found in it"
        elif form["kept_text"]:
            file_name, file_text = form["kept_name"], form["kept_text"]
        else:
            form["error"] = "Upload a competency question file first (or switch to Type)"
    elif not form["questions"].strip():
        form["error"] = "Type at least one competency question, or upload a file"
    if not views.clean_name(form["domain"]):
        form["error"] = "Fill in the Job Name, e.g. Clark_Wine"

    if not form["error"]:
        try:
            info = await get_info(request)
        except Backend:
            info = None
        body = {"domain": form["domain"], "model": form["model"], "agents": form["agents"],
                "reasoning": views.reasoning_value(form["model"], form["reason"], form["effort"], info)}
        if form["src"] == "file":
            body.update(cq_file_text=file_text, cq_file_name=file_name)
        else:
            body["questions"] = form["questions"]
        try:
            r = await n8n(request, "submit", "POST", body=body)
        except Backend as exc:
            r = {"error": str(exc)}
        if r.get("job_id"):
            return redirect(f"{base_path(request)}?job={urllib.parse.quote(r['job_id'])}#detail",
                            ("ok", f"Queued {r['job_id']} · {views.plural(r.get('cq_count') or 0, 'question')}"
                                   f" · position {r.get('position')}"))
        form["error"] = clean_error(r.get("error") or "Could not queue the job")
    if file_text:                     # keep the file for the next try
        form.update(kept_name=file_name, kept_text=file_text,
                    kept_info=f"{views.plural(views.count_cqs(file_text), 'question')} · "
                              f"{len(file_text.encode()) / 1024:.1f} KB")
    elif form["kept_text"]:
        form["kept_info"] = f"{views.plural(views.count_cqs(form['kept_text']), 'question')}"
    return await render(request, "new", form=form, status=400)


@app.post("/jobs/{job_id}/cancel", dependencies=[Depends(login)])
async def cancel(request: Request, job_id: str):
    back = f"{base_path(request)}?job={urllib.parse.quote(job_id)}#detail"
    try:
        r = await n8n(request, "cancel", "POST", params={"id": job_id})
    except Backend as exc:
        return redirect(back, ("err", str(exc)))
    if not r.get("id"):
        return redirect(back, ("err", clean_error(r.get("error") or "Could not cancel")))
    return redirect(back, ("ok", f"{job_id}: " + ("cancelled" if r.get("status") == "cancelled"
                                                  else "cancelling after the current step")))


@app.post("/jobs/{job_id}/delete", dependencies=[Depends(login)])
async def delete(request: Request, job_id: str):
    try:
        r = await n8n(request, "delete", "POST", params={"id": job_id})
    except Backend as exc:
        r = {"error": str(exc)}
    if not r.get("deleted"):
        return redirect(f"{base_path(request)}?job={urllib.parse.quote(job_id)}#detail",
                        ("err", clean_error(r.get("error") or "Could not delete")))
    return redirect(base_path(request), ("ok", f"Deleted {job_id}"))


@app.get("/download/{job_id}", dependencies=[Depends(login)])
@app.get("/download/{job_id}/{file}", dependencies=[Depends(login)])
async def download(request: Request, job_id: str, file: str = ""):
    """The job's zip or one of its files, streamed from n8n."""
    params = {"id": job_id, **({"file": file} if file else {})}
    client = request.app.state.client
    try:
        upstream = await client.send(client.build_request("GET", api_url("download"), params=params), stream=True)
    except httpx.HTTPError as exc:
        return PlainTextResponse(f"The web app cannot reach n8n ({exc.__class__.__name__}).", 502)
    if upstream.status_code != 200:
        text = (await upstream.aread()).decode("utf-8", "replace")[:300]
        await upstream.aclose()
        return PlainTextResponse(text or f"No such file ({upstream.status_code})", upstream.status_code)
    headers = {k: v for k, v in upstream.headers.items()
               if k.lower() in ("content-type", "content-disposition", "content-length")}
    headers["Cache-Control"] = "no-store"
    return StreamingResponse(upstream.aiter_bytes(), headers=headers, background=BackgroundTask(upstream.aclose))


@app.get("/theme", dependencies=[Depends(login)])
async def theme(request: Request, to: str = "dark", back: str = "/"):
    if not back.startswith("/") or back.startswith("//"):
        back = base_path(request)
    r = RedirectResponse(back, status_code=303)
    r.set_cookie(THEME, "dark" if to == "dark" else "light", max_age=365 * 86400, samesite="lax")
    return r


@app.get("/healthz")
async def healthz(request: Request):
    try:
        r = await request.app.state.client.get(api_url("info"), timeout=20)
        ok = r.status_code == 200
        return JSONResponse({"ok": ok}, 200 if ok else 503)
    except httpx.HTTPError:
        return JSONResponse({"ok": False, "error": "n8n does not answer"}, 503)


def main():
    ap = argparse.ArgumentParser(description="The MASEO web app (FastAPI) with n8n as the backend")
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8080), help="port (default 8080)")
    ap.add_argument("--n8n", default=CONFIG["n8n"], help=f"n8n base URL (default {CONFIG['n8n']})")
    ap.add_argument("--password", default=CONFIG["password"], help="ask for this password (default: none)")
    a = ap.parse_args()
    CONFIG["n8n"], CONFIG["password"] = a.n8n.rstrip("/"), a.password
    print(f"MASEO web app on http://{a.host}:{a.port}/  ->  n8n {CONFIG['n8n']}{CONFIG['api_path']}"
          f"  (password {'on' if a.password else 'off'})", flush=True)
    import uvicorn
    uvicorn.run(app, host=a.host, port=a.port, proxy_headers=True, log_level="warning")


if __name__ == "__main__":
    main()
