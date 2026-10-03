import subprocess
import tempfile
from pathlib import Path
from typing import Tuple, Union
 
from rdflib import Graph
 

import subprocess
import tempfile
from pathlib import Path
from typing import Tuple, Union
 
from rdflib import Graph
 

    
def reason_ontology(
    onto_str: str,
    hermit_jar: Union[str, Path],
) -> Tuple[str, str]:

    hermit_jar = Path(hermit_jar)
    if not hermit_jar.exists():
        raise FileNotFoundError(
            f"HermiT jar not found at: {hermit_jar}. "
            f"Check `hermit.jar_path` in config.yaml."
        )
 
    ontology = Graph().parse(data=onto_str, format="xml")

    with tempfile.NamedTemporaryFile(
        suffix=".xml", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
        ontology.serialize(format="xml", destination=str(tmp_path))
 
    try:
        result = subprocess.run(
            ["java", "-jar", str(hermit_jar), "-k", str(tmp_path)],
            capture_output=True,
            text=True,
        )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
 
    return result.stdout, result.stderr
 