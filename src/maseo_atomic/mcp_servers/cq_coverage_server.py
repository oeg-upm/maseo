import os
import re

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("CQCoverage")


def read_ontology(path: str) -> str:
    if path and os.path.isfile(path):
        return open(path, encoding="utf-8").read()
    return ""


@mcp.tool()
def cq_coverage(ontology_path: str = "", cqs=None) -> dict:
    """Check that every competency question's terms appear in the ontology."""
    cqs = [c if isinstance(c, dict) else {"id": str(c)} for c in (cqs or [])]
    onto = read_ontology(ontology_path)
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
    checked = {}
    for c in cqs:
        cid = str(c.get("id"))
        terms = [t for v in (c.get("terms") or {}).values()
                 if isinstance(v, list) for t in v]
        checked[cid] = [t + (" [MISSING]" if norm(t) not in names else "")
                        for t in terms] or ["(no terms mapped; "
                                            + ("cited" if cid in cited
                                               else "NOT cited")
                                            + " in dc:source)"]
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
    return {"passed": not uncovered, "covered": covered,
            "uncovered": uncovered, "missing": missing, "checked": checked,
            "report": report}


if __name__ == "__main__":
    mcp.run(transport="stdio")
