import json
import os
import re
from typing import Any, Dict, List, Optional, Union

import yaml
from langchain_core.messages import HumanMessage, SystemMessage

# Supported LLM providers (only these three).
PROVIDER_DEFAULTS = {
    # OpenRouter and DeepSeek are OpenAI-compatible (used via langchain_openai).
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",
                   "key_env": "OPENROUTER_API_KEY"},
    "deepseek":   {"base_url": "https://api.deepseek.com",
                   "key_env": "DEEPSEEK_API_KEY"},
    # Ollama runs locally (used via langchain_ollama); no API key.
    "ollama":     {"base_url": "http://localhost:11434", "key_env": None},
}

# Fallback prompts, used only when config.yaml does not define them.
DEFAULT_EXTRACTION_PROMPT = (
    "You are an ontology requirements analyst. For EACH competency question, "
    "identify the ontology terms needed to answer it and the Themis "
    "verification tests that check it is answerable. Return ONLY a JSON "
    "object mapping every CQ id to its terms: "
    '{"CQ1": {"classes": [...], "object_properties": [...], '
    '"data_properties": [...], "individuals": [...], "axioms": [...], '
    '"tests": [...]}}. '
    "Classes and individuals in CamelCase, properties in camelCase; axioms "
    "are short natural-language constraints; individuals only when the "
    "question needs instance-level data. Tests use the official Themis "
    "catalogue (themis.linkeddata.es/tests-info.html: 'ClassA type Class', "
    "'ClassA SubClassOf propP some ClassB', 'ClassA propP ClassB', 'propP "
    "domain ClassA', ...); every non-keyword token MUST be a term listed "
    "for the same CQ (identical spelling, never invent names) and the tests "
    "must capture the semantic meaning of the question, not just term "
    "existence. For DATA properties use only 'ClassA SubClassOf propP some "
    "<datatype>' (never domain/range) with datatypes limited to string, "
    "integer, float, double, long, boolean, dateTime, dateTimeStamp, anyURI "
    "(no xsd: prefix). Write 1-4 per CQ including at least one relation "
    "test. No markdown, no commentary."
)
DEFAULT_GENERATION_PROMPT = (
    "You are an experienced knowledge engineer. From the provided competency "
    "questions, produce a complete OWL ontology as RDF/XML (classes, "
    "properties, domains, ranges, axioms). Append local names directly to the "
    "base URI {base_uri}. On every entity add rdfs:label, rdfs:comment and a "
    "dc:source listing each competency question it serves as "
    "'(competency_question) <id>'. Return ONLY the RDF/XML."
)
DEFAULT_CORRECTION_PROMPT = (
    "You are an experienced ontology engineer. Update the RDF/XML ontology to "
    "resolve the reported issues. Preserve every entity and its provenance "
    "(rdfs:label, rdfs:comment, dc:source); keep URIs anchored to the base "
    "URI. For every uncovered competency question, add what is needed and "
    "record its id in dc:source as '(competency_question) <id>'. "
    "Return ONLY the complete corrected RDF/XML."
)


HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_FILE = os.path.join(HERE, "agent.md")


def load_spec(path: str = SPEC_FILE) -> str:
    """agent.md = the agents' SKILL.md-style output contract. Return its body
    with the YAML frontmatter stripped ('' if the file is missing)."""
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return ""
    m = re.match(r"\s*---\n.*?\n---\n", text, re.S)
    return (text[m.end():] if m else text).strip()


# ---- config -------------------------------------------------------------
def _expand_env(value: Any) -> Any:
    """Expand ${VAR} references inside string config values."""
    if isinstance(value, str):
        return re.sub(r"\$\{([^}]+)\}",
                      lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(config_path: str) -> Dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return _expand_env(raw)


# ---- LLM ----------------------------------------------------------------
def build_llm(llm: Dict[str, Any]):
    """Build the chat model from config.yaml's ``llm`` section.
    Returns (llm, resolved_cfg)."""
    provider = (llm.get("provider") or "openrouter").lower()
    if provider not in PROVIDER_DEFAULTS:
        raise ValueError(f"Unsupported llm.provider '{provider}'. "
                         f"Choose one of: {', '.join(PROVIDER_DEFAULTS)}.")
    d = PROVIDER_DEFAULTS[provider]
    cfg = {"provider": provider,
           "model": llm.get("model") or "",
           "base_url": llm.get("base_url") or d["base_url"],
           "api_key": llm.get("api_key")
                      or (os.environ.get(d["key_env"], "") if d["key_env"] else ""),
           "temperature": llm.get("temperature", 0.0),
           "timeout": llm.get("timeout", 300),
           "retries": int(llm.get("retries", 3)),
           "retry_backoff": float(llm.get("retry_backoff", 10)),
           "extra_headers": llm.get("extra_headers")}
    if not cfg["model"]:
        raise ValueError(f"No model set for provider '{provider}'. "
                         "Set llm.model in config.yaml.")
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=cfg["model"], base_url=cfg["base_url"],
                          temperature=cfg["temperature"]), cfg
    from langchain_openai import ChatOpenAI
    kwargs = dict(model=cfg["model"], base_url=cfg["base_url"],
                  api_key=cfg["api_key"] or "not-needed",
                  temperature=cfg["temperature"], timeout=cfg["timeout"])
    if cfg["extra_headers"]:
        kwargs["default_headers"] = cfg["extra_headers"]
    return ChatOpenAI(**kwargs), cfg


# ---- shared helpers -------------------------------------------------------
def extract_rdfxml(text: str) -> str:
    """Strip fences/commentary; keep only the RDF/XML document."""
    if not text:
        return ""
    t = text.strip()
    fence = re.search(r"```(?:xml|rdf|owl)?\s*(.*?)```", t, re.S | re.I)
    if fence:
        t = fence.group(1).strip()
    start = None
    for marker in ("<?xml", "<rdf:RDF", "<RDF"):
        idx = t.find(marker)
        if idx != -1:
            start = idx if start is None else min(start, idx)
    if start is not None:
        t = t[start:]
    end = t.rfind("</rdf:RDF>")
    if end == -1:
        end = t.rfind("</RDF>")
    if end != -1:
        close = t.find(">", end)
        t = t[:close + 1] if close != -1 else t
    return t.strip()


def load_cqs(cqs: Union[str, List[Any]]) -> List[Dict[str, str]]:
    """Accept a path, a JSON string, or a list; normalize to [{'id','value'}]."""
    if isinstance(cqs, str):
        if os.path.isfile(cqs):
            with open(cqs, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.loads(cqs)
    else:
        data = cqs
    out = []
    for item in data:
        if isinstance(item, dict):
            out.append({"id": str(item.get("id") or item.get("cq_id") or ""),
                        "value": str(item.get("value") or item.get("text") or "")})
        elif isinstance(item, str):
            out.append({"id": item, "value": ""})
    return out


def format_cqs(cqs: List[Dict[str, str]]) -> str:
    return "\n".join(f"{c['id']}: {c['value']}".rstrip(": ") for c in cqs)


def extract_json(text: str) -> Dict[str, Any]:
    """Parse the first JSON object in an LLM reply (handles ``` fences)."""
    if not text:
        return {}
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S | re.I)
    if fence:
        t = fence.group(1).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(t[start:end + 1])
    except ValueError:
        return {}


def format_terms(cqs: List[Dict[str, str]], terms: Dict[str, Any]) -> str:
    """One block per CQ: the question plus its mapped terms."""
    lines = []
    for c in cqs:
        t = terms.get(c["id"], {}) or {}
        parts = [f"{k}: {', '.join(map(str, v))}" for k, v in t.items() if v]
        lines.append(f"{c['id']}: {c['value']}\n    "
                     + ("; ".join(parts) if parts else "(no terms mapped)"))
    return "\n".join(lines)


class ExtractionAgent:
    """Maps each competency question to the ontology terms (classes,
    properties, individuals, axioms) needed to answer it. Returns
    {cq_id: {category: [terms]}}. No RDF contract — this agent emits JSON."""

    def __init__(self, llm, base_uri: str, system_prompt: Optional[str] = None):
        self.llm = llm
        self.base_uri = base_uri if base_uri.endswith("#") else base_uri + "#"
        self.system = system_prompt or DEFAULT_EXTRACTION_PROMPT

    async def run(self, cqs: Union[str, List[Any]]) -> Dict[str, Any]:
        cq_list = load_cqs(cqs)
        user = ("Competency questions:\n" + format_cqs(cq_list)
                + "\n\nReturn the JSON mapping from EVERY CQ id to its terms.")
        msg = await self.llm.ainvoke(
            [SystemMessage(content=self.system.replace("{base_uri}", self.base_uri)),
             HumanMessage(content=user)])
        terms = extract_json(msg.content)
        return {c["id"]: terms.get(c["id"], {}) for c in cq_list}

class GenerationAgent:
    """Drafts an OWL ontology (RDF/XML) from competency questions."""

    def __init__(self, llm, base_uri: str, system_prompt: Optional[str] = None,
                 spec: Optional[str] = None):
        self.llm = llm
        self.base_uri = base_uri if base_uri.endswith("#") else base_uri + "#"
        spec = load_spec() if spec is None else spec
        self.system = (system_prompt or DEFAULT_GENERATION_PROMPT) \
            + (("\n\n" + spec) if spec else "")

    async def run(self, cqs: Union[str, List[Any]],
                  terms: Optional[Dict[str, Any]] = None) -> str:
        system = self.system.replace("{base_uri}", self.base_uri)
        cq_list = load_cqs(cqs)
        body = (("Competency questions with the terms each one requires "
                 "(model EVERY listed term under its mapped name):\n"
                 + format_terms(cq_list, terms))
                if terms else
                "Competency questions:\n" + format_cqs(cq_list))
        user = (f"Base URI: {self.base_uri}\n\n" + body
                + "\n\nProduce the RDF/XML OWL ontology that answers all of them.")
        msg = await self.llm.ainvoke([SystemMessage(content=system),
                                      HumanMessage(content=user)])
        return extract_rdfxml(msg.content)


class CorrectionAgent:
    """Corrects an OWL ontology (RDF/XML) given one tool report."""

    def __init__(self, llm, base_uri: str, system_prompt: Optional[str] = None,
                 spec: Optional[str] = None):
        self.llm = llm
        self.base_uri = base_uri if base_uri.endswith("#") else base_uri + "#"
        spec = load_spec() if spec is None else spec
        self.system = (system_prompt or DEFAULT_CORRECTION_PROMPT) \
            + (("\n\n" + spec) if spec else "")

    async def run(self, ontology: str, report: str, kind: str = "issues",
                  uncovered_cqs: Optional[List[Dict[str, str]]] = None) -> str:
        system = self.system.replace("{base_uri}", self.base_uri)
        parts = [f"Base URI: {self.base_uri}\n",
                 "Ontology (RDF/XML):\n" + ontology.strip(),
                 f"\n=== {kind} to fix ===\n" + (report or "").strip()]
        if uncovered_cqs:
            lines = []
            for c in uncovered_cqs:
                lines.append(f"{c['id']}: {c['value']}")
                if c.get("missing"):
                    lines.append("    MISSING TERMS: " + ", ".join(c["missing"]))
            parts.append("\nThese competency questions are NOT yet captured by the "
                         "ontology. Add every MISSING TERM under exactly that name "
                         "(as a class, property or individual, as appropriate), make "
                         "it answer its question, and record the CQ id in the "
                         "entity's dc:source as '(competency_question) <id>':\n"
                         + "\n".join(lines))
        parts.append("\nReturn the full corrected RDF/XML only.")
        msg = await self.llm.ainvoke([SystemMessage(content=system),
                                      HumanMessage(content="\n".join(parts))])
        return extract_rdfxml(msg.content) or ontology


if __name__ == "__main__":
    print("agent.py holds the two agents (generation + correction). Run:\n"
          "    python mcp_client.py <domain>    (e.g. python mcp_client.py swo)")
