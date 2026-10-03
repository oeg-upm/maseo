import os
import re
from xml.sax.saxutils import escape

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("OOPSScan")

OOPS_ENDPOINT = "https://oops.linkeddata.es/rest"


def read_ontology(path: str) -> str:
    if path and os.path.isfile(path):
        return open(path, encoding="utf-8").read()
    return ""


@mcp.tool()
def oops_scan(ontology_path: str = "", timeout: int = 120) -> dict:
    """Scan the ontology with the OOPS! pitfall service and report Critical/Important pitfalls."""
    onto = read_ontology(ontology_path)
    if not onto:
        return {"passed": False, "pitfalls": [], "report": "No ontology given."}
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
        return {"passed": False, "pitfalls": [], "tool_error": True,
                "report": f"OOPS! scan failed: {e}"}
    local = lambda u: re.split(r"[#/]", str(u).rstrip("#/"))[-1]
    subjects = {}
    for s, p, o in g:
        subjects.setdefault(s, {}).setdefault(local(p).lower(), []).append(str(o))
    lines, pitfalls = [], []
    for props in subjects.values():
        code = (props.get("hascode") or props.get("code") or [None])[0]
        importance = local((props.get("hasimportancelevel")
                            or props.get("importance") or [""])[0])
        if not code or not importance:
            continue
        name = (props.get("hasname") or props.get("name") or [""])[0]
        description = (props.get("hasdescription")
                       or props.get("description") or [""])[0]
        affected = sorted({local(u) for u in (props.get("hasaffectedelement")
                                              or props.get("affectedelement")
                                              or []) if local(u)})
        # every pitfall (any importance) is returned for the log; only
        # Critical/Important ones block the check and enter the report
        pitfalls.append({"importance": importance, "code": code, "name": name,
                         "description": description, "affected": affected})
        if importance.lower() not in ("critical", "important"):
            continue
        lines.append(f"[{importance}] {code} {name}".rstrip()
                     + (f"\n    {description}" if description else "")
                     + (f"\n    affected elements: {', '.join(affected)}"
                        if affected else ""))
    return {"passed": not lines, "pitfalls": pitfalls,
            "report": "\n".join(lines) if lines else "No major pitfalls."}


if __name__ == "__main__":
    mcp.run(transport="stdio")
