import os
import xml.etree.ElementTree as ET

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("SyntaxCheck")


def read_ontology(path: str) -> str:
    if path and os.path.isfile(path):
        return open(path, encoding="utf-8").read()
    return ""


@mcp.tool()
def syntax_check(ontology_path: str = "") -> dict:
    """Check that the ontology file is well-formed RDF/XML."""
    onto = read_ontology(ontology_path)
    if not onto:
        return {"passed": False, "errors": ["No ontology given."],
                "report": "No ontology given."}
    errors = []
    root = None
    triples = 0
    try:
        root = ET.fromstring(onto)
    except ET.ParseError as e:
        errors.append(f"XML is not well-formed: {e}")
    if root is not None and not root.tag.endswith("RDF"):
        errors.append(f"Root element is '{root.tag}'; it must be rdf:RDF.")
    if root is not None:
        try:
            from rdflib import Graph
            g = Graph()
            g.parse(data=onto, format="xml")
            triples = len(g)
            if triples == 0:
                errors.append("The RDF/XML parses but yields no triples.")
        except Exception as e:
            errors.append(f"RDF/XML is not parseable as RDF: {e}")
    report = (f"No syntax problems ({triples} triples parsed)."
              if not errors else
              "SYNTAX ERRORS:\n" + "\n".join(f"- {e}" for e in errors))
    return {"passed": not errors, "errors": errors, "triples": triples,
            "report": report}


if __name__ == "__main__":
    mcp.run(transport="stdio")
