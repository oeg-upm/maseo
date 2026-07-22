import argparse
import asyncio
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, TypedDict

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.graph import END, StateGraph

from agent import (CorrectionAgent, ExtractionAgent, GenerationAgent,
                   build_llm, load_config, load_cqs)

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")


def _connections() -> dict:
    """stdio transport: run mcp_server.py as a subprocess (no HTTP)."""
    return {"maseo": {"command": sys.executable, "args": [SERVER],
                      "transport": "stdio"}}


def _parse_tool_result(res: Any) -> Dict[str, Any]:
    if isinstance(res, list):
        for block in res:
            text = block.get("text") if isinstance(block, dict) \
                else getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except (ValueError, TypeError):
                    return {"raw": text}
    if isinstance(res, dict):
        return res
    if isinstance(res, str):
        try:
            return json.loads(res)
        except (ValueError, TypeError):
            return {"raw": res}
    return {"raw": str(res)}


def _summary(tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    s: Dict[str, Any] = {"passed": bool(result.get("passed"))}
    if tool in ("cq_coverage", "themis_test"):
        s["covered"] = result.get("covered", [])
        s["uncovered"] = result.get("uncovered", [])
        s["missing"] = result.get("missing", {})
    elif tool == "oops_scan":
        pitfalls = result.get("pitfalls")
        if pitfalls is None:  # older server without structured pitfalls:
            pitfalls = [l.strip() for l in       # recover them from the report
                        (result.get("report") or "").splitlines()
                        if l.strip().startswith("[")]
        s["major_count"] = result.get("major_count")
        s["pitfalls"] = pitfalls
    elif tool == "hermit_consistency":
        s["consistent"] = result.get("consistent")
        s["unsatisfiable_classes"] = result.get("unsatisfiable_classes", [])
        if not s["passed"]:
            s["error"] = (result.get("report") or "").split("\n", 1)[0]
    elif tool == "syntax_check":
        s["errors"] = result.get("errors", [])
    return s


def _terms_doc(cqs: List[Dict[str, str]],
               terms: Dict[str, Any]) -> List[Dict[str, Any]]:
    def cat(t: Any, key: str) -> List[str]:
        v = t.get(key) if isinstance(t, dict) else None
        return v if isinstance(v, list) else ([] if v in (None, "") else [str(v)])
    doc = []
    for c in cqs:
        t = terms.get(c["id"]) or {}
        obj, dat = cat(t, "object_properties"), cat(t, "data_properties")
        doc.append({"id": c["id"],
                    "question": c["value"],
                    "classes": cat(t, "classes"),
                    "properties": obj + dat,
                    "data_properties": dat,
                    "object_properties": obj,
                    "axioms": cat(t, "axioms"),
                    "individuals": cat(t, "individuals"),
                    "tests": cat(t, "tests")})
    return doc


_THEMIS_KEYWORDS = {"type", "Class", "Property", "SubClassOf", "subClassOf",
                    "subclassOf", "disjointWith", "equivalentTo", "domain",
                    "range", "some", "only", "min", "max", "exactly", "and",
                    "or", "that", "characteristic", "symmetricProperty",
                    "string", "integer", "float", "double", "long", "boolean",
                    "dateTime", "dateTimeStamp", "anyURI", "rational",
                    "Literal"}


_THEMIS_DTYPES = {"string": "string", "integer": "integer", "float": "float",
                  "double": "double", "long": "long", "boolean": "boolean",
                  "datetime": "dateTime", "datetimestamp": "dateTimeStamp",
                  "anyuri": "anyURI", "rational": "rational",
                  "literal": "Literal"}

_DTYPE_FIX = {"decimal": "double", "date": "dateTime", "duration": "dateTime",
              "gyear": "dateTime", "gyearmonth": "dateTime",
              "time": "dateTime", "int": "integer", "short": "integer",
              "byte": "integer", "nonnegativeinteger": "integer",
              "positiveinteger": "integer", "negativeinteger": "integer",
              "unsignedint": "integer", "unsignedlong": "long"}


_THEMIS_GRAMMAR = [re.compile(p, re.I) for p in (
    r"^\S+ type \S+$",
    r"^\S+ subclassof \S+$",
    r"^\S+ subclassof \S+ and \S+$",
    r"^\S+ (?:domain|range) \S+$",
    r"^\S+ \S+ \S+$",               
    r"^\S+ subclassof \S+ (?:some|only) \S+$",
    r"^\S+ subclassof \S+ only \S+ or \S+$",
    r"^\S+ subclassof \S+ (?:min|max|exactly) \d+ \S+$",
    r"^\S+ disjointwith \S+$",
    r"^\S+ equivalentto \S+$",
    r"^\S+ characteristic symmetricproperty$",
    r"^\S+ subclassof \S+ that \S+ some \S+$",
    r"^\S+ and \S+ subclassof \S+ (?:some|only) \S+$",
    r"^\S+ subclassof \S+ min \d+ \S+ and \S+ subclassof \S+ (?:some|only) \S+$",
    r"^\S+ subclassof \S+ and \S+ subclassof \S+ that disjointwith \S+$",
)]


def _vtc_ttl(cqs: List[Dict[str, str]], terms: Dict[str, Any],
             base_uri: str) -> str:
    esc = lambda s: str(s).replace("\\", "\\\\").replace('"', '\\"')
    base = base_uri[:-1] if base_uri.endswith("#") else base_uri
    lines = [
        "@prefix vtc: <https://w3id.org/def/vtc#> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "@prefix dcterms: <http://purl.org/dc/terms/> .",
        f"@prefix : <{base}/tests#> .",
        "",
        ":suite rdf:type owl:NamedIndividual , vtc:TestSuite .",
        ""]
    for c in cqs:
        cid = str(c.get("id"))
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", cid)
        t = terms.get(cid) or {}
        tests = t.get("tests") if isinstance(t, dict) else None
        lines += [
            f":{safe} rdf:type owl:NamedIndividual , vtc:Requirement ;",
            f'    vtc:requirementId "{esc(cid)}"^^xsd:string ;',
            '    vtc:category "competency question"^^xsd:string ;',
            f'    dcterms:description "{esc(c.get("value", ""))}"'
            '^^xsd:string .',
            ""]
        for i, test in enumerate(tests or [], 1):
            lines += [
                f":{safe}_test{i} rdf:type owl:NamedIndividual , "
                "vtc:TestCaseDesign ;",
                "    vtc:belongsTo :suite ;",
                f"    vtc:comesFromRequirement :{safe} ;",
                f'    vtc:isRelatedToRequirement "{esc(base_uri + cid)}"'
                "^^xsd:anyURI ;",
                f'    vtc:desiredBehaviour "{esc(test)}"^^xsd:string .',
                ""]
    return "\n".join(lines)


def _harmonize_suite(cqs: List[Dict[str, str]],
                     terms: Dict[str, Any]) -> Dict[str, List[str]]:
    rel_kw = {"type", "subclassof", "domain", "range", "disjointwith",
              "equivalentto", "characteristic"}
    indiv_class: Dict[str, str] = {}   
    individuals: set = set()           
    class_terms: set = set()
    all_tests: List[str] = []
    for c in cqs:
        t = terms.get(str(c.get("id")))
        if isinstance(t, dict):
            if isinstance(t.get("tests"), list):
                all_tests.extend(str(x) for x in t["tests"])
            if isinstance(t.get("classes"), list):
                class_terms.update(str(x) for x in t["classes"])
            if isinstance(t.get("individuals"), list):
                individuals.update(str(x) for x in t["individuals"])
    for test in all_tests:
        toks = test.split()
        if len(toks) == 3 and toks[1] == "type":
            if toks[2] == "Class":
                class_terms.add(toks[0])
            elif toks[2] != "Property":
                indiv_class[toks[0]] = toks[2]
                individuals.add(toks[0])
        elif len(toks) >= 3 and toks[1].lower() == "subclassof":
            class_terms.add(toks[0])
            if len(toks) == 3:
                class_terms.add(toks[2])

    changes: Dict[str, List[str]] = {}
    for c in cqs:
        cid = str(c.get("id"))
        t = terms.get(cid)
        if not (isinstance(t, dict) and isinstance(t.get("tests"), list)):
            continue
        new_tests, ch = [], []
        for test in (str(x) for x in t["tests"]):
            toks = test.split()
            new = test
            if len(toks) == 3 and toks[1] == "type" \
                    and toks[2] not in ("Class", "Property") \
                    and toks[0] in class_terms:
                new = f"{toks[0]} SubClassOf {toks[2]}"
                ch.append(f"class/individual clash (class reading wins): "
                          f"'{test}' -> '{new}'")
            elif len(toks) == 3 and toks[1].lower() not in rel_kw:
                s, p, o = toks
                dropped = False
                for pos, x in (("object", o), ("subject", s)):
                    if x in class_terms or x not in individuals:
                        continue
                    cls = indiv_class.get(x)
                    if cls:
                        prev = new
                        s2, p2, o2 = new.split()
                        new = (f"{s2} {p2} {cls}" if pos == "object"
                               else f"{cls} {p2} {o2}")
                        ch.append(f"individual '{x}' in class position: "
                                  f"'{prev}' -> '{new}'")
                    else:
                        ch.append(f"dropped (individual '{x}' has no known "
                                  f"class; the relation test would demand "
                                  f"it be a class -> Incorrect forever): "
                                  f"'{new}'")
                        dropped = True
                        break
                if dropped:
                    continue
            if new not in new_tests:
                new_tests.append(new)
        t["tests"] = new_tests
        if ch:
            changes[cid] = ch
    return changes


def _sanitize_tests(tests: List[str], terms: Any) -> tuple:
    """Turn the generated tests into a SATISFIABLE gold standard. Two Themis
    limitations (Implementations.java) make some LLM-written tests unfixable
    by ANY ontology edit: (1) 'prop domain/range X' is checked by asserting
    an OBJECT property, so on a data property the verdict is 'Absent'
    forever; (2) datatypes outside _THEMIS_DTYPES are matched as ontology
    terms -> 'Undefined' forever. Normalize datatype tokens and rewrite
    data-property domain/range tests to the supported pattern
    'Class SubClassOf prop some datatype'. Returns (clean_tests, changes)."""
    t = terms if isinstance(terms, dict) else {}

    def listy(key):
        v = t.get(key)
        return [str(x) for x in v] if isinstance(v, list) else []
    dprops = set(listy("data_properties"))
    classes = listy("classes")
    names = {str(x) for k, v in t.items()
             if k not in ("axioms", "tests") and isinstance(v, list)
             for x in v}

    def force_dtype(tok: str) -> str:
        """Canonical supported datatype for a datatype-position token."""
        base = tok.lower()
        base = base[4:] if base.startswith("xsd:") else base
        return _DTYPE_FIX.get(base) or _THEMIS_DTYPES.get(base) or "string"

    def fix_dtype(tok: str) -> str:
        if tok in names:
            return tok
        base = tok.lower()
        base = base[4:] if base.startswith("xsd:") else base
        if base in _DTYPE_FIX:
            return _DTYPE_FIX[base]
        if base in _THEMIS_DTYPES:
            return _THEMIS_DTYPES[base]
        return tok

    normalized, changes = [], []
    for test in tests:
        new = " ".join(fix_dtype(tok) for tok in str(test).split())
        if new != " ".join(str(test).split()):
            changes.append(f"datatype normalized: '{test}' -> '{new}'")
        normalized.append(new)

    dom_of, rng_of = {}, {}
    for test in normalized:
        toks = test.split()
        if len(toks) == 3 and toks[1] == "domain":
            dom_of[toks[0]] = toks[2]
        elif len(toks) == 3 and toks[1] == "range":
            rng_of[toks[0]] = toks[2]

    clean = []
    for test in normalized:
        toks = test.split()
        if len(toks) == 3 and toks[1] in ("domain", "range") \
                and toks[0] in dprops:
            p = toks[0]
            if toks[1] == "domain":
                cls, dt = toks[2], force_dtype(rng_of.get(p, "string"))
            else:
                cls = dom_of.get(p) or (classes[0] if classes else None)
                dt = force_dtype(toks[2])
            if not cls:
                changes.append(f"dropped (no domain class known for data "
                               f"property '{p}'): '{test}'")
                continue
            new = f"{cls} SubClassOf {p} some {dt}"
            changes.append(f"data-property {toks[1]} test rewritten: "
                           f"'{test}' -> '{new}'")
            test = new
        toks = test.split()
        if len(toks) == 5 and toks[1].lower() == "subclassof" \
                and toks[3].lower() == "some":
            new = f"{toks[0]} {toks[2]} {toks[4]}"
            changes.append(f"existential test rewritten (a declared "
                           f"rdfs:range makes it unexecutable in Themis): "
                           f"'{test}' -> '{new}'")
            test = new
        if not any(g.match(test) for g in _THEMIS_GRAMMAR):
            changes.append(f"dropped (unsupported Themis syntax, would "
                           f"truncate the whole batch): '{test}'")
            continue
        if test not in clean:
            clean.append(test)
    return clean, changes


def _test_schema_warnings(tests: List[str], terms: Any) -> List[str]:
    names = {str(x) for k, v in (terms or {}).items()
             if k not in ("axioms", "tests") and isinstance(v, list)
             for x in v}
    warns = []
    for test in tests:
        bad = [tok for tok in str(test).split()
               if tok not in _THEMIS_KEYWORDS and not tok.isdigit()
               and tok not in names]
        if bad:
            warns.append(f"{test}  <- not in this CQ's term mapping: "
                         + ", ".join(bad))
    return warns


def _effect(tool: str, before: Dict[str, Any],
            after: Dict[str, Any]) -> Dict[str, Any]:
    if tool in ("cq_coverage", "themis_test"):
        b, a = before.get("covered") or [], after.get("covered") or []
        return {"covered_before": b, "covered_after": a,
                "added_cqs": [c for c in a if c not in b],
                "removed_cqs": [c for c in b if c not in a]}
    if tool == "oops_scan":
        def codes(r):
            return [p.get("code", "?") if isinstance(p, dict) else str(p)
                    for p in r.get("pitfalls") or []]
        b, a = codes(before), codes(after)
        return {"pitfalls_before": before.get("major_count"),
                "pitfalls_after": after.get("major_count"),
                "pitfall_codes_before": b, "pitfall_codes_after": a,
                "fixed_pitfalls": [c for c in b if c not in a],
                "introduced_pitfalls": [c for c in a if c not in b]}
    if tool == "hermit_consistency":
        b = before.get("unsatisfiable_classes") or []
        a = after.get("unsatisfiable_classes") or []
        return {"consistent_before": before.get("consistent"),
                "consistent_after": after.get("consistent"),
                "unsatisfiable_before": b, "unsatisfiable_after": a,
                "fixed_classes": [c for c in b if c not in a],
                "introduced_classes": [c for c in a if c not in b]}
    if tool == "syntax_check":
        b, a = before.get("errors") or [], after.get("errors") or []
        return {"errors_before": b, "errors_after": a,
                "fixed_errors": [e for e in b if e not in a],
                "introduced_errors": [e for e in a if e not in b]}
    return {}


class Tracer:

    def __init__(self, path: str, enabled: bool = True, console: bool = True,
                 truncate: int = 600):
        self.enabled, self.console, self.truncate = enabled, console, truncate
        self.t0 = time.time()
        self.path = path
        self.f = open(path, "w", encoding="utf-8") if enabled else None

    def log(self, event: str, **data) -> None:
        if not self.enabled:
            return
        ts = round(time.time() - self.t0, 2)
        self.f.write(json.dumps({"ts": ts, "event": event, **data},
                                ensure_ascii=False, default=str) + "\n")
        self.f.flush()
        if self.console:
            parts = []
            for k, v in data.items():
                s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False,
                                                            default=str)
                s = s.replace("\n", " ")
                if len(s) > self.truncate:
                    s = s[:self.truncate] + f"...(+{len(s) - self.truncate} chars)"
                parts.append(f"{k}={s}")
            print(f"[{ts:>7.1f}s] {event:<13} " + "  ".join(parts))

    def close(self) -> None:
        if self.f:
            self.f.close()


class TracedLLM:

    def __init__(self, llm, tracer: Tracer, agent: str,
                 retries: int = 3, backoff: float = 10.0):
        self.llm, self.tracer, self.agent = llm, tracer, agent
        self.retries, self.backoff = retries, backoff

    async def ainvoke(self, messages):
        self.tracer.log("llm_call", agent=self.agent,
                        messages=[{"role": m.type, "content": m.content}
                                  for m in messages])
        t = time.time()
        for attempt in range(self.retries + 1):
            try:
                msg = await self.llm.ainvoke(messages)
                break
            except Exception as e:
                if attempt == self.retries:
                    raise
                wait = self.backoff * (attempt + 1)
                self.tracer.log("llm_retry", agent=self.agent,
                                attempt=attempt + 1, retries=self.retries,
                                error=f"{type(e).__name__}: {e}",
                                wait_seconds=wait)
                await asyncio.sleep(wait)
        self.tracer.log("llm_response", agent=self.agent,
                        seconds=round(time.time() - t, 2),
                        usage=getattr(msg, "usage_metadata", None),
                        content=msg.content)
        return msg


class TracedTool:

    def __init__(self, tool, tracer: Tracer):
        self.tool, self.tracer, self.name = tool, tracer, tool.name

    async def ainvoke(self, args):
        self.tracer.log("tool_call", tool=self.name, args=args)
        t = time.time()
        res = await self.tool.ainvoke(args)
        self.tracer.log("tool_result", tool=self.name,
                        seconds=round(time.time() - t, 2),
                        result=_parse_tool_result(res))
        return res


class AgentState(TypedDict, total=False):
    cqs: List[Dict[str, str]]
    terms: Dict[str, Any]
    ontology: str
    round: int
    syntax: Dict[str, Any]
    oops: Dict[str, Any]
    hermit: Dict[str, Any]
    coverage: Dict[str, Any]
    themis: Dict[str, Any]
    all_passed: bool


class MASEORun:

    def __init__(self, config_path: str, domain: str):
        cfg = load_config(config_path)
        cfg_dir = os.path.dirname(os.path.abspath(config_path))
        self.domain = domain

        # dataset -> CQ file
        ds = cfg.get("dataset", {}) or {}
        cq_dir = ds.get("cq_dir", "./dataset")
        if not os.path.isabs(cq_dir):
            cq_dir = os.path.normpath(os.path.join(cfg_dir, cq_dir))
        pattern = ds.get("file_pattern", "{domain}_cq2onto_cqs.json")
        self.cq_file = os.path.join(cq_dir, pattern.format(domain=domain))
        if not os.path.isfile(self.cq_file):
            raise FileNotFoundError(
                f"No competency-question file for domain '{domain}': {self.cq_file}\n"
                f"Available: "
                + ", ".join(sorted(os.listdir(cq_dir))
                            if os.path.isdir(cq_dir) else ["<cq_dir missing>"]))

        # ontology + run settings
        onto = cfg.get("ontology", {}) or {}
        base_uri = onto.get("base_uri_template",
                            "http://www.semanticweb.org/{domain}#").format(domain=domain)
        self.base_uri = base_uri if base_uri.endswith("#") else base_uri + "#"
        run = cfg.get("run", {}) or {}
        # ONE hyperparameter for every loop (inner fix loops AND outer rounds).
        self.max_attempts = int(run.get("max_attempts", 3))
        out = run.get("output_dir", "outputs").replace("{domain}", domain)
        # normpath merges the configured dir (which may use / separators)
        # with the platform's - no more mixed "outputs/wine\file" paths
        self.output_dir = os.path.normpath(
            out if os.path.isabs(out) else os.path.join(HERE, out))
        os.makedirs(self.output_dir, exist_ok=True)
        self.onto_path = os.path.join(self.output_dir, f"{domain}_ontology.owl")
        self.terms_path = os.path.join(self.output_dir, f"{domain}_terms.json")
        # Themis test log: every generated test and every execution's per-test
        # verdicts, written to <domain>_tests.json after each event.
        self.tests_path = os.path.join(self.output_dir, f"{domain}_tests.json")
        # Gold test suite as Turtle following the VTC ontology
        # (https://w3id.org/def/vtc#) - written once at extraction.
        self.suite_path = os.path.join(self.output_dir,
                                       f"{domain}_testsuite.ttl")
        self._testlog: Dict[str, Any] = {"domain": domain, "generated": {},
                                         "executions": [], "test_history": {}}
        # Minimal step log (<domain>_steps.json): one JSON entry per step -
        # {"step": agent and/or tool, "prompt_information": what it was
        #  given, "ontology": the full OWL source at that point}.
        self.steps_path = os.path.join(self.output_dir, f"{domain}_steps.json")
        self._steps: List[Dict[str, Any]] = []
        # Performance log: one record per agent/tool invocation, in order.
        # Written to <domain>_run.json as `records`.
        self.records: List[Dict[str, Any]] = []
        self._idx = 0
        # Previous round's verify results (hermit, oops, cov) — used to diff
        # each round_summary against the round before it.
        self._prev_verify = None

        # LLM + the three agents (prompts editable in config.yaml under `agents:`)
        self.llm, self.llm_cfg = build_llm(cfg.get("llm", {}) or {})
        ag = cfg.get("agents", {}) or {}
        self.extractor = ExtractionAgent(
            self.llm, self.base_uri,
            (ag.get("extraction") or {}).get("system_prompt"))
        self.generator = GenerationAgent(
            self.llm, self.base_uri,
            (ag.get("generation") or {}).get("system_prompt"))
        self.corrector = CorrectionAgent(
            self.llm, self.base_uri,
            (ag.get("correction") or {}).get("system_prompt"))

        # tracing: full JSONL trace + truncated console echo (run.trace)
        tr = run.get("trace", {}) or {}
        self.tracer = Tracer(
            os.path.join(self.output_dir, f"{domain}_trace.jsonl"),
            enabled=bool(tr.get("enabled", True)),
            console=bool(tr.get("console", True)),
            truncate=int(tr.get("truncate", 600)))
        retries = int(self.llm_cfg.get("retries", 3))
        backoff = float(self.llm_cfg.get("retry_backoff", 10))
        self.extractor.llm = TracedLLM(self.llm, self.tracer, "extraction",
                                       retries, backoff)
        self.generator.llm = TracedLLM(self.llm, self.tracer, "generation",
                                       retries, backoff)
        self.corrector.llm = TracedLLM(self.llm, self.tracer, "correction",
                                       retries, backoff)
        self.tracer.log("run_start", domain=domain, base_uri=self.base_uri,
                        cq_file=self.cq_file, max_attempts=self.max_attempts,
                        model=self.llm_cfg["model"],
                        provider=self.llm_cfg["provider"])

    def _write(self, ontology: str) -> None:
        with open(self.onto_path, "w", encoding="utf-8") as f:
            f.write(ontology)

    def _write_testlog(self) -> None:
        with open(self.tests_path, "w", encoding="utf-8") as f:
            json.dump(self._testlog, f, indent=2, ensure_ascii=False)

    def _step(self, step: str, prompt_information: Any, ontology: str) -> None:
        self._steps.append({"step": step,
                            "prompt_information": prompt_information,
                            "ontology": ontology})
        with open(self.steps_path, "w", encoding="utf-8") as f:
            json.dump(self._steps, f, indent=2, ensure_ascii=False)

    def _track_tests(self, result: Dict[str, Any], phase: str, round_: int,
                     attempt, seconds: float) -> None:
        entry: Dict[str, Any] = {"n": len(self._testlog["executions"]) + 1,
                                 "round": round_, "phase": phase}
        if attempt is not None:
            entry["attempt"] = attempt
        entry["seconds"] = seconds
        entry["passed"] = bool(result.get("passed"))
        entry["covered"] = result.get("covered", [])
        entry["uncovered"] = result.get("uncovered", [])
        entry["results"] = result.get("results", {})
        if not result.get("results"):        # tool-level failure (no jar,
            entry["error"] = result.get("report", "")[:500]   # timeout, ...)
        self._testlog["executions"].append(entry)
        for outcomes in (result.get("results") or {}).values():
            for o in outcomes:
                self._testlog["test_history"].setdefault(
                    o.get("test", "?"), []).append(o.get("result", "?"))
        self._write_testlog()

    def _record(self, round_: int, phase: str, tool_calling: str,
                input_information: Any, output: Any, attempt: int = None,
                next_action: str = None, seconds: float = None) -> Dict[str, Any]:
        self._idx += 1
        rec: Dict[str, Any] = {"round": round_, "index": self._idx,
                               "phase": phase}
        if attempt is not None:
            rec["attempt"] = attempt
        rec["tool_calling"] = tool_calling
        rec["input_information"] = input_information
        rec["output"] = output
        if next_action is not None:
            rec["next_action"] = next_action
        if seconds is not None:
            rec["seconds"] = seconds
        self.records.append(rec)
        return rec

    async def _drive(self, toolmap, ontology, tool, kind, cqs=None,
                     phase="", round_=0):
        result: Dict[str, Any] = {}
        before: Dict[str, Any] = {}      # detector result before the last fix
        pending: Dict[str, Any] = None   # correction record awaiting `effect`
        prev_onto: str = None            # ontology before the last correction
        for attempt in range(self.max_attempts + 1):
            self._write(ontology)
            args = {"ontology_path": self.onto_path}
            if cqs is not None:
                args["cqs"] = cqs
            if tool == "themis_test":
                # official VTC test-suite input (falls back to list inside)
                args["testsuite_path"] = self.suite_path
            t0 = time.time()
            result = _parse_tool_result(await toolmap[tool].ainvoke(args))
            secs = round(time.time() - t0, 2)
            if tool == "themis_test":
                self._track_tests(result, phase or tool, round_,
                                  attempt + 1, secs)
            if pending is not None:     # the re-check after a correction:
                pending["effect"] = _effect(tool, before, result)
                if tool in ("cq_coverage", "themis_test") \
                        and prev_onto is not None:
                    b = set(before.get("covered") or [])
                    a = set(result.get("covered") or [])
                    if (b - a) and not (a - b) \
                            and not result.get("tool_error"):
                        # DESTRUCTIVE correction: coverage lost, nothing
                        # gained - revert to the pre-correction ontology so
                        # progress is monotonic, and retry from there.
                        pending["effect"]["rolled_back"] = True
                        self.tracer.log("rollback", tool=tool,
                                        lost=sorted(b - a))
                        ontology = prev_onto
                        self._write(ontology)
                        result = before
                pending = None
            next_action = ("pass" if result.get("passed")
                           else "give_up" if attempt == self.max_attempts
                           else "retry_tool" if result.get("tool_error")
                           else "correct")
            tool_in: Dict[str, Any] = {"ontology_file": self.onto_path}
            if cqs is not None:
                tool_in["cqs"] = [c["id"] for c in cqs]
            self._record(round_, phase or tool, tool, tool_in,
                         _summary(tool, result), attempt=attempt + 1,
                         next_action=next_action,
                         seconds=secs)
            self._step(tool,
                       {**tool_in, "report": result.get("report", "")},
                       ontology)
            if result.get("passed") or attempt == self.max_attempts:
                break
            if result.get("tool_error"):
                # The DETECTOR itself failed (e.g. Themis service unreachable)
                # - there is nothing in the ontology to fix; don't waste a
                # correction call, wait briefly and retry the tool.
                self.tracer.log("tool_error", tool=tool,
                                attempt=attempt + 1,
                                report=(result.get("report") or "")[:300])
                await asyncio.sleep(10)
                continue
            self.tracer.log("correction", tool=tool, kind=kind,
                            attempt=attempt + 1, max_attempts=self.max_attempts)
            uncovered_cqs = None
            if cqs is not None and result.get("uncovered"):
                unc = set(result["uncovered"])
                miss = result.get("missing", {}) or {}
                uncovered_cqs = [{**c, "missing": miss.get(c["id"], [])}
                                 for c in cqs if c["id"] in unc]
            t0 = time.time()
            ontology = await self.corrector.run(
                ontology, result.get("report", ""), kind=kind,
                uncovered_cqs=uncovered_cqs)
            corr_in: Dict[str, Any] = {"kind": kind,
                                       "report": result.get("report", "")}
            if uncovered_cqs:
                corr_in["uncovered_cqs"] = [
                    {"id": c["id"], "missing": c.get("missing", [])}
                    for c in uncovered_cqs]
            before = result
            prev_onto = ontology
            pending = self._record(round_, phase or tool, "correction_agent",
                                   corr_in,
                                   {"ontology_file": self.onto_path,
                                    "ontology_chars": len(ontology)},
                                   attempt=attempt + 1,
                                   seconds=round(time.time() - t0, 2))
            self._step(f"correction_agent + {tool}", corr_in, ontology)
        self._write(ontology)
        return ontology, result

    def build_graph(self, toolmap: Dict[str, Any]):
        SYNTAX = "RDF/XML syntax and writing-style errors"
        COVER = "uncovered competency questions"
        PITFALL = "OOPS! major / important pitfalls"
        CONSIST = "HermiT logical-consistency errors"
        THEMIS = "failing Themis competency-question tests"

        def with_terms(state):
            t = state.get("terms", {}) or {}
            return [{"id": c["id"], "value": c["value"],
                     "terms": t.get(c["id"], {})} for c in state["cqs"]]

        async def extract(state: AgentState) -> AgentState:
            self.tracer.log("phase", name="extract", agent="extraction",
                            n_cqs=len(state["cqs"]))
            t0 = time.time()
            terms = await self.extractor.run(state["cqs"])
            sanitized: Dict[str, List[str]] = {}
            for c in state["cqs"]:
                t = terms.get(c["id"])
                if isinstance(t, dict) and isinstance(t.get("tests"), list):
                    clean, changes = _sanitize_tests(t["tests"], t)
                    t["tests"] = clean
                    if changes:
                        sanitized[c["id"]] = changes
            for cid, ch in _harmonize_suite(state["cqs"], terms).items():
                sanitized.setdefault(cid, []).extend(ch)
            self._testlog["sanitized"] = sanitized
            with open(self.terms_path, "w", encoding="utf-8") as f:
                json.dump(_terms_doc(state["cqs"], terms), f, indent=2,
                          ensure_ascii=False)
            with open(self.suite_path, "w", encoding="utf-8") as f:
                f.write(_vtc_ttl(state["cqs"], terms, self.base_uri))
            self._testlog["testsuite_file"] = self.suite_path

            def gen(t):  
                v = t.get("tests") if isinstance(t, dict) else None
                return [str(x).strip() for x in v if str(x).strip()] \
                    if isinstance(v, list) else []
            self._testlog["generated"] = {
                c["id"]: gen(terms.get(c["id"]) or {}) for c in state["cqs"]}
            warns = {c["id"]: _test_schema_warnings(
                         self._testlog["generated"][c["id"]],
                         terms.get(c["id"]) or {})
                     for c in state["cqs"]}
            self._testlog["generated_warnings"] = {k: v for k, v in
                                                   warns.items() if v}
            self._write_testlog()
            self._step("extraction_agent",
                       {"cqs": state["cqs"]}, "")
            self._record(0, "extract", "extraction_agent",
                         {"cqs": [c["id"] for c in state["cqs"]]},
                         {"terms_file": self.terms_path,
                          "terms_per_cq": {k: sum(len(v) for v in (tv or {}).values()
                                                  if isinstance(v, list))
                                           for k, tv in terms.items()}},
                         seconds=round(time.time() - t0, 2))
            return {"terms": terms}

        async def generate(state: AgentState) -> AgentState:
            self.tracer.log("phase", name="generate", agent="generation",
                            n_cqs=len(state["cqs"]))
            t0 = time.time()
            onto = await self.generator.run(state["cqs"], state.get("terms"))
            self._write(onto)
            self._step("generation_agent",
                       {"cqs": state["cqs"], "terms": state.get("terms")},
                       onto)
            self._record(0, "generate", "generation_agent",
                         {"cqs": [c["id"] for c in state["cqs"]],
                          "with_terms": bool(state.get("terms"))},
                         {"ontology_file": self.onto_path,
                          "ontology_chars": len(onto)},
                         seconds=round(time.time() - t0, 2))
            return {"ontology": onto, "round": 0}

        async def syntax_phase(state: AgentState) -> AgentState:
            self.tracer.log("phase", name="syntax", tool="syntax_check")
            onto, res = await self._drive(toolmap, state["ontology"],
                                          "syntax_check", SYNTAX,
                                          phase="syntax",
                                          round_=state.get("round", 0) + 1)
            return {"ontology": onto, "syntax": res}

        async def cover_phase(state: AgentState) -> AgentState:
            """Step 2: literal CQ coverage - every mapped term exists."""
            self.tracer.log("phase", name="cover", tool="cq_coverage")
            onto, res = await self._drive(toolmap, state["ontology"],
                                          "cq_coverage", COVER,
                                          cqs=with_terms(state),
                                          phase="cover",
                                          round_=state.get("round", 0) + 1)
            return {"ontology": onto, "coverage": res}

        async def oops_phase(state: AgentState) -> AgentState:
            self.tracer.log("phase", name="oops", tool="oops_scan")
            onto, res = await self._drive(toolmap, state["ontology"],
                                          "oops_scan", PITFALL, phase="oops",
                                          round_=state.get("round", 0) + 1)
            return {"ontology": onto, "oops": res}

        async def hermit_phase(state: AgentState) -> AgentState:
            self.tracer.log("phase", name="hermit", tool="hermit_consistency")
            onto, res = await self._drive(toolmap, state["ontology"],
                                          "hermit_consistency", CONSIST,
                                          phase="hermit",
                                          round_=state.get("round", 0) + 1)
            return {"ontology": onto, "hermit": res}

        async def themis_phase(state: AgentState) -> AgentState:
            """Step 5: every CQ's Themis tests must pass against the
            ontology; failing tests go to the CorrectionAgent together
            with the OWL source."""
            self.tracer.log("phase", name="themis", tool="themis_test")
            onto, res = await self._drive(toolmap, state["ontology"],
                                          "themis_test", THEMIS,
                                          cqs=with_terms(state),
                                          phase="themis",
                                          round_=state.get("round", 0) + 1)
            return {"ontology": onto, "themis": res}

        async def verify(state: AgentState) -> AgentState:
            self.tracer.log("phase", name="verify")
            self._write(state["ontology"])
            p = self.onto_path
            rnd = state.get("round", 0) + 1

            async def check(tool, args):
                t0 = time.time()
                res = _parse_tool_result(await toolmap[tool].ainvoke(args))
                secs = round(time.time() - t0, 2)
                if tool == "themis_test":
                    self._track_tests(res, "verify", rnd, None, secs)
                tool_in: Dict[str, Any] = {"ontology_file": p}
                if tool in ("cq_coverage", "themis_test"):
                    tool_in["cqs"] = [c["id"] for c in state["cqs"]]
                self._record(rnd, "verify", tool, tool_in,
                             _summary(tool, res),
                             seconds=secs)
                return res

            syn = await check("syntax_check", {"ontology_path": p})
            hermit = await check("hermit_consistency", {"ontology_path": p})
            oops = await check("oops_scan", {"ontology_path": p})
            cov = await check("cq_coverage",
                              {"ontology_path": p, "cqs": with_terms(state)})
            them = await check("themis_test",
                               {"ontology_path": p, "cqs": with_terms(state),
                                "testsuite_path": self.suite_path})
            all_passed = bool(syn.get("passed") and oops.get("passed")
                              and hermit.get("passed") and cov.get("passed")
                              and them.get("passed"))
            summary_out = {"all_passed": all_passed,
                           "syntax_check": _summary("syntax_check", syn),
                           "cq_coverage": _summary("cq_coverage", cov),
                           "themis_test": _summary("themis_test", them),
                           "oops": _summary("oops_scan", oops),
                           "hermit": _summary("hermit_consistency", hermit)}
            if self._prev_verify is not None:
                ps, ph, po, pc, pt = self._prev_verify
                summary_out["change_from_previous_round"] = {
                    "syntax_check": _effect("syntax_check", ps, syn),
                    "cq_coverage": _effect("cq_coverage", pc, cov),
                    "themis_test": _effect("themis_test", pt, them),
                    "oops": _effect("oops_scan", po, oops),
                    "hermit": _effect("hermit_consistency", ph, hermit)}
            self._prev_verify = (syn, hermit, oops, cov, them)
            self._record(rnd, "verify", "round_summary",
                         {"checks": ["syntax_check", "hermit_consistency",
                                     "oops_scan", "cq_coverage",
                                     "themis_test"]},
                         summary_out,
                         next_action=("done" if all_passed
                                      or rnd >= self.max_attempts else "repeat"))
            self._step("verify [syntax_check + hermit_consistency + "
                       "oops_scan + cq_coverage + themis_test]",
                       {"all_passed": all_passed,
                        "reports": {
                            "syntax_check": syn.get("report", ""),
                            "hermit_consistency": hermit.get("report", ""),
                            "oops_scan": oops.get("report", ""),
                            "cq_coverage": cov.get("report", ""),
                            "themis_test": them.get("report", "")}},
                       state["ontology"])
            self.tracer.log("round", round=rnd, all_passed=all_passed,
                            syntax_errors=len(syn.get("errors", []) or []),
                            covered=cov.get("covered", []),
                            uncovered=cov.get("uncovered", []),
                            themis_uncovered=them.get("uncovered", []),
                            oops_major=oops.get("major_count"),
                            unsatisfiable=len(hermit.get(
                                "unsatisfiable_classes", []) or []))
            return {"syntax": syn, "oops": oops, "hermit": hermit,
                    "coverage": cov, "themis": them,
                    "all_passed": all_passed, "round": rnd}

        def route(state: AgentState) -> str:
            if state.get("all_passed") or state.get("round", 0) >= self.max_attempts:
                return "done"
            others_ok = all(bool((state.get(k) or {}).get("passed"))
                            for k in ("syntax", "coverage", "oops", "hermit"))
            if others_ok and not (state.get("themis") or {}).get("passed"):
                return "tests_only"   # all other agents are done: loop only
            return "repeat"           # the test -> fix cycle from here on

        g = StateGraph(AgentState)
        for name, fn in (("extract", extract), ("generate", generate),
                         ("syntax", syntax_phase), ("cover", cover_phase),
                         ("oops", oops_phase), ("hermit", hermit_phase),
                         ("themis", themis_phase), ("verify", verify)):
            g.add_node(name, fn)
        g.set_entry_point("extract")
        g.add_edge("extract", "generate")
        g.add_edge("generate", "syntax")
        g.add_edge("syntax", "cover")
        g.add_edge("cover", "oops")
        g.add_edge("oops", "hermit")
        g.add_edge("hermit", "themis")
        g.add_edge("themis", "verify")
        g.add_conditional_edges("verify", route,
                                {"repeat": "syntax", "tests_only": "themis",
                                 "done": END})
        return g.compile()
    
    
    def report(self, cq_list, final: AgentState) -> Dict[str, Any]:
        s, o, h, c, t = (final.get(k, {}) for k in ("syntax", "oops", "hermit",
                                                    "coverage", "themis"))
        rep = {
            "domain": self.domain,
            "base_uri": self.base_uri,
            "cq_file": self.cq_file,
            "cqs": [x["id"] for x in cq_list],
            "rounds": final.get("round", 0),
            "final_status": {
                "syntax_check": _summary("syntax_check", s),
                "oops": _summary("oops_scan", o),
                "hermit": _summary("hermit_consistency", h),
                "cq_coverage": _summary("cq_coverage", c),
                "themis_test": _summary("themis_test", t),
            },
            "all_passed": bool(s.get("passed") and o.get("passed")
                               and h.get("passed") and c.get("passed")
                               and t.get("passed")),
            "records": self.records,
            "ontology_file": self.onto_path,
            "terms_file": self.terms_path,
            "tests_file": self.tests_path,
            "testsuite_file": self.suite_path,
            "steps_file": self.steps_path,
        }
        with open(os.path.join(self.output_dir, f"{self.domain}_run.json"),
                  "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2)
        self.tracer.log("run_end", all_passed=rep["all_passed"],
                        rounds=rep["rounds"], ontology_file=self.onto_path,
                        trace_file=self.tracer.path)
        self.tracer.close()
        return rep


async def run(runner: MASEORun) -> dict:
    """Open one stdio MCP session, load the tools, run the workflow."""
    cq_list = load_cqs(runner.cq_file)
    client = MultiServerMCPClient(_connections())
    async with client.session("maseo") as session:
        tools = await load_mcp_tools(session)
        toolmap = {t.name: TracedTool(t, runner.tracer) for t in tools}
        graph = runner.build_graph(toolmap)
        final = await graph.ainvoke(
            {"cqs": cq_list},
            {"recursion_limit": runner.max_attempts * 10 + 12})
    return runner.report(cq_list, final)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate a validated OWL ontology from competency questions "
                    "(MASEO: generation + correction agents over a stdio MCP "
                    "server). Example: python mcp_client.py swo")
    ap.add_argument("domain", help="Dataset/domain name: wine, awo, odrl, swo, "
                                   "vgo, water.")
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"),
                    help="Path to config.yaml (default: ./config.yaml).")
    args = ap.parse_args()

    runner = MASEORun(args.config, args.domain)
    print(f"[init] domain={runner.domain}  base_uri={runner.base_uri}")
    print(f"[init] cq_file={runner.cq_file}")
    print(f"[init] model={runner.llm_cfg['model']} @ {runner.llm_cfg['base_url']}")
    print(f"[init] max_attempts={runner.max_attempts}  server={SERVER}")

    result = asyncio.run(run(runner))

    print(json.dumps(result["final_status"], indent=2))
    print(("ALL CHECKS PASSED" if result["all_passed"] else "NOT CONVERGED")
          + f" in {result['rounds']} round(s).")
    print("Ontology:", result["ontology_file"])


if __name__ == "__main__":
    main()

