import json
import re
import xml.etree.ElementTree as ET
from typing import List

from models import Answer, Entity, RationaleEntry, SourceEntry

def str2bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("yes", "true", "t", "1")


def is_owl(onto_str: str = "") -> bool:
    try:
        root = ET.fromstring(onto_str)
        return root.tag.endswith("RDF")
    except ET.ParseError:
        return False


def format_cqs_prompt(cqs: list) -> str:
    if not isinstance(cqs, list):
        raise ValueError(
            "Competency questions file must be a JSON list of "
            "{'id': ..., 'value': ...} objects."
        )

    lines = []
    for idx, item in enumerate(cqs, start=1):
        if not isinstance(item, dict):
            raise ValueError(
                f"CQ entry at position {idx} is not an object: {item!r}"
            )
        cq_id = item.get("id") or f"CQ{idx}"
        cq_value = item.get("value")
        if cq_value is None:
            raise ValueError(
                f"CQ entry '{cq_id}' is missing the required 'value' field."
            )
        lines.append(f"{cq_id}: {cq_value}")
    return "\n".join(lines)

def parse_answer(response, agent_name: str) -> Answer:

    content = response.content

    if isinstance(content, Answer):
        answer = content
    elif isinstance(content, dict):
        answer = Answer(**content)
    elif isinstance(content, str):
        try:
            data = json.loads(content)
            answer = Answer(**data)
        except (json.JSONDecodeError, TypeError):
            print("  Warning: agent returned raw string, wrapping as fallback Answer")
            answer = Answer(
                reason="Raw string fallback — agent did not follow output_schema",
                OWL=[],
            )
    else:
        raise ValueError(f"Unexpected response.content type: {type(content)}")

    # Stamp the agent name onto every rationale entry — always correct server-side
    for entity in answer.OWL:
        for entry in entity.Rationale:
            entry.agent = agent_name

    return answer


def merge_rationale(old_owl_str: str, new_answer: Answer) -> Answer:

    old_rationale_map = {}
    old_source_map = {}

    try:
        root = ET.fromstring(old_owl_str)

        for elem in root.iter():
            about = elem.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about")
            if not about:
                continue
            name = about.split("#")[-1]

            # Extract vaem:rationale text
            rationale_elem = elem.find("{http://www.linkedmodel.org/schema/vaem#}rationale")
            if rationale_elem is not None and rationale_elem.text:
                entries: List[RationaleEntry] = []
                for line in rationale_elem.text.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    m = re.match(r"\[(.+?)\]\s+(.+?):\s+(.+)", line)
                    if m:
                        entries.append(
                            RationaleEntry(
                                agent=m.group(1), change=m.group(2), reason=m.group(3)
                            )
                        )
                    else:
                        entries.append(
                            RationaleEntry(
                                agent="Ontology Generation Agent",
                                change="unknown",
                                reason=line,
                            )
                        )
                old_rationale_map[name] = entries

            source_elem = elem.find("{http://purl.org/dc/elements/1.1/}source")
            if source_elem is not None and source_elem.text:
                entries_s: List[SourceEntry] = []
                for line in source_elem.text.strip().split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    m = re.match(r"\((\w+)\)\s+(.+)", line)
                    if m:
                        stype = m.group(1)
                        if stype not in ["competency_question", "pitfall", "error_message", "other"]:
                            stype = "other"
                        entries_s.append(SourceEntry(sourcetype=stype, content=m.group(2)))
                    else:
                        entries_s.append(SourceEntry(sourcetype="other", content=line))
                old_source_map[name] = entries_s

    except ET.ParseError as e:
        print(f"  Warning: could not parse old OWL for rationale merging: {e}")
        return new_answer

    for entity in new_answer.OWL:
        if entity.Name in old_rationale_map:
            old_entries = old_rationale_map[entity.Name]
            existing = {(e.agent, e.change) for e in entity.Rationale}
            new_old = [e for e in old_entries if (e.agent, e.change) not in existing]
            entity.Rationale = new_old + entity.Rationale

        if entity.Name in old_source_map:
            old_entries_s = old_source_map[entity.Name]
            existing_s = {(e.sourcetype, e.content) for e in entity.Source}
            new_old_s = [e for e in old_entries_s if (e.sourcetype, e.content) not in existing_s]
            entity.Source = new_old_s + entity.Source

    return new_answer
