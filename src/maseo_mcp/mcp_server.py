import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("maseo-ontology-eval")
HERE = os.path.dirname(os.path.abspath(__file__))
OOPS_ENDPOINT = "https://oops.linkeddata.es/rest"


def _load(content: str, path: str) -> str:
    if content:
        return content
    if path and os.path.isfile(path):
        return open(path, encoding="utf-8").read()
    return ""


@mcp.tool()
def syntax_check(ontology_content: str = "", ontology_path: str = "") -> dict:
    onto = _load(ontology_content, ontology_path)
    if not onto:
        return {"passed": False, "errors": ["No ontology given."],
                "report": "No ontology given."}
    errors = []
    body = onto.strip()
    if not body.startswith(("<?xml", "<rdf:RDF", "<RDF")):
        head = body[:80].replace("\n", " ")
        errors.append("Document must START with '<?xml' or '<rdf:RDF' - no "
                      f"markdown fences or commentary before it (found: "
                      f"'{head}...').")
    if not body.endswith(">"):
        tail = body[-80:].replace("\n", " ")
        errors.append("Document must END with '</rdf:RDF>' - no trailing "
                      f"text (found: '...{tail}').")
    import xml.etree.ElementTree as ET
    root = None
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        errors.append(f"XML is not well-formed: {e}")
    if root is not None and not (root.tag == "RDF" or root.tag.endswith("}RDF")):
        errors.append(f"Root element is '{root.tag}'; it must be rdf:RDF "
                      "(namespace http://www.w3.org/1999/02/22-rdf-syntax-ns#).")
    if root is not None:
        try:
            from rdflib import Graph
            g = Graph()
            g.parse(data=body, format="xml")
            if len(g) == 0:
                errors.append("The RDF/XML parses but yields no triples - "
                              "the document is empty of actual statements.")
        except Exception as e:
            errors.append(f"RDF/XML is not parseable as RDF: {e}")
    report = ("No syntax or style problems." if not errors else
              "SYNTAX / STYLE ERRORS:\n"
              + "\n".join(f"- {e}" for e in errors)
              + "\nReturn ONE complete well-formed RDF/XML document: nothing "
                "before '<?xml', an rdf:RDF root element, valid XML, and "
                "nothing after '</rdf:RDF>'.")
    return {"passed": not errors, "errors": errors, "report": report}


@mcp.tool()
def oops_scan(ontology_content: str = "", ontology_path: str = "",
              timeout: int = 120) -> dict:
    onto = _load(ontology_content, ontology_path)
    if not onto:
        return {"passed": False, "major_count": 0, "pitfalls": [],
                "report": "No ontology given."}
    body = ('<?xml version="1.0" encoding="UTF-8"?><OOPSRequest>'
            '<OntologyURI></OntologyURI>'
            f'<OntologyContent>{escape(onto)}</OntologyContent>'
            '<Pitfalls></Pitfalls><OutputFormat>RDF/XML</OutputFormat></OOPSRequest>')
    try:
        r = requests.post(OOPS_ENDPOINT, data=body.encode("utf-8"),
                          headers={"Content-Type": "application/xml;charset=UTF-8"},
                          timeout=timeout)
        r.raise_for_status()
        from rdflib import Graph
        g = Graph()
        g.parse(data=r.text, format="xml")
    except Exception as e:
        return {"passed": False, "major_count": 0, "pitfalls": [],
                "report": f"OOPS! scan failed: {e}"}
    # Group triples by subject; match predicates/values by local name.
    ln = lambda u: re.split(r"[#/]", str(u).rstrip("#/"))[-1]
    subj: dict = {}
    for s, p, o in g:
        subj.setdefault(s, {}).setdefault(ln(p).lower(), []).append(str(o))
    lines, pitfalls = [], []
    for pr in subj.values():
        imp = ln((pr.get("hasimportancelevel") or pr.get("importance") or [""])[0])
        if imp.lower() not in ("critical", "important"):
            continue
        code = (pr.get("hascode") or pr.get("code") or ["?"])[0]
        name = (pr.get("hasname") or pr.get("name") or [""])[0]
        desc = (pr.get("hasdescription") or pr.get("description") or [""])[0]
        n = (pr.get("hasnumberaffectedelements") or [""])[0]
        pitfalls.append({"importance": imp, "code": code, "name": name,
                         "description": desc, "affected_elements": n})
        lines.append(f"[{imp}] {code} {name}".rstrip()
                     + (f"\n    {desc}" if desc else "")
                     + (f"\n    affected elements: {n}" if n else ""))
    return {"passed": not lines, "major_count": len(lines), "pitfalls": pitfalls,
            "report": "\n".join(lines) if lines else "No major pitfalls."}


@mcp.tool()
def hermit_consistency(ontology_content: str = "", ontology_path: str = "",
                       timeout: int = 300, java_heap: str = "2G",
                       hermit_jar: str = "") -> dict:
    jar = next((j for j in (hermit_jar, os.environ.get("HERMIT_JAR"),
                            os.path.join(HERE, "HermiT.jar"),
                            os.path.join(HERE, "hermit.jar"))
                if j and os.path.isfile(j)), None)
    onto = _load(ontology_content, ontology_path)
    if not jar or not onto:
        return {"passed": False, "consistent": None, "unsatisfiable_classes": [],
                "report": ("HermiT.jar not found (put it next to mcp_server.py "
                           "or set $HERMIT_JAR)." if not jar else "No ontology given.")}
    tmp = tempfile.NamedTemporaryFile(suffix=".owl", mode="w",
                                      encoding="utf-8", delete=False)
    tmp.write(onto); tmp.close()
    try:
        cmd = ["java", f"-Xmx{java_heap}", "-cp", jar,
               "org.semanticweb.HermiT.cli.CommandLine", "-k", "-U",
               Path(tmp.name).resolve().as_uri()]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"passed": False, "consistent": None, "unsatisfiable_classes": [],
                "report": f"HermiT timeout (>{timeout}s)."}
    except FileNotFoundError:
        return {"passed": False, "consistent": None, "unsatisfiable_classes": [],
                "report": "java not found on PATH."}
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    raw = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if "inconsistent ontology" in raw.lower():
        return {"passed": False, "consistent": False, "unsatisfiable_classes": [],
                "report": "Ontology is INCONSISTENT.\n" + raw[:4000]}
    unsat = [c for c in dict.fromkeys(re.findall(r"<([^>]+)>", raw))
             if not c.endswith("owl#Nothing")]
    consistent = "is satisfiable" in raw.lower()
    report = ("Consistent: " + ("yes" if consistent else "unknown")
              + ("\nUnsatisfiable classes:\n" + "\n".join("  - " + c for c in unsat)
                 if unsat else "\nUnsatisfiable classes: none")
              + "\n\n" + raw[:4000])
    return {"passed": consistent and not unsat, "consistent": consistent,
            "unsatisfiable_classes": unsat, "report": report}


@mcp.tool()
def cq_coverage(ontology_content: str = "", ontology_path: str = "",
                cqs=None, cqs_path: str = "") -> dict:
    if cqs_path:
        cqs = json.load(open(cqs_path, encoding="utf-8"))
    elif isinstance(cqs, str):
        cqs = json.loads(cqs)
    cqs = [c if isinstance(c, dict) else {"id": str(c)} for c in (cqs or [])]
    onto = _load(ontology_content, ontology_path)
    if not cqs or not onto:
        return {"passed": False, "covered": [],
                "uncovered": [str(c.get("id")) for c in cqs], "missing": {},
                "report": "No CQs given." if not cqs else "No ontology given."}
    norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
    names = {norm(re.split(r"[#/]", u)[-1]) for u in
             re.findall(r'rdf:(?:about|ID|resource)="([^"]+)"', onto)}
    names |= {norm(x) for x in
              re.findall(r"<rdfs:label[^>]*>([^<]+)</rdfs:label>", onto)}
    cited = set(re.findall(
        r"\(\s*competency[_ ]question\s*\)\s*([A-Za-z][\w\-]*)", onto, re.I))
    covered, uncovered, missing = [], [], {}
    for c in cqs:
        cid = str(c.get("id"))
        terms = [t for k, v in (c.get("terms") or {}).items()
                 if k not in ("axioms", "tests") and isinstance(v, list)
                 for t in v]
        miss = [t for t in terms if norm(t) not in names]
        ok = (not miss) if terms else (cid in cited)
        (covered if ok else uncovered).append(cid)
        if miss:
            missing[cid] = miss
    report = (f"Covered {len(covered)}/{len(cqs)}. "
              + ("All competency-question terms are captured."
                 if not uncovered else
                 "UNCOVERED: " + "; ".join(
                     f"{cid} missing [{', '.join(missing[cid])}]" if cid in missing
                     else f"{cid} (no dc:source citation)" for cid in uncovered)
                 + ". Add each missing term under exactly that name and record "
                   "the CQ id in its dc:source as '(competency_question) <id>'."))
    return {"passed": not uncovered, "covered": covered, "uncovered": uncovered,
            "missing": missing, "report": report}


THEMIS_ENDPOINT = "https://themis.linkeddata.es/rest/api/results"


def _themis_cfg() -> tuple:
    mode, endpoint = "api", THEMIS_ENDPOINT
    try:
        import yaml
        with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        th = cfg.get("themis", {}) or {}
        mode = str(th.get("mode") or mode).lower()
        endpoint = th.get("endpoint") or endpoint
    except Exception:
        pass
    mode = str(os.environ.get("THEMIS_MODE") or mode).lower()
    endpoint = os.environ.get("THEMIS_ENDPOINT") or endpoint
    return mode, endpoint


@mcp.tool()
def themis_test(ontology_content: str = "", ontology_path: str = "",
                cqs=None, cqs_path: str = "", timeout: int = 300,
                themis_jar: str = "", mode: str = "",
                endpoint: str = "", testsuite_path: str = "") -> dict:
    if cqs_path:
        cqs = json.load(open(cqs_path, encoding="utf-8"))
    elif isinstance(cqs, str):
        cqs = json.loads(cqs)
    cqs = [c if isinstance(c, dict) else {"id": str(c)} for c in (cqs or [])]
    onto = _load(ontology_content, ontology_path)
    cfg_mode, cfg_endpoint = _themis_cfg()
    mode = (str(mode).lower() or cfg_mode)
    if mode not in ("api", "jar"):
        mode = "api"
    endpoint = endpoint or cfg_endpoint
    jar = next((j for j in (themis_jar, os.environ.get("THEMIS_JAR"),
                            os.path.join(HERE, "themis.jar"),
                            os.path.join(HERE, "Themis.jar"))
                if j and os.path.isfile(j)), None) if mode == "jar" else None
    ids = [str(c.get("id")) for c in cqs]

    def cq_tests(c) -> list:
        v = (c.get("terms") or {}).get("tests") or []
        return [str(t).strip() for t in v if str(t).strip()]

    def missing_all(reason: str) -> dict:
        return {str(c.get("id")): [f"{t} -> NotExecuted ({reason})"
                                   for t in cq_tests(c)]
                for c in cqs if cq_tests(c)}

    if not cqs or not onto or (mode == "jar" and not jar):
        reason = ("No CQs given." if not cqs else
                  "No ontology given." if not onto else
                  "themis.jar not found (put it next to mcp_server.py or "
                  "set $THEMIS_JAR, or switch themis.mode to 'api').")
        return {"passed": False, "covered": [], "uncovered": ids,
                "missing": missing_all(reason), "results": {},
                "tool_error": True, "mode": mode, "report": reason}

    all_tests = list(dict.fromkeys(t for c in cqs for t in cq_tests(c)))
    if not all_tests:
        return {"passed": True, "covered": ids, "uncovered": [],
                "missing": {}, "results": {},
                "report": "No Themis tests mapped to any CQ; nothing to run."}

    tdir = tempfile.mkdtemp(prefix="themis_") if mode == "jar" else None
    tests_path = os.path.join(tdir, "tests.txt") if tdir else None
    onto_path = os.path.join(tdir, "onto.owl") if tdir else None
    if onto_path:
        open(onto_path, "w", encoding="utf-8").write(onto)
    squash = lambda s: re.sub(r"\s+", " ", str(s)).strip()

    def run_batch(batch, suite_file=None):
        if mode == "jar":
            if suite_file:
                cmd = ["java", "-jar", jar, "-t", suite_file, "-f", "RDF",
                       "-o", onto_path, "-r", "json"]
            else:
                open(tests_path, "w", encoding="utf-8").write(";".join(batch))
                cmd = ["java", "-jar", jar, "-t", tests_path, "-f", "list",
                       "-o", onto_path, "-r", "json"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=timeout)
            except subprocess.TimeoutExpired:
                return f"Themis timeout (>{timeout}s)."
            except FileNotFoundError:
                return "java not found on PATH."
            raw = (proc.stdout or "") + ("\n" + proc.stderr
                                         if proc.stderr else "")
        else:   # api: same backend the jar talks to, called directly
            payload = {"ontologiesCode": [onto], "format": "json"}
            if suite_file:
                try:
                    payload["testfile"] = open(suite_file,
                                               encoding="utf-8").read()
                except OSError as e:
                    return f"Cannot read the VTC test suite ({e})."
            else:
                payload["tests"] = list(batch)
            try:
                resp = requests.post(endpoint, json=payload, timeout=timeout)
            except Exception as e:
                return (f"Themis API request failed "
                        f"({type(e).__name__}: {e}).")
            if resp.status_code >= 400:
                return (f"Themis API error HTTP {resp.status_code}: "
                        + resp.text[:500])
            if resp.status_code == 204 or not resp.text.strip():
                return (f"Themis API returned no results (HTTP "
                        f"{resp.status_code}, empty body) - the tests could "
                        "not be executed.")
            raw = resp.text
        start, end = raw.find("["), raw.rfind("]")
        try:
            payload = json.loads(raw[start:end + 1])
            assert isinstance(payload, list)
        except Exception:
            return ("Themis returned no parseable JSON (service "
                    "unreachable or error). Raw output:\n" + raw[:2000])
        out = {}
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            test = squash(entry.get("Test", ""))
            for r in entry.get("Results") or []:
                res = str(r.get("Result", "Unknown"))
                detail = ""
                for key in ("Undefined", "Incorrect"):
                    if r.get(key):
                        detail = f" (undefined/wrong terms: {r[key]})"
                out[test] = (res, detail)
        return out

    try:
        verdict = {}
        state = {"consec": 0}

        def execute(batch):
            if state["consec"] >= 5 and not verdict:
                return ("Themis service appears down: 5 consecutive "
                        "failed calls.")
            out = run_batch(batch)
            if isinstance(out, str) and len(batch) == 1:
                time.sleep(5)
                out = run_batch(batch)
            if isinstance(out, dict):
                state["consec"] = 0
                verdict.update(out)
                return ""
            state["consec"] += 1
            if len(batch) == 1:
                return out
            time.sleep(3)
            mid = len(batch) // 2
            e1 = execute(batch[:mid])
            e2 = execute(batch[mid:])
            return e1 or e2

        if testsuite_path and os.path.isfile(testsuite_path):
            out = run_batch(all_tests, suite_file=testsuite_path)
            if isinstance(out, dict):
                verdict.update(out)
        remaining = [t for t in all_tests if squash(t) not in verdict]
        hard_err = execute(remaining) if remaining else ""
        if not verdict:
            err = hard_err or "Themis returned no results."
            return {"passed": False, "covered": [], "uncovered": ids,
                    "missing": missing_all(
                        re.sub(r"\s+", " ", err).strip()[:300]),
                    "results": {}, "tool_error": True, "mode": mode,
                    "report": err}

        for t in [t for t in all_tests if squash(t) not in verdict][:12]:
            solo = run_batch([t])
            if isinstance(solo, dict):
                verdict.update(solo)
        hard_short = re.sub(r"\s+", " ", hard_err).strip()[:160]
    finally:
        if tdir:
            for f in (tests_path, onto_path):
                try:
                    os.remove(f)
                except OSError:
                    pass
            try:
                os.rmdir(tdir)
            except OSError:
                pass

    covered, uncovered, missing = [], [], {}
    results = {}   
    skipped = [] 
    for c in cqs:
        cid = str(c.get("id"))
        fails, outcomes = [], []
        for t in cq_tests(c):
            if squash(t) in verdict:
                res, detail = verdict[squash(t)]
            elif hard_err:     
                res, detail = "NotExecuted", f" ({hard_short})"
            else:               
                res, detail = "UnsupportedSyntax", ""
            outcomes.append({"test": t, "result": res,
                             "detail": detail.strip()})
            if res == "UnsupportedSyntax":
                if t not in skipped:
                    skipped.append(t)
            elif res != "Passed":
                fails.append(f"{t} -> {res}{detail}")
        results[cid] = outcomes
        (uncovered if fails else covered).append(cid)
        if fails:
            missing[cid] = fails
    all_res = [o["result"] for outs in results.values() for o in outs
               if o["result"] != "UnsupportedSyntax"]
    all_conflict = len(all_res) >= 3 and all(r == "Conflict" for r in all_res)
    if all_conflict:
        report = (f"Themis tests: 0/{len(cqs)} CQs pass - EVERY test "
                  "returned Conflict. This is ONE global problem, not "
                  f"{len(all_res)} modelling gaps: a blanket Conflict means "
                  "the reasoner already finds the ontology plus each test's "
                  "auxiliary axioms inconsistent BEFORE the test can be "
                  "evaluated. The ontology is logically inconsistent, or "
                  "contains a global axiom (an over-broad owl:disjointWith, "
                  "a universal restriction, a closure axiom) that "
                  "contradicts every synthetic test individual. Do NOT "
                  "change the tested axioms one by one - find and remove "
                  "the contradiction, keep everything else intact, and the "
                  "tests will evaluate normally on the next run.")
    else:
        report = (f"Themis tests: {len(covered)}/{len(cqs)} CQs pass all "
                  "their tests. "
                  + ("Every competency-question test passed."
                     if not uncovered else
                     "FAILING TESTS:\n" + "\n".join(
                         f"{cid}:\n  " + "\n  ".join(missing[cid])
                         for cid in uncovered)
                     + "\nFix each failing test in the ontology: Undefined "
                       "= add the term under exactly that name; Incorrect = "
                       "declare it with the right type (class/property/"
                       "individual); Absent = add the tested axiom "
                       "(subclass, domain/range, restriction) so the "
                       "knowledge is modelled; Conflict = the ontology "
                       "contradicts the test, correct the axioms. Record "
                       "each CQ id in dc:source as "
                       "'(competency_question) <id>'."))
    if skipped:
        report += ("\nSKIPPED (Themis cannot execute these - unsupported "
                   "syntax; excluded from coverage, do NOT try to fix them "
                   "in the ontology): " + "; ".join(skipped))
    if hard_err and any(o["result"] == "NotExecuted"
                        for outs in results.values() for o in outs):
        report += ("\nNOTE: 'NotExecuted' tests failed at the NETWORK level "
                   "(Themis call dropped) - they are NOT ontology problems; "
                   "do not change the ontology for them.")
    return {"passed": not uncovered, "covered": covered,
            "uncovered": uncovered, "missing": missing,
            "results": results, "mode": mode, "report": report}


@mcp.resource("oops://pitfall-catalogue")
def pitfall_catalogue() -> str:
    """OOPS! pitfall reference (codes + typical importance)."""
    return json.dumps({
        "about": "OOPS! pitfall reference; oops_scan keeps only what the live "
                 "service marks Critical/Important.",
        "service": OOPS_ENDPOINT,
        "pitfalls": {
            "P05": "Wrong inverse relationships (Critical)",
            "P06": "Cycles in the class hierarchy (Critical)",
            "P10": "Missing disjointness (Important)",
            "P11": "Missing domain or range in properties (Important)",
            "P19": "Multiple domains or ranges in properties (Critical)",
            "P24": "Recursive definitions (Important)",
            "P27": "Wrong equivalent relationships (Critical)",
            "P28": "Wrong symmetric relationships (Critical)",
            "P29": "Wrong transitive relationships (Critical)",
            "P30": "Equivalent classes not explicitly declared (Important)",
            "P31": "Wrong equivalent classes (Critical)",
            "P34": "Untyped class (Important)",
            "P35": "Untyped property (Important)",
            "P41": "No license declared (Important)"}}, indent=2)


@mcp.resource("cq4oe://provenance-format")
def provenance_format() -> str:
    """How CQ provenance is encoded in the generated OWL."""
    return ("Every entity records why it exists via dc:source "
            "(http://purl.org/dc/elements/1.1/source), one tagged line per "
            "source: '(competency_question) CQ6', '(pitfall) <...>', "
            "'(error_message) <...>'. cq_coverage checks each input CQ id "
            "appears in at least one '(competency_question) <id>' line.")


def _agent_prompt(name: str, default: str) -> str:
    try:
        import yaml
        with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        p = ((cfg.get("agents", {}) or {}).get(name, {}) or {}) \
            .get("system_prompt") or default
    except Exception:
        p = default
    try:
        spec = open(os.path.join(HERE, "agent.md"), encoding="utf-8").read()
        spec = re.sub(r"^\s*---\n.*?\n---\n", "", spec, count=1, flags=re.S).strip()
        return p + ("\n\n" + spec if spec else "")
    except OSError:
        return p


@mcp.prompt()
def generate_ontology(competency_questions: str, base_uri: str) -> str:
    """Generation-agent prompt (edit in config.yaml: agents.generation)."""
    sys = _agent_prompt("generation",
                        "Produce a complete OWL ontology (RDF/XML) answering "
                        "the competency questions. Return ONLY the RDF/XML.")
    return (sys.replace("{base_uri}", base_uri)
            + f"\n\nBase URI: {base_uri}\n\nCompetency questions:\n"
            + competency_questions)


@mcp.prompt()
def correct_ontology(ontology: str, report: str) -> str:
    """Correction-agent prompt (edit in config.yaml: agents.correction)."""
    sys = _agent_prompt("correction",
                        "Fix the reported issues in the RDF/XML ontology; "
                        "preserve provenance. Return ONLY the RDF/XML.")
    return sys + "\n\nOntology:\n" + ontology + "\n\nIssues to fix:\n" + report


if __name__ == "__main__":
    mcp.run()
