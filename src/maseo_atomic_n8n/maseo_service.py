#!/usr/bin/env python3
"""MASEO atomic as a service for n8n: maseo_atomic's own LangGraph
pipeline, one HTTP endpoint per graph node.

The n8n workflow is agent_graph.py's graph drawn in n8n: every graph node
(atomize_cqs, extract_terms, ... check_tool, correct_ontology, advance) is
an n8n node that calls this service, and every conditional edge
(the generation toggles, route_after_check, route_after_advance) is an n8n
IF/Switch node. The service runs the UNCHANGED maseo_atomic code for each
node - MASEOPipeline's own methods, the agents of agent.py talking to
Ollama through ChatOllama, the MCP validation servers of mcp_servers/
reached through tools.MCPToolbox over stdio, models.py for the schemas,
the OWL serializer and the provenance recorder - and keeps the LangGraph
state of each run between calls.

  Web app queue (the n8n web API workflow calls these):
  GET  /info                     models, reasoning, defaults
  POST /jobs                     queue a job             GET /jobs   queue + live progress
  GET  /jobs/<id>                one job with its log    POST /jobs/<id>/cancel
  POST /jobs/<id>/delete         remove a finished job and its files
  GET  /jobs/<id>/zip            the job's files         GET /jobs/<id>/file/<name>
  POST /queue/claim              (n8n worker) the next job, one at a time
  POST /jobs/<id>/fail           (n8n worker) a graph node failed

  Graph nodes (the n8n worker calls these for the claimed job):
  POST /runs                     start a run directly (the form's settings) -> run id
  POST /runs/<id>/node/<node>    run one graph node -> its output, the
                                 graph's next node, route, position, log
  POST /runs/<id>/finish         END: write_final() -> verdict, files
  GET  /runs/<id>                status        GET /runs          all runs
  GET  /runs/<id>/zip            every file of the run as a zip
  GET  /runs/<id>/file/<name>    one file      GET /              health

Environment: MASEO_ATOMIC (the maseo_atomic folder), MASEO_WORK_DIR (where
runs are written), MASEO_PORT (8030), OLLAMA_HOST (http://ollama:11434 in
the Docker deployment). Run: python maseo_service.py
"""
import asyncio
import contextvars
import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
MASEO_ATOMIC = os.path.abspath(os.environ.get("MASEO_ATOMIC")
                               or os.path.join(HERE, "..", "maseo_atomic"))
WORK_DIR = os.path.abspath(os.environ.get("MASEO_WORK_DIR")
                           or os.path.join(HERE, "outputs"))
PORT = int(os.environ.get("MASEO_PORT") or 8030)
OLLAMA_HOST = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA_HOST.startswith("http"):
    OLLAMA_HOST = "http://" + OLLAMA_HOST

# ---------------------------------------------------------------- output ---
# maseo_atomic prints its progress; every print of a graph node goes to the
# node's own log (returned to n8n) and to the run's run.log, and still to
# the container log. A context variable picks the sink, so concurrent runs
# (request threads and the asyncio tasks of the MCP calls) never mix.
_sink = contextvars.ContextVar("maseo_sink", default=None)


class _Router(io.TextIOBase):
    def __init__(self, real):
        self.real = real

    def writable(self):
        return True

    def write(self, text):
        sink = _sink.get()
        if sink is not None:
            sink.write(text)
        try:
            self.real.write(text)
        except Exception:
            pass
        return len(text)

    def flush(self):
        try:
            self.real.flush()
        except Exception:
            pass


class Sink:
    """One graph node's printed output, also appended to run.log."""

    def __init__(self, log_path):
        self.parts = []
        self.log_path = log_path
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.parts.append(text)
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(text)
            except OSError:
                pass

    def text(self, limit=60000):
        out = "".join(self.parts)
        if len(out) > limit:
            out = out[:limit // 2] + "\n... [cut, full text in run.log] ...\n" + out[-limit // 2:]
        return out


sys.stdout = _Router(sys.__stdout__)

# ------------------------------------------------------------ maseo_atomic ---
sys.path.insert(0, MASEO_ATOMIC)
import yaml                                   # noqa: E402
import agent_graph                            # noqa: E402  maseo_atomic, unchanged
from config import Config                     # noqa: E402
from tools import MCPToolbox                  # noqa: E402

CONFIG_PATH = os.path.join(MASEO_ATOMIC, "config.yaml")
DATASET_DIR = os.path.join(MASEO_ATOMIC, "dataset")
TOGGLES = {  # form checkbox label -> config.yaml generation key
    "CQAtomization": "atomic",
    "TermIdentification": "identification",
    "TermRefinement": "refinement",
    "AxiomGeneration": "axioms",
    "TestGeneration": "tests",
}
NODE_NAMES = ("atomize_cqs", "extract_terms", "identify_terms", "refine_terms",
              "finalize_terms", "generate_axioms", "generate_tests",
              "generate_ontology", "check_tool", "correct_ontology", "advance")


# ------------------------------------------------------------- MCP tools ---
class ToolHost:
    """The five MCP servers of maseo_atomic/mcp_servers, opened ONCE through
    tools.MCPToolbox (stdio, as agent_graph.run_pipeline does) on a
    dedicated asyncio loop and shared by every run. A keeper task holds the
    sessions open for the life of the service; check_tool coroutines run on
    the same loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True,
                         name="mcp-loop").start()
        self.toolbox = None
        self.ready = threading.Event()
        self.error = None
        self.stop = None
        self.lock = threading.Lock()

    async def _keeper(self):
        self.stop = asyncio.Event()
        try:
            async with MCPToolbox() as toolbox:
                self.toolbox = toolbox
                self.ready.set()
                await self.stop.wait()
        except Exception as exc:                  # a server failed to start
            self.error = f"{exc.__class__.__name__}: {exc}"
            self.ready.set()
        finally:
            self.toolbox = None

    def ensure(self):
        with self.lock:
            if self.toolbox is not None:
                return self.toolbox
            self.ready.clear()
            self.error = None
            asyncio.run_coroutine_threadsafe(self._keeper(), self.loop)
            self.ready.wait(180)
            if self.toolbox is None:
                raise RuntimeError("the MCP tool servers did not start: "
                                   + (self.error or "timeout"))
            return self.toolbox

    def restart(self):
        with self.lock:
            if self.stop is not None:
                self.loop.call_soon_threadsafe(self.stop.set)
            self.toolbox = None
        time.sleep(1)
        return self.ensure()

    def run(self, coroutine_factory, sink):
        async def wrapped():
            _sink.set(sink)
            return await coroutine_factory()
        return asyncio.run_coroutine_threadsafe(wrapped(), self.loop).result()

    def names(self):
        try:
            return sorted(self.ensure()._tools)
        except Exception as exc:
            return [f"unavailable: {exc}"]


TOOLS = ToolHost()


# ------------------------------------------------------------ the inputs ---
def parse_cqs(text):
    """Competency questions from pasted text or an uploaded file: a JSON
    list of {"id", "value"} (the dataset format), a JSON object holding
    such a list, a CSV with id/value columns, or plain lines
    ("CQ1: question" or just the question)."""
    text = (text or "").strip().lstrip("﻿")
    if not text:
        return []
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        if isinstance(data, list):
            out = []
            for i, c in enumerate(data, start=1):
                if isinstance(c, dict):
                    value = c.get("value") or c.get("question") or c.get("cq") or c.get("text")
                    cid = c.get("id") or c.get("cq_id") or f"CQ{i}"
                else:
                    value, cid = c, f"CQ{i}"
                if value and str(value).strip():
                    out.append({"id": str(cid).strip(), "value": str(value).strip()})
            return out
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and "," in lines[0] and re.search(r"\b(value|question|cq)\b", lines[0], re.I):
        rows = list(csv.DictReader(io.StringIO(text)))
        out = []
        for i, r in enumerate(rows, start=1):
            low = {str(k).strip().lower(): (v or "").strip() for k, v in r.items() if k}
            value = low.get("value") or low.get("question") or low.get("cq")
            if value:
                out.append({"id": low.get("id") or f"CQ{i}", "value": value})
        return out
    out = []
    for i, line in enumerate(lines, start=1):
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", line.strip())
        m = re.match(r"^([A-Za-z][\w .-]{0,20}?\d[\w-]*)\s*[:)]\s*(.+)$", line)
        if m:
            out.append({"id": m.group(1).strip(), "value": m.group(2).strip()})
        else:
            out.append({"id": f"CQ{i}", "value": line})
    return out


def agent_list(value):
    """The Agents checkbox: a list, a JSON array string, or comma text.
    None or empty = every box unticked."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except ValueError:
            value = [v for v in text.split(",")]
    if not isinstance(value, list):
        value = [value]
    names = []
    for v in value:
        key = re.sub(r"[^a-z]", "", str(v).lower())
        for label in TOGGLES:
            if key and key.startswith(label.lower()) and label not in names:
                names.append(label)
    return names


def reasoning_value(value):
    text = str(value if value is not None else "off").strip().lower()
    text = text.split()[0] if text else "off"
    return {"off": False, "false": False, "no": False, "none": False,
            "on": True, "true": True, "yes": True}.get(text, text)


_CAPS = {}
_CAPS_LOCK = threading.Lock()
EFFORT_LEVELS = ["low", "medium", "high"]


MODELS_FILE = os.path.abspath(os.environ.get("MASEO_MODELS_FILE")
                              or os.path.join(HERE, "reasoning_models.yaml"))
_LISTED = {"mtime": None, "models": {}}


def listed_models():
    """reasoning_models.yaml: model -> the reasoning settings it accepts
    (re-read whenever the file changes)."""
    try:
        mtime = os.path.getmtime(MODELS_FILE)
    except OSError:
        return {}
    if _LISTED["mtime"] != mtime:
        try:
            with open(MODELS_FILE, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            models = data.get("models") or {}
            _LISTED["models"] = {str(k): v if isinstance(v, list) else [v] for k, v in models.items()}
        except Exception as exc:
            print(f"[service] could not read {MODELS_FILE}: {exc}", flush=True)
            _LISTED["models"] = {}
        _LISTED["mtime"] = mtime
    return _LISTED["models"]


def listed_caps(name):
    """The capabilities reasoning_models.yaml gives a model, or None."""
    models = listed_models()
    allowed = models.get(name)
    if allowed is None and name.endswith(":latest"):
        allowed = models.get(name[:-len(":latest")])
    if allowed is None and ":" not in name:
        allowed = models.get(name + ":latest")
    if allowed is None:
        return None
    values = [reasoning_value(v) for v in allowed]
    levels = [lv for lv in EFFORT_LEVELS if lv in values]
    return {"name": name, "thinking": bool(levels) or True in values,
            "levels": levels, "source": "reasoning_models.yaml"}


def model_caps(name, max_age=600):
    """What a model can do with reasoning. reasoning_models.yaml first;
    for a model not listed there, Ollama's /api/show: thinking = "thinking"
    is among its capabilities; effort levels = its prompt template reads
    Ollama's think level ({{ .ThinkLevel }}, as gpt-oss style templates do).
    thinking None = Ollama did not say."""
    listed = listed_caps(name)
    if listed is not None:
        return listed
    with _CAPS_LOCK:
        hit = _CAPS.get(name)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
    caps = {"name": name, "thinking": None, "levels": [], "source": "ollama"}
    try:
        req = urllib.request.Request(OLLAMA_HOST + "/api/show",
                                     data=json.dumps({"model": name}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            info = json.load(r)
        listed = info.get("capabilities")
        if isinstance(listed, list):
            caps["thinking"] = "thinking" in listed
        family = str((info.get("details") or {}).get("family") or "").lower()
        if caps["thinking"] and ("ThinkLevel" in str(info.get("template") or "")
                                 or family in ("gptoss", "gpt-oss")):
            caps["levels"] = list(EFFORT_LEVELS)
    except Exception:
        return caps                                  # not cached: ask again next time
    with _CAPS_LOCK:
        _CAPS[name] = (time.time(), caps)
    return caps


def reasoning_for(model, value):
    """The reasoning setting a job really gets: off for a model that cannot
    think, on/off only for a model without effort levels."""
    caps = model_caps(model)
    v = reasoning_value(value)
    if v is False or caps["thinking"] is False:
        return "off"
    if caps["levels"]:
        return v if isinstance(v, str) and v in caps["levels"] else "medium"
    return "on"


def loaded_models():
    """The models Ollama holds in memory right now (GET /api/ps)."""
    try:
        with urllib.request.urlopen(OLLAMA_HOST + "/api/ps", timeout=15) as r:
            return [m.get("name") or m.get("model") for m in json.load(r).get("models", [])]
    except Exception:
        return None


def release_model(name, wait=20):
    """Unload a model from Ollama (an empty request with keep_alive 0, as
    Ollama documents), then wait until /api/ps no longer lists it. Returns
    a short text for the run log."""
    try:
        req = urllib.request.Request(OLLAMA_HOST + "/api/generate",
                                     data=json.dumps({"model": name, "keep_alive": 0}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
    except Exception as exc:
        return f"could not release {name} from Ollama: {exc}"
    deadline = time.time() + wait
    while time.time() < deadline:
        loaded = loaded_models()
        if loaded is None or not any(m in (name, f"{name}:latest") for m in loaded):
            return f"Ollama model {name} released"
        time.sleep(1)
    return f"Ollama model {name}: unload requested (still listed after {wait}s, it frees when its last call ends)"


def ollama_models():
    try:
        with urllib.request.urlopen(OLLAMA_HOST + "/api/tags", timeout=15) as r:
            return sorted(m.get("name") or m.get("model")
                          for m in json.load(r).get("models", []))
    except Exception:
        return None


# -------------------------------------------------------------- the runs ---
RUNS = {}
RUNS_LOCK = threading.Lock()
RELEASE_LOCK = threading.Lock()      # one Ollama unload at a time
PENDING_RELEASE = set()              # models of jobs cut off by a service restart


def resolve_cqs(body, allow_dataset=True):
    """(domain, cqs, source) of a request: the uploaded file wins, then the
    pasted questions, then (legacy form only) the dataset of the domain."""
    domain = re.sub(r"[^A-Za-z0-9_-]+", "_", str(body.get("domain") or "").strip()).strip("_")
    if not domain:
        raise ValueError("Job Name is required (letters, digits, _ or -), e.g. Clark_Wine")
    cqs = parse_cqs(body.get("cq_file_text") or "")
    source = "uploaded file" + (f" {body.get('cq_file_name')}" if body.get("cq_file_name") else "")
    if not cqs:
        cqs = parse_cqs(body.get("questions") or "")
        source = "pasted questions"
    if not cqs and allow_dataset:
        path = os.path.join(DATASET_DIR, f"{domain}_cq2onto_cqs.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                cqs = parse_cqs(f.read())
            source = f"dataset {domain}_cq2onto_cqs.json"
    if not cqs:
        raise ValueError("No competency questions: type them or upload a file"
                         + (f", or use a domain with a dataset file ({', '.join(dataset_names())})"
                            if allow_dataset else ""))
    seen = set()
    for c in cqs:                      # ids must be unique for the joins
        base, n = c["id"], 2
        while c["id"] in seen:
            c["id"] = f"{base}_{n}"
            n += 1
        seen.add(c["id"])
    return domain, cqs, source


def dataset_names():
    return sorted(n[:-len("_cq2onto_cqs.json")] for n in os.listdir(DATASET_DIR)
                  if n.endswith("_cq2onto_cqs.json"))


def check_model(model):
    model = str(model or "").strip()
    models = ollama_models()
    if model and models and model not in models and f"{model}:latest" not in models:
        raise ValueError(f"Ollama has no model '{model}'. Pulled: {', '.join(models)} "
                         f"(docker compose exec ollama ollama pull {model})")


STEP_LABELS = {"atomize_cqs": "CQAtomization", "extract_terms": "TermExtraction",
               "identify_terms": "TermIdentification", "refine_terms": "TermRefinement",
               "finalize_terms": "Requirements", "generate_axioms": "AxiomGeneration",
               "generate_tests": "TestGeneration", "generate_ontology": "OntoGeneration"}


class StepFile:
    """A run file copied as-is into a step's files (so the bytes match)."""

    def __init__(self, path):
        self.path = path


def file_sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def jsonable(value):
    """json.dump default: pydantic models (the pipeline's state) as dicts."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (set, tuple)):
        return list(value)
    return str(value)


class Cancelled(Exception):
    pass


# ------------------------------------------------------------ cancelling ---
class CancellableLLM:
    """Wraps the agents' model (agent.llm) so a cancelled run makes no new
    model call; abort() also cuts the call in flight: the Ollama connection
    is shut down, and Ollama stops generating when its client goes away."""

    def __init__(self, llm, run):
        self.llm, self.run = llm, run

    def invoke(self, messages, *args, **kwargs):
        if self.run.cancel_requested:
            raise Cancelled(f"run {self.run.id} was cancelled from the web app")
        return self.llm.invoke(messages, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.llm, name)

    def abort(self):
        chat = getattr(self.llm, "bound", self.llm)            # RunnableBinding -> ChatOllama
        for attr in ("_client", "_async_client"):
            http = getattr(getattr(chat, attr, None), "_client", None)   # ollama.Client -> httpx
            pool = getattr(getattr(http, "_transport", None), "_pool", None)
            for conn in list(getattr(pool, "connections", None) or []):
                stream = getattr(getattr(conn, "_connection", None), "_network_stream", None)
                sock = getattr(stream, "_sock", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass



class Run:

    def __init__(self, body, run_id=None):
        domain, cqs, source = resolve_cqs(body)

        model = str(body.get("model") or "").strip()
        reasoning = reasoning_value(body.get("reasoning"))
        agents = agent_list(body.get("agents")) if "agents" in body else list(TOGGLES)

        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        raw["model"]["provider"] = "ollama"
        ollama_cfg = dict(raw["model"].get("ollama") or {})
        ollama_cfg["id"] = model or ollama_cfg.get("id") or "qwen3.6:27b"
        ollama_cfg["host"] = OLLAMA_HOST
        ollama_cfg["reasoning"] = reasoning
        raw["model"]["ollama"] = ollama_cfg
        gen = raw.setdefault("generation", {}) or {}
        gen["atomic"] = "CQAtomization" in agents
        gen["identification"] = "TermIdentification" in agents
        gen["refinement"] = "TermRefinement" in agents
        gen["others"] = [k for k in ("axioms", "tests")
                         if {"axioms": "AxiomGeneration", "tests": "TestGeneration"}[k] in agents]
        raw["generation"] = gen

        self.id = run_id or new_id(domain)
        self.dir = os.path.join(WORK_DIR, self.id)
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, f"{domain}_cq2onto_cqs.json"), "w",
                  encoding="utf-8") as f:
            json.dump(cqs, f, indent=2, ensure_ascii=False)
        raw.setdefault("dataset", {})["cq_dir"] = self.dir
        raw["dataset"]["file_pattern"] = "{domain}_cq2onto_cqs.json"
        raw.setdefault("run", {})["output_dir"] = self.dir
        with open(os.path.join(self.dir, "config.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, sort_keys=False, allow_unicode=True, width=100)

        self.log_path = os.path.join(self.dir, "run.log")
        self.lock = threading.Lock()
        self.domain = domain
        self.source = source
        self.cqs = cqs
        self.agents = agents
        self.state = {}
        self.last = None          # the last graph node run
        self.history = []         # (node, seconds)
        self.status = "running"
        self.error = ""
        self.created = time.time()
        self.summary = None
        self.last_tool = ""
        self.current = None       # the graph node executing right now
        self.current_since = None
        self.last_activity = time.time()
        self.cancel_requested = False
        self.llms = {}
        self.step_no = 0          # graph steps with saved input/output files
        self.published = {}       # short file name -> the step output it came from
        self.steps_dir = os.path.join(self.dir, "steps")
        self.current_step = None  # the running step: number, prefix, files so far
        sink = Sink(self.log_path)
        token = _sink.set(sink)
        try:
            # config_dir = the maseo_atomic folder, so HermiT.jar, themis.jar
            # and claro_templates.txt resolve exactly as in a CLI run
            self.config = Config(raw, config_dir=Path(MASEO_ATOMIC),
                                 domain=domain)
            self.pipeline = agent_graph.MASEOPipeline(self.config)
            self.llms = {}
            for agent in self.pipeline.agents.values():
                key = id(agent.llm)
                if key not in self.llms:
                    self.llms[key] = CancellableLLM(agent.llm, self)
                agent.llm = self.llms[key]
            print(f"MASEO atomic (n8n) starting for domain '{domain}' - run {self.id}")
            print(f"Model: ollama/{self.config.model_id}  reasoning: {reasoning}  host: {OLLAMA_HOST}")
            print(f"CQs: {len(cqs)} from {source}")
            print(f"Pipeline: {' > '.join(self.stages())}")
            print(f"Experiment mode: {self.config.mode}")
            print(f"Tools: {', '.join(t[0] for t in self.pipeline.tools)}")
            print(f"Tool retries: {self.config.tool_retries}  Loop rounds: {self.config.loop_rounds}")
        finally:
            _sink.reset(token)
        self.start_log = sink.text()

    def release_model(self):
        """The job is over (finished, failed or cancelled): unload its model
        from Ollama. Done once, before the worker starts the next job."""
        if not hasattr(self, "config"):
            return ""
        # a cancel thread and the worker's fail call can both get here; the
        # second one waits until the first has unloaded the model
        with RELEASE_LOCK:
            if getattr(self, "released", False):
                return self.release_note
            self.release_note = release_model(self.config.model_id)
            self.released = True
        print(f"[service] {self.id}: {self.release_note}", flush=True)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[service] {self.release_note}\n")
        except OSError:
            pass
        return self.release_note

    def abort(self):
        """Cancel: no new graph node, no new model call, and the model call
        in flight is cut off."""
        self.cancel_requested = True
        for llm in list(self.llms.values()):
            llm.abort()

    # the graph of agent_graph.build_graph for this run's toggles
    def order(self):
        c = self.config
        nodes = ["atomize_cqs", "extract_terms"]
        if c.run_identification:
            nodes.append("identify_terms")
        if c.run_refinement:
            nodes.append("refine_terms")
        nodes.append("finalize_terms")
        if c.run_axioms:
            nodes.append("generate_axioms")
        if c.run_tests:
            nodes.append("generate_tests")
        nodes.append("generate_ontology")
        return nodes

    def stages(self):
        c = self.config
        names = ["CQAtomization"] if c.run_atomization else ["(CQAtomization off)"]
        names.append("TermExtraction")
        if c.run_identification:
            names.append("TermIdentification")
        if c.run_refinement:
            names.append("TermRefinement")
        if c.run_axioms:
            names.append("AxiomGeneration")
        if c.run_tests:
            names.append("TestGeneration")
        return names + ["OntoGeneration", "validation loop (OntoCorrection)"]

    def next_node(self):
        """What LangGraph would run after self.last (the conditional edges
        are the pipeline's own route_after_check / route_after_advance)."""
        order = self.order()
        if self.last is None:
            return order[0]
        if self.last in order[:-1]:
            return order[order.index(self.last) + 1]
        if self.last == "generate_ontology":
            return "check_tool"
        if self.last == "check_tool":
            return {"correct": "correct_ontology",
                    "advance": "advance"}[self.pipeline.route_after_check(self.state)]
        if self.last == "correct_ontology":
            return "check_tool"
        if self.last == "advance":
            return {"check": "check_tool",
                    "finish": "finish"}[self.pipeline.route_after_advance(self.state)]
        return "done"

    def current_tool(self):
        idx = self.state.get("tool_index")
        if idx is None or not (0 <= idx < len(self.pipeline.tools)):
            return ""
        return self.pipeline.tools[idx][0]

    def position(self):
        s = self.state
        return {"round": s.get("round"), "phase": s.get("phase"),
                "attempt": s.get("attempt"), "tool_index": s.get("tool_index"),
                "tool": self.current_tool(), "round_failed": s.get("round_failed"),
                "loop_rounds": self.config.loop_rounds,
                "tool_retries": self.config.tool_retries}

    def settings(self):
        c = self.config
        return {"domain": self.domain, "model": f"ollama/{c.model_id}",
                "reasoning": c.params.get("reasoning"), "mode": c.mode,
                "cq_source": self.source, "cq_count": len(self.cqs),
                "agents_on": self.agents, "stages": self.stages(),
                "atomic": c.run_atomization, "identification": c.run_identification,
                "refinement": c.run_refinement, "axioms": c.run_axioms,
                "tests": c.run_tests, "tools": [t[0] for t in self.pipeline.tools],
                "tool_retries": c.tool_retries, "loop_rounds": c.loop_rounds,
                "parse_retries": c.parse_retries, "base_uri": c.base_uri,
                "output_dir": self.dir}

    def node_output(self, node, update):
        """What the n8n node shows: the part of the state the node produced."""
        def dump(v):
            return v.model_dump() if hasattr(v, "model_dump") else v
        if node == "check_tool":
            return {"tool": self.last_tool, "result": self.state.get("result")}
        if node == "advance":
            return {k: v for k, v in (update or {}).items()}
        if node in ("generate_ontology", "correct_ontology"):
            answer = self.state.get("answer")
            return {"entities": len(answer.OWL) if answer else 0,
                    "reason": getattr(answer, "reason", ""),
                    "ontology_file": self.pipeline.onto_path,
                    "ontology": self.pipeline.serialize(answer) if answer else ""}
        return {k: dump(v) for k, v in (update or {}).items()}

    # ------------------------------------------------ per-step files ---
    # Every graph step (all but advance) keeps its own input and output files
    # in <run>/steps/, named <domain>_step<NN>_<label>_input_<x> / _output_<x>.
    # The files are coherent along the pipeline: what one step hands over is
    # an output of that step AND, under the same name and with the same bytes,
    # the input of the step that consumes it:
    #   CQAtomization     cqs.json            -> atomic_cqs.json
    #   TermExtraction    atomic_cqs.json     -> cq_terms.json (+ term_document.json
    #                                            when TermIdentification is off)
    #   TermIdentification cq_terms.json      -> term_document.json
    #   TermRefinement    term_document.json  -> term_document.json
    #   Requirements      term_document.json  -> cq_refined_terms.json
    #   Axiom/TestGeneration cqs.json, cq_refined_terms.json -> axiom.json / tests.json
    #   OntoGeneration    cq_refined_terms.json, axiom.json, tests.json -> ontology.owl
    #   <tool> check      ontology.owl (+ cq_refined_terms.json / tests.json) -> result.json
    #   OntoCorrection    ontology.owl, result.json -> ontology.owl
    # Agent steps also output reply.json: what the model itself answered
    # (with its reasoning and any error). Inputs are written when the step
    # starts, outputs when it ends; each input notes the step whose output
    # it is (files["from"]).
    def begin_step(self, node, tool):
        self.step_no += 1
        label = {"check_tool": tool or "check", "correct_ontology": f"OntoCorrection-{tool}"}.get(
            node, STEP_LABELS.get(node, node))
        prefix = f"{self.domain}_step{self.step_no:02d}_{label}"
        step = {"no": self.step_no, "node": node, "label": label, "prefix": prefix, "input": [], "output": [],
                "from": {}, "n0": len(getattr(self.pipeline, "steps", [])), "marks": self.file_marks()}
        try:
            os.makedirs(self.steps_dir, exist_ok=True)
            for short, data in self.step_inputs(node):
                name = self.write_step_file(prefix, "input_" + short, data)
                step["input"].append(name)
                src = self.published.get(short)
                if short == "cqs.json":
                    step["from"][name] = "the job's questions"
                elif src and src["sha"] == file_sha1(os.path.join(self.steps_dir, name)):
                    step["from"][name] = f"{src['label']} (step {src['step']})"
        except Exception as exc:
            print(f"[service] could not save the inputs of step {self.step_no}: {exc}")
        self.current_step = step
        return step

    def step_inputs(self, node):
        """(file name, data) the step starts from; data is a str, a JSON-able
        value, or StepFile(path) for a copy of a run file."""
        st, p = self.state, self.pipeline
        items = []

        def add(short, make):
            try:
                value = make()
                if value is not None:
                    items.append((short, value))
            except Exception as exc:
                print(f"[service] step input {short}: {exc}")

        def add_file(short, path):
            if path and os.path.isfile(path):
                items.append((short, StepFile(path)))
        if node == "atomize_cqs":
            add("cqs.json", lambda: p.cqs)
        elif node == "extract_terms":
            add("atomic_cqs.json", lambda: agent_graph.atomization_document(p.cqs, st["atomization"]))
        elif node == "identify_terms":
            add_file("cq_terms.json", p.cq_terms_path)
        elif node in ("refine_terms", "finalize_terms"):
            add("term_document.json", lambda: agent_graph.build_final_document(
                p.cqs, st["atomization"], st["term_map"]))
        elif node in ("generate_axioms", "generate_tests"):
            add("cqs.json", lambda: p.cqs)
            add_file("cq_refined_terms.json", p.cq_refined_terms_path)
        elif node == "generate_ontology":
            add_file("cq_refined_terms.json", p.cq_refined_terms_path)
            if st.get("axioms") is not None:
                add_file("axiom.json", p.axiom_path)
            if st.get("tests") is not None:
                add_file("tests.json", p.tests_path)
        elif node in ("check_tool", "correct_ontology"):
            add_file("ontology.owl", p.onto_path)
            tool = self.current_tool()
            if node == "check_tool" and tool == "cq_coverage":
                add_file("cq_refined_terms.json", p.cq_refined_terms_path)
            if node == "check_tool" and tool == "themis_test":
                add_file("tests.json", p.tests_path)
            if node == "correct_ontology":
                add("result.json", lambda: st.get("result"))
        return items

    def write_step_file(self, prefix, short, data):
        name = f"{prefix}_{short}"
        path = os.path.join(self.steps_dir, name)
        if isinstance(data, StepFile):
            shutil.copyfile(data.path, path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                if isinstance(data, str):
                    f.write(data)
                else:
                    json.dump(data, f, indent=2, ensure_ascii=False, default=jsonable)
        return name

    def file_marks(self):
        marks = {}
        for n in os.listdir(self.dir):
            full = os.path.join(self.dir, n)
            if os.path.isfile(full):
                st = os.stat(full)
                marks[n] = (st.st_mtime_ns, st.st_size)
        return marks

    def end_step(self, step, update):
        """Write the step's outputs; returns what the history entry gets."""
        if step is None:
            return {}
        prefix, node = step["prefix"], step.get("node")
        try:
            entries = list(getattr(self.pipeline, "steps", [])[step["n0"]:])
            calls = [e for e in entries if e.get("agent")]
            if calls:                         # what the model answered, call by call
                out = [{k: e[k] for k in ("agent", "tool", "error_message", "round", "attempt",
                                          "thinking", "output")
                        if e.get(k) not in (None, "")} for e in calls]
                step["output"].append(self.write_step_file(
                    prefix, "output_reply.json", out[0] if len(out) == 1 else out))
            elif entries:                     # a tool check: its full result
                last = entries[-1]
                result = last.get("output") if isinstance(last.get("output"), dict) else \
                    {"passed": False, "error": last.get("error_message") or "the tool call failed"}
                step["output"].append(self.write_step_file(prefix, "output_result.json", result))
            # every run file the step wrote, as it was at the end of the step
            marks = self.file_marks()
            for n in sorted(marks):
                if n == "run.log" or n.endswith("_steps.json") or step["marks"].get(n) == marks[n]:
                    continue
                short = n[len(self.domain) + 1:] if n.startswith(self.domain + "_") else n
                step["output"].append(self.write_step_file(
                    prefix, f"output_{short}", StepFile(os.path.join(self.dir, n))))
            # the document the next requirements step starts from
            if update is not None:
                st, p = self.state, self.pipeline
                if node == "atomize_cqs":
                    step["output"].append(self.write_step_file(
                        prefix, "output_atomic_cqs.json",
                        agent_graph.atomization_document(p.cqs, st["atomization"])))
                elif node in ("identify_terms", "refine_terms") or (
                        node == "extract_terms" and "TermIdentification" not in self.agents):
                    step["output"].append(self.write_step_file(
                        prefix, "output_term_document.json",
                        agent_graph.build_final_document(p.cqs, st["atomization"], st["term_map"])))
            if not step["output"] and update:
                step["output"].append(self.write_step_file(prefix, "output_state.json", update))
            for name in step["output"]:
                short = name[len(prefix) + len("_output_"):]
                self.published[short] = {"sha": file_sha1(os.path.join(self.steps_dir, name)),
                                         "label": step["label"], "step": step["no"]}
        except Exception as exc:
            print(f"[service] could not save the outputs of step {step['no']}: {exc}")
        self.current_step = None
        return {"step": step["no"], "files": {"prefix": prefix, "input": step["input"],
                                              "output": step["output"], "from": step["from"]}}

    def run_node(self, node, expect_tool=""):
        if node not in NODE_NAMES:
            raise KeyError(f"unknown graph node '{node}'")
        with self.lock:
            if self.status != "running":
                raise RuntimeError(f"run {self.id} is {self.status}")
            wanted = self.next_node()
            if node != wanted:
                raise PermissionError(f"graph order: after '{self.last}' the graph runs "
                                      f"'{wanted}', not '{node}' - check the workflow wiring")
            if node == "check_tool" and expect_tool and expect_tool != self.current_tool():
                raise PermissionError(f"check_tool would call '{self.current_tool()}', "
                                      f"the workflow sent it to '{expect_tool}'")
            if self.cancel_requested:
                self.status = "cancelled"
                self.error = "cancelled from the web app"
                raise Cancelled(f"run {self.id} was cancelled from the web app")
            sink = Sink(self.log_path)
            token = _sink.set(sink)
            t0 = time.time()
            before = {"round": self.state.get("round"), "phase": self.state.get("phase"),
                      "attempt": self.state.get("attempt"),
                      "tool": self.current_tool() if node in ("check_tool", "correct_ontology") else ""}
            self.current, self.current_since = node, t0
            self.current_tool_name = before["tool"]
            self.last_activity = t0
            step = self.begin_step(node, before["tool"]) if node != "advance" else None
            update = None
            try:
                if node == "check_tool":
                    self.last_tool = self.current_tool()
                    self.pipeline.toolbox = TOOLS.ensure()
                    try:
                        update = TOOLS.run(lambda: self.pipeline.check_tool(self.state), sink)
                    except Exception as exc:
                        # an MCP session that died: reopen the servers once
                        print(f"[service] MCP call failed ({exc}); restarting the tool servers")
                        self.pipeline.toolbox = TOOLS.restart()
                        update = TOOLS.run(lambda: self.pipeline.check_tool(self.state), sink)
                else:
                    update = getattr(self.pipeline, node)(self.state)
                self.state.update(update or {})
            except Exception as exc:
                files = self.end_step(step, None)
                if self.cancel_requested:
                    self.status = "cancelled"
                    self.error = "cancelled from the web app"
                    print(f"\n[service] run cancelled during {node}")
                    self.history.append(dict(before, node=node, failed=True, cancelled=True,
                                             seconds=round(time.time() - t0, 1), **files))
                    raise Cancelled(f"run {self.id} was cancelled from the web app") from exc
                self.status = "failed"
                self.error = f"{node}: {exc.__class__.__name__}: {exc}"
                print(f"\n[service] graph node {node} failed:\n{traceback.format_exc()}")
                self.history.append(dict(before, node=node, failed=True,
                                         seconds=round(time.time() - t0, 1), **files))
                raise
            finally:
                _sink.reset(token)
                self.current = None
                self.last_activity = time.time()
            seconds = round(time.time() - t0, 1)
            files = self.end_step(step, update)
            if self.cancel_requested:
                self.status = "cancelled"
                self.error = "cancelled from the web app"
                self.history.append(dict(before, node=node, failed=True, cancelled=True,
                                         seconds=seconds, **files))
                raise Cancelled(f"run {self.id} was cancelled from the web app")
            self.last = node
            entry = dict(before, node=node, seconds=seconds, **files)
            if node == "check_tool":
                result = self.state.get("result") or {}
                entry.update(passed=bool(result.get("passed")),
                             tool_error=bool(result.get("tool_error")),
                             report=str(result.get("report") or "")[:400])
            if node == "advance":
                entry.update({k: v for k, v in (update or {}).items()
                              if k in ("phase", "round", "done", "all_passed")})
            self.history.append(entry)
            nxt = self.next_node()
            reply = {"run_id": self.id, "node": node, "seconds": seconds,
                     "next": nxt, "tool": self.current_tool() if nxt == "check_tool" else "",
                     "position": self.position(),
                     "output": self.node_output(node, update),
                     "log": sink.text()}
            if node == "check_tool":
                reply["route"] = self.pipeline.route_after_check(self.state)
                reply["checked_tool"] = self.last_tool
                reply["passed"] = bool((self.state.get("result") or {}).get("passed"))
            if node == "advance":
                reply["route"] = self.pipeline.route_after_advance(self.state)
            return reply

    def finish(self):
        with self.lock:
            if self.status == "finished":
                return self.summary
            if self.next_node() != "finish":
                raise PermissionError(f"graph order: after '{self.last}' the graph runs "
                                      f"'{self.next_node()}', not END")
            sink = Sink(self.log_path)
            token = _sink.set(sink)
            try:
                self.pipeline.write_final(self.state["answer"])
                passed = bool(self.state.get("all_passed"))
                print("\n" + "=" * 50)
                print("ALL CHECKS PASSED" if passed else "NOT ALL CHECKS PASSED")
                print("=" * 50)
            finally:
                _sink.reset(token)
            steps = self.pipeline.steps
            corrections = sum(1 for s in steps if s.get("agent") == "OntoCorrection Agent"
                              and s.get("output"))
            last_checks = {}
            for s in steps:
                if s.get("tool") and isinstance(s.get("output"), dict):
                    last_checks[s["tool"]] = {"passed": bool(s["output"].get("passed")),
                                              "tool_error": bool(s["output"].get("tool_error")),
                                              "report": str(s["output"].get("report") or "")[:3000]}
            self.status = "finished"
            self.last_activity = time.time()
            self.summary = {
                "run_id": self.id, "verdict": "ALL CHECKS PASSED" if passed
                else "NOT ALL CHECKS PASSED", "all_passed": passed,
                "rounds": self.state.get("round"), "corrections": corrections,
                "minutes": round((time.time() - self.created) / 60, 1),
                "settings": self.settings(), "last_checks": last_checks,
                "graph_path": self.history, "files": self.files(),
                "final_ontology": self.pipeline.onto_path,
                "log": sink.text()}
            with open(os.path.join(self.dir, "run_summary.json"), "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in self.summary.items() if k != "log"}, f,
                          indent=2, ensure_ascii=False)
            self.pipeline = _Finished(self.pipeline)   # free the agents
            self.summary["model_release"] = self.release_model()
            self.summary["files"] = self.files()
            return self.summary

    def files(self):
        return sorted(n for n in os.listdir(self.dir)
                      if os.path.isfile(os.path.join(self.dir, n)))

    def zip_bytes(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name in self.files():
                z.write(os.path.join(self.dir, name), f"{self.id}/{name}")
        return buf.getvalue()

    def status_view(self):
        return {"run_id": self.id, "status": self.status, "error": self.error,
                "last_node": self.last, "next": self.next_node() if self.status == "running" else "",
                "position": self.position() if self.status == "running" else {},
                "settings": self.settings() if self.status == "running" else
                (self.summary or {}).get("settings"),
                "graph_path": self.history, "files": self.files(),
                "verdict": (self.summary or {}).get("verdict")}


class _Finished:
    """What stays of a finished pipeline: the paths and tool list."""

    def __init__(self, p):
        self.tools = p.tools
        self.onto_path = p.onto_path
        self.steps = p.steps

    def route_after_check(self, state):
        return "advance"

    def route_after_advance(self, state):
        return "finish"



# ------------------------------------------------------------- the queue ---
# The web app submits jobs; the n8n worker workflow claims them one at a
# time (POST /queue/claim) and walks the graph for the claimed job. Jobs are
# kept in <work dir>/jobs.json, so the queue survives a restart of the
# service; a job that was running when the service stopped is marked failed.
JOBS = {}
JOBS_LOCK = threading.RLock()
JOBS_FILE = os.path.join(WORK_DIR, "jobs.json")
WORKER_URL = os.environ.get("MASEO_WORKER_URL") or "http://n8n:5678/webhook/maseo-worker"
STALL_SECONDS = int(os.environ.get("MASEO_STALL_SECONDS") or 300)
REQUEST_KEYS = ("domain", "questions", "cq_file_text", "cq_file_name", "model", "reasoning",
                "agents")
GRAPH_STEPS = [  # (graph node, label, generation toggle or None = always)
    ("atomize_cqs", "CQAtomization", "CQAtomization"),
    ("extract_terms", "TermExtraction", None),
    ("identify_terms", "TermIdentification", "TermIdentification"),
    ("refine_terms", "TermRefinement", "TermRefinement"),
    ("generate_axioms", "AxiomGeneration", "AxiomGeneration"),
    ("generate_tests", "TestGeneration", "TestGeneration"),
    ("generate_ontology", "OntoGeneration", None),
]
_last_kick = [0.0]


def new_id(domain):
    """The run's unique id: what the user typed as Job Name (e.g.
    Clark_Wine) plus the date and time it was queued, e.g.
    Clark_Wine_20260923-151105. Its folder and zip carry this name."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rid, n = f"{domain}_{stamp}", 2
    with JOBS_LOCK:
        while rid in JOBS or rid in RUNS or os.path.exists(os.path.join(WORK_DIR, rid)):
            rid = f"{domain}_{stamp}_{n}"
            n += 1
    return rid


def save_jobs():
    with JOBS_LOCK:
        data = sorted(JOBS.values(), key=lambda j: j["created"])
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
        os.replace(tmp, JOBS_FILE)


def load_jobs():
    if not os.path.isfile(JOBS_FILE):
        return
    try:
        with open(JOBS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"[queue] could not read {JOBS_FILE}: {exc}", flush=True)
        return
    for job in data:
        if job.get("status") == "running":
            job.update(status="failed", finished=time.time(),
                       error="the maseo service restarted while this job was running")
            model = (job.get("summary") or {}).get("model")
            if model:
                PENDING_RELEASE.add(model)
        JOBS[job["id"]] = job
    save_jobs()
    if PENDING_RELEASE:
        threading.Thread(target=release_pending, daemon=True).start()


def release_pending():
    """Unload the model of every job that has ended and was not released yet
    (and, after a restart, of the jobs that were running). The worker's
    claim calls this first, so a new job never starts while the last one's
    model is still loaded."""
    for name in list(PENDING_RELEASE):
        with RELEASE_LOCK:
            if name not in PENDING_RELEASE:
                continue
            note = release_model(name)
            PENDING_RELEASE.discard(name)
        print("[queue] after restart: " + note, flush=True)
    for run in list(RUNS.values()):
        if run.status in ("finished", "failed", "cancelled") and not getattr(run, "released", False):
            note = run.release_model()
            with JOBS_LOCK:
                if run.id in JOBS and note:
                    JOBS[run.id]["model_release"] = note
                    save_jobs()


def queued_ids():
    return [j["id"] for j in sorted(JOBS.values(), key=lambda j: j["created"])
            if j["status"] == "queued"]


def job_for(job_id):
    job = JOBS.get(job_id)
    if job is None:
        raise LookupError(f"unknown job '{job_id}'")
    return job


def job_submit(body):
    request = {k: body.get(k) for k in REQUEST_KEYS if body.get(k) not in (None, "")}
    if "agents" not in request:
        request["agents"] = body.get("agents") or []
    domain, cqs, source = resolve_cqs(request, allow_dataset=False)
    check_model(request.get("model"))
    request["model"] = str(request.get("model") or "") or default_model()
    request["reasoning"] = reasoning_for(request["model"], request.get("reasoning"))
    agents = agent_list(request.get("agents"))
    job = {"id": new_id(domain), "status": "queued", "created": time.time(),
           "started": None, "finished": None, "request": request,
           "summary": {"domain": domain, "cq_count": len(cqs), "cq_source": source,
                       "model": request["model"],
                       "reasoning": request["reasoning"],
                       "agents": agents},
           "cqs": cqs, "error": "", "error_node": "", "cancel_requested": False}
    with JOBS_LOCK:
        JOBS[job["id"]] = job
        save_jobs()
    kick_worker("new job")
    return {"job_id": job["id"], "position": queued_ids().index(job["id"]) + 1,
            "cq_count": len(cqs), "cq_source": source}


def sync_job(run):
    """Copy a run's end state into its job record."""
    with JOBS_LOCK:
        job = JOBS.get(run.id)
        if job is None:
            return
        job["history"] = list(run.history)
        if run.status == "finished" and run.summary:
            sm = run.summary
            job.update(status="done", finished=time.time(), verdict=sm["verdict"],
                       all_passed=sm["all_passed"], rounds=sm["rounds"],
                       corrections=sm["corrections"], last_checks=sm["last_checks"],
                       files=run.files())
        elif run.status in ("failed", "cancelled") and job["status"] == "running":
            job.update(status="cancelled" if (run.cancel_requested or run.status == "cancelled")
                       else "failed", finished=time.time(), error=job.get("error") or run.error)
        save_jobs()
    if run.status in ("finished", "failed", "cancelled"):
        note = run.release_model()
        with JOBS_LOCK:
            if run.id in JOBS and note:
                JOBS[run.id]["model_release"] = note
                save_jobs()


def job_claim():
    """The n8n worker asks for work: the oldest queued job, unless a job is
    still running (one run at a time - Ollama serves one request at a time)."""
    with JOBS_LOCK:
        check_stalled()
    release_pending()
    with JOBS_LOCK:
        running =[j for j in JOBS.values() if j["status"] == "running"]
        if running:
            return {"job_id": None, "reason": "busy", "running": running[0]["id"]}
        while True:
            ids = queued_ids()
            if not ids:
                return {"job_id": None, "reason": "queue empty"}
            job = JOBS[ids[0]]
            try:
                run = Run(job["request"], run_id=job["id"])
            except Exception as exc:
                job.update(status="failed", started=time.time(), finished=time.time(),
                           error=f"could not start: {exc}", error_node="Start run")
                save_jobs()
                continue
            RUNS[run.id] = run
            job.update(status="running", started=time.time(), history=[])
            save_jobs()
            return {"job_id": job["id"], "run_id": run.id, "next": run.next_node(),
                    "settings": run.settings(), "log": run.start_log}


def job_cancel(job_id):
    with JOBS_LOCK:
        job = job_for(job_id)
        if job["status"] == "queued":
            job.update(status="cancelled", finished=time.time(), error="cancelled before it started")
        elif job["status"] == "running":
            job["cancel_requested"] = True
            run = RUNS.get(job_id)
            if run is not None:
                run.abort()
                if run.current is None:          # between steps: stop right away
                    run.status = "cancelled"
                    job.update(status="cancelled", finished=time.time(),
                               error="cancelled from the web app")
                    threading.Thread(target=sync_job, args=(run,), daemon=True).start()
        save_jobs()
        return job_view(job)


def job_delete(job_id):
    """Remove a finished job (done, failed, cancelled): its queue entry and
    its folder of files. A queued or running job must be cancelled first."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", job_id or "") or job_id in (".", ".."):
        raise ValueError(f"bad job id '{job_id}'")
    with JOBS_LOCK:
        job = job_for(job_id)
        if job["status"] in ("queued", "running"):
            raise PermissionError(f"job {job_id} is {job['status']} - cancel it first")
        del JOBS[job_id]
        RUNS.pop(job_id, None)
        save_jobs()
    folder = os.path.realpath(os.path.join(WORK_DIR, job_id))
    removed = False
    if folder.startswith(os.path.realpath(WORK_DIR) + os.sep) and os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
        removed = not os.path.exists(folder)
    return {"deleted": job_id, "files_removed": removed}


def job_fail(job_id, node, error):
    with JOBS_LOCK:
        job = job_for(job_id)
        if job["status"] in ("queued", "running"):
            run = RUNS.get(job_id)
            cancelled = job.get("cancel_requested") or (run is not None and run.status == "cancelled")
            job.update(status="cancelled" if cancelled else "failed", finished=time.time(),
                       error="cancelled from the web app" if cancelled else str(error or "failed"),
                       error_node=str(node or ""))
            if run is not None:
                if run.status == "running":
                    run.status = "failed"
                job["history"] = list(run.history)
            save_jobs()
        run = RUNS.get(job_id)
    if run is not None:
        job["model_release"] = run.release_model()
        with JOBS_LOCK:
            save_jobs()
    with JOBS_LOCK:
        return job_view(job)


def check_stalled():
    """A running job whose next graph node nobody asked for within
    STALL_SECONDS: the n8n execution was stopped or n8n restarted."""
    now = time.time()
    for job in list(JOBS.values()):
        if job["status"] != "running":
            continue
        run = RUNS.get(job["id"])
        if run is None:
            job.update(status="failed", finished=now, error="the run is gone (service restart)")
        elif run.current is None and now - run.last_activity > STALL_SECONDS:
            run.status = "failed"
            job.update(status="failed", finished=now, history=list(run.history),
                       error=f"stalled: no graph node was requested for {STALL_SECONDS // 60} min "
                             "(the n8n execution was stopped, or n8n restarted)")
            threading.Thread(target=sync_job, args=(run,), daemon=True).start()
        else:
            continue
        save_jobs()


def kick_worker(why, force=False):
    """Start the n8n worker workflow (it claims the next job itself)."""
    if not force and time.time() - _last_kick[0] < 5:
        return
    _last_kick[0] = time.time()

    def go():
        try:
            req = urllib.request.Request(WORKER_URL, data=json.dumps({"why": why}).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as exc:
            print(f"[queue] could not reach the n8n worker at {WORKER_URL}: {exc}", flush=True)
    threading.Thread(target=go, daemon=True).start()


def watchdog():
    while True:
        time.sleep(20)
        try:
            with JOBS_LOCK:
                check_stalled()
                idle = not any(j["status"] == "running" for j in JOBS.values())
                waiting = bool(queued_ids())
            if idle and waiting and time.time() - _last_kick[0] > 60:
                kick_worker("watchdog", force=True)
        except Exception as exc:
            print(f"[queue] watchdog: {exc}", flush=True)


def tail(path, lines=200):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()[-200000:]
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def job_view(job, detail=False):
    sm = job.get("summary") or {}
    agents = sm.get("agents") or []
    run = RUNS.get(job["id"])
    now = time.time()
    view = {"id": job["id"], "status": job["status"], "created": job["created"],
            "started": job.get("started"), "finished": job.get("finished"),
            "domain": sm.get("domain"), "model": sm.get("model"),
            "reasoning": sm.get("reasoning"), "agents": agents,
            "cq_count": sm.get("cq_count"), "cq_source": sm.get("cq_source"),
            "steps": [{"node": n, "label": lab, "on": t is None or t in agents}
                      for n, lab, t in GRAPH_STEPS],
            "tools": [t for t in ("syntax_check", "cq_coverage", "oops_scan",
                                  "hermit_consistency", "themis_test")
                      if t != "themis_test" or "TestGeneration" in agents],
            "error": job.get("error", ""), "error_node": job.get("error_node", ""),
            "cancel_requested": bool(job.get("cancel_requested")),
            "verdict": job.get("verdict"), "all_passed": job.get("all_passed"),
            "rounds": job.get("rounds"), "corrections": job.get("corrections"),
            "last_checks": job.get("last_checks"), "files": job.get("files") or [],
            "history": job.get("history") or [], "current": None, "position": None,
            "model_release": job.get("model_release", "")}
    if job["status"] == "queued":
        ids = queued_ids()
        view["queue_position"] = ids.index(job["id"]) + 1 if job["id"] in ids else None
    if run is not None:
        view["history"] = list(run.history)
        if job["status"] == "running":
            try:
                view["position"] = run.position()
            except Exception:
                view["position"] = None
            if run.current:
                view["current"] = {"node": run.current, "since": run.current_since,
                                   "seconds": round(now - run.current_since, 1),
                                   "tool": getattr(run, "current_tool_name", "")}
                cur = getattr(run, "current_step", None)
                if cur and detail:
                    view["current"]["step"] = cur["no"]
                    view["current"]["files"] = {"prefix": cur["prefix"], "input": list(cur["input"]),
                                                "output": [], "from": dict(cur.get("from") or {})}
    if not detail:     # the queue overview polls often: step files only in the detail
        view["history"] = [{k: v for k, v in h.items() if k != "files"} for h in view["history"]]
    if detail:
        view["cqs"] = job.get("cqs") or []
        folder = os.path.join(WORK_DIR, job["id"])
        view["log"] = tail(os.path.join(folder, "run.log"))
        if os.path.isdir(folder):
            view["files"] = sorted(n for n in os.listdir(folder)
                                   if os.path.isfile(os.path.join(folder, n)))
    return view


def jobs_overview():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
        return {"now": time.time(),
                "running": next((j["id"] for j in jobs if j["status"] == "running"), None),
                "queued": len(queued_ids()),
                "jobs": [job_view(j) for j in jobs[:200]]}


def job_folder(job_id):
    job_for(job_id)
    folder = os.path.join(WORK_DIR, job_id)
    if not os.path.isdir(folder):
        raise LookupError(f"job {job_id} has no files yet")
    return folder


def folder_zip(folder, name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in sorted(os.listdir(folder)):
            if os.path.isfile(os.path.join(folder, n)):
                z.write(os.path.join(folder, n), f"{name}/{n}")
        steps = os.path.join(folder, "steps")      # every step's input/output files
        if os.path.isdir(steps):
            for n in sorted(os.listdir(steps)):
                if os.path.isfile(os.path.join(steps, n)):
                    z.write(os.path.join(steps, n), f"{name}/steps/{n}")
    return buf.getvalue()


def default_model():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return (raw.get("model", {}).get("ollama") or {}).get("id") or "qwen3.6:27b"
    except (OSError, ValueError, AttributeError):
        return "qwen3.6:27b"


def service_info():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    names = ollama_models() or []
    run = raw.get("run", {}) or {}
    return {"ollama_models": names, "models": [model_caps(n) for n in names],
            "default_model": default_model(), "agents": list(TOGGLES),
            "tool_retries": run.get("tool_retries", 3), "loop_rounds": run.get("loop_rounds", 3),
            "mcp_tools": TOOLS.names()}


# ------------------------------------------------------------------ HTTP ---
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.__stderr__.write("%s %s\n" % (self.address_string(), fmt % args))

    def send(self, code, body, ctype="application/json", filename=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return json.loads(raw) if raw.strip() else {}

    def run_for(self, run_id):
        run = RUNS.get(run_id)
        if run is None:
            raise LookupError(f"unknown run '{run_id}' (the service was restarted, or the "
                              "id is wrong) - start again from the form")
        return run

    def fail(self, exc):
        code = (404 if isinstance(exc, LookupError) else
                409 if isinstance(exc, (PermissionError, Cancelled)) else
                400 if isinstance(exc, (ValueError, KeyError)) else 500)
        self.send(code, {"error": f"{exc.__class__.__name__}: {exc}"})

    def do_GET(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        try:
            if not parts:
                return self.send(200, {
                    "service": "maseo atomic for n8n", "maseo_atomic": MASEO_ATOMIC,
                    "work_dir": WORK_DIR, "ollama": OLLAMA_HOST,
                    "ollama_models": ollama_models(), "mcp_tools": TOOLS.names(),
                    "runs": len(RUNS), "jobs": len(JOBS), "worker_url": WORKER_URL})
            if parts == ["info"]:
                return self.send(200, service_info())
            if parts == ["jobs"]:
                return self.send(200, jobs_overview())
            if len(parts) == 2 and parts[0] == "jobs":
                with JOBS_LOCK:
                    return self.send(200, job_view(job_for(parts[1]), detail=True))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "zip":
                folder = job_folder(parts[1])
                return self.send(200, folder_zip(folder, parts[1]), "application/zip",
                                 f"{parts[1]}.zip")
            if len(parts) == 4 and parts[0] in ("jobs", "runs") and parts[2] == "file":
                folder = job_folder(parts[1]) if parts[0] == "jobs" else self.run_for(parts[1]).dir
                name = os.path.basename(urllib.parse.unquote(parts[3]))
                path = os.path.join(folder, name)
                if not os.path.isfile(path):          # a step's input/output file
                    path = os.path.join(folder, "steps", name)
                if not name or not os.path.isfile(path):
                    raise LookupError(f"no file '{name}' in {parts[1]}")
                ctype = ("application/rdf+xml" if name.endswith(".owl") else
                         "application/json" if name.endswith(".json") else
                         "text/plain; charset=utf-8")
                with open(path, "rb") as f:
                    return self.send(200, f.read(), ctype, name)
            if parts == ["runs"]:
                return self.send(200, [r.status_view() for r in RUNS.values()])
            if len(parts) == 2 and parts[0] == "runs":
                return self.send(200, self.run_for(parts[1]).status_view())
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "zip":
                run = self.run_for(parts[1])
                return self.send(200, run.zip_bytes(), "application/zip", f"{run.id}.zip")
            self.send(404, {"error": "unknown path"})
        except Exception as exc:
            self.fail(exc)

    def do_POST(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        try:
            body = self.body()
            if parts == ["jobs"]:
                return self.send(200, job_submit(body))
            if parts == ["queue", "claim"]:
                return self.send(200, job_claim())
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                return self.send(200, job_cancel(parts[1]))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "delete":
                return self.send(200, job_delete(parts[1]))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "fail":
                return self.send(200, job_fail(parts[1], body.get("node"), body.get("error")))
            if parts == ["runs"]:
                check_model(body.get("model"))
                run = Run(body)
                with RUNS_LOCK:
                    RUNS[run.id] = run
                return self.send(200, {"run_id": run.id, "next": run.next_node(),
                                       "settings": run.settings(),
                                       "cqs": run.cqs, "log": run.start_log})
            if len(parts) == 4 and parts[0] == "runs" and parts[2] == "node":
                run = self.run_for(parts[1])
                try:
                    return self.send(200, run.run_node(parts[3],
                                                       str(body.get("expect_tool") or "")))
                finally:
                    if run.status != "running":
                        sync_job(run)
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "finish":
                run = self.run_for(parts[1])
                summary = run.finish()
                sync_job(run)
                return self.send(200, summary)
            self.send(404, {"error": "unknown path"})
        except Exception as exc:
            self.fail(exc)


def main():
    os.makedirs(WORK_DIR, exist_ok=True)
    load_jobs()
    threading.Thread(target=watchdog, daemon=True, name="queue-watchdog").start()
    # open the port first, so the web app and n8n can reach the service while
    # the five MCP tool servers (Java for HermiT, ...) are still starting
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"maseo atomic service on http://0.0.0.0:{PORT}\n  maseo_atomic: {MASEO_ATOMIC}\n"
          f"  work dir: {WORK_DIR}\n  ollama: {OLLAMA_HOST}\n"
          f"  n8n worker: {WORKER_URL}  ({len(queued_ids())} job(s) queued)", flush=True)

    def warm_up():
        try:
            print("  MCP tools ready: " + ", ".join(TOOLS.names()), flush=True)
        except Exception as exc:
            print(f"  MCP tools not ready: {exc}", flush=True)
        if queued_ids():
            kick_worker("service start", force=True)

    threading.Thread(target=warm_up, daemon=True, name="mcp-warm-up").start()
    server.serve_forever()


if __name__ == "__main__":
    main()
