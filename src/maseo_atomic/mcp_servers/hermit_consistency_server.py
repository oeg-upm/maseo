import os
import re
import subprocess
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("HermiTConsistency")

PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_ontology(path: str) -> str:
    if path and os.path.isfile(path):
        return open(path, encoding="utf-8").read()
    return ""


@mcp.tool()
def hermit_consistency(ontology_path: str = "", jar_path: str = "",
                       timeout: int = 300, java_heap: str = "2G") -> dict:
    """Run the HermiT reasoner and report inconsistency or unsatisfiable classes."""
    jar = jar_path or os.path.join(PACKAGE, "HermiT.jar")
    onto = read_ontology(ontology_path)
    if not os.path.isfile(jar) or not onto:
        return {"passed": False, "consistent": None,
                "unsatisfiable_classes": [], "tool_error": True,
                "report": (f"HermiT jar not found at '{jar}' (set "
                           "hermit.jar_path in config.yaml)."
                           if not os.path.isfile(jar)
                           else "No ontology given.")}
    tmp = tempfile.NamedTemporaryFile(suffix=".owl", mode="w",
                                      encoding="utf-8", delete=False)
    tmp.write(onto)
    tmp.close()
    try:
        cmd = ["java", f"-Xmx{java_heap}", "-cp", jar,
               "org.semanticweb.HermiT.cli.CommandLine", "-k", "-U",
               Path(tmp.name).resolve().as_uri()]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"passed": False, "consistent": None,
                "unsatisfiable_classes": [], "tool_error": True,
                "report": f"HermiT timeout (>{timeout}s)."}
    except FileNotFoundError:
        return {"passed": False, "consistent": None,
                "unsatisfiable_classes": [], "tool_error": True,
                "report": "java not found on PATH."}
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    raw = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    output = raw.strip()[:2000]
    if "inconsistent ontology" in raw.lower():
        return {"passed": False, "consistent": False,
                "unsatisfiable_classes": [], "output": output,
                "report": "Ontology is INCONSISTENT.\n" + raw[:4000]}
    unsatisfiable = [c for c in dict.fromkeys(re.findall(r"<([^>]+)>", raw))
                     if not c.endswith("owl#Nothing")]
    consistent = "is satisfiable" in raw.lower()
    report = ("Consistent: " + ("yes" if consistent else "unknown")
              + ("\nUnsatisfiable classes:\n"
                 + "\n".join("  - " + c for c in unsatisfiable)
                 if unsatisfiable else "\nUnsatisfiable classes: none")
              + "\n\n" + raw[:4000])
    return {"passed": consistent and not unsatisfiable,
            "consistent": consistent, "output": output,
            "unsatisfiable_classes": unsatisfiable, "report": report}


if __name__ == "__main__":
    mcp.run(transport="stdio")
