import json
import os
import re
import shutil
import subprocess
import tempfile

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ThemisTest")

PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEMIS_ENDPOINT = "https://themis.linkeddata.es/rest/api/results"


def read_ontology(path: str) -> str:
    if path and os.path.isfile(path):
        return open(path, encoding="utf-8").read()
    return ""


def run_themis(onto, tests, mode, endpoint, jar, timeout):
    if mode == "jar":
        tdir = tempfile.mkdtemp(prefix="themis_")
        tests_path = os.path.join(tdir, "tests.txt")
        onto_path = os.path.join(tdir, "onto.owl")
        open(tests_path, "w", encoding="utf-8").write(";".join(tests))
        open(onto_path, "w", encoding="utf-8").write(onto)
        cmd = ["java", "-jar", jar, "-t", tests_path, "-f", "list",
               "-o", onto_path, "-r", "json"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
            raw = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return None, f"Themis timeout (>{timeout}s)."
        except FileNotFoundError:
            return None, "java not found on PATH."
        finally:
            shutil.rmtree(tdir, ignore_errors=True)
    else:
        payload = {"ontologiesCode": [onto], "format": "json", "tests": tests}
        try:
            resp = requests.post(endpoint, json=payload, timeout=timeout)
        except Exception as e:
            return None, f"Themis API request failed: {e}"
        if resp.status_code >= 400:
            return None, f"Themis API error HTTP {resp.status_code}: {resp.text[:500]}"
        raw = resp.text
    start, end = raw.find("["), raw.rfind("]")
    try:
        payload = json.loads(raw[start:end + 1])
        assert isinstance(payload, list)
    except Exception:
        return None, "Themis returned no parseable JSON:\n" + raw[:2000]
    squash = lambda s: re.sub(r"\s+", " ", str(s)).strip()
    verdict = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        test = squash(entry.get("Test", ""))
        for r in entry.get("Results") or []:
            verdict[test] = str(r.get("Result", "Unknown"))
    return verdict, ""


@mcp.tool()
def themis_test(ontology_path: str = "", cqs=None, mode: str = "api",
                jar_path: str = "", endpoint: str = THEMIS_ENDPOINT,
                timeout: int = 300) -> dict:
    """Run each competency question's Themis tests against the ontology."""
    cqs = [c if isinstance(c, dict) else {"id": str(c)} for c in (cqs or [])]
    onto = read_ontology(ontology_path)
    mode = str(mode or "api").lower()
    jar = (jar_path or os.path.join(PACKAGE, "themis.jar")) \
        if mode == "jar" else None
    ids = [str(c.get("id")) for c in cqs]

    def cq_tests(c):
        return [str(t).strip() for t in (c.get("tests") or []) if str(t).strip()]

    if not cqs or not onto or (mode == "jar" and not os.path.isfile(jar)):
        reason = ("No CQs given." if not cqs else
                  "No ontology given." if not onto else
                  f"Themis jar not found at '{jar}' (set themis.jar_path in "
                  "config.yaml, or switch themis.mode to 'api').")
        return {"passed": False, "covered": [], "uncovered": ids,
                "missing": {}, "skipped": [], "tool_error": True,
                "report": reason}

    all_tests = list(dict.fromkeys(t for c in cqs for t in cq_tests(c)))
    if not all_tests:
        return {"passed": True, "covered": ids, "uncovered": [],
                "missing": {}, "skipped": [],
                "report": "No Themis tests mapped to any CQ."}

    verdict, error = run_themis(onto, all_tests, mode, endpoint, jar, timeout)
    if verdict is None:
        return {"passed": False, "covered": [], "uncovered": ids,
                "missing": {}, "skipped": [], "tool_error": True,
                "report": error}

    squash = lambda s: re.sub(r"\s+", " ", str(s)).strip()
    covered, uncovered, missing, skipped = [], [], {}, []
    results = {}
    for c in cqs:
        cid = str(c.get("id"))
        fails = []
        results[cid] = []
        for t in cq_tests(c):
            result = verdict.get(squash(t), "UnsupportedSyntax")
            results[cid].append(f"{t} -> {result}")
            if result == "UnsupportedSyntax":
                if t not in skipped:
                    skipped.append(t)
            elif result != "Passed":
                fails.append(f"{t} -> {result}")
        (uncovered if fails else covered).append(cid)
        if fails:
            missing[cid] = fails
    report = (f"Themis tests: {len(covered)}/{len(cqs)} CQs pass all their "
              "tests. "
              + ("Every competency-question test passed."
                 if not uncovered else
                 "FAILING TESTS:\n" + "\n".join(
                     f"{cid}:\n  " + "\n  ".join(missing[cid])
                     for cid in uncovered)
                 + "\nFix each failing test in the ontology: Undefined = add "
                   "the term under exactly that name; Incorrect = declare it "
                   "with the right type; Absent = add the tested axiom so the "
                   "knowledge is modelled; Conflict = the ontology "
                   "contradicts the test, correct the axioms."))
    if skipped:
        report += ("\nSKIPPED (unsupported Themis syntax; excluded from "
                   "coverage, do NOT try to fix these in the ontology): "
                   + "; ".join(skipped))
    return {"passed": not uncovered, "covered": covered,
            "uncovered": uncovered, "missing": missing, "skipped": skipped,
            "results": results, "report": report}


if __name__ == "__main__":
    mcp.run(transport="stdio")
