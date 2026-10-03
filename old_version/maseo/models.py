import re
from typing import List, Literal, Optional
 
from pydantic import BaseModel, Field, field_validator
 
OWLType = Literal[
    "owl:Class",
    "owl:ObjectProperty",
    "owl:DatatypeProperty",
]
 
class SourceEntry(BaseModel):
    sourcetype: Literal["competency_question", "pitfall", "error_message", "other"] = Field(
        description="The type of source that triggered this change"
    )
    content: str = Field(description="The actual competency question, pitfall ID/description, or error message")
 
 
class RationaleEntry(BaseModel):
    agent: Literal[
        "Ontology Generation Agent",
        "Syntax Repair Agent",
        "Logical Consistency Agent",
        "Pitfall Resolution Agent",
    ] = Field(description="The agent that made this change")
    change: str = Field(description="What was changed or added to this entity")
    reason: str = Field(description="Why this change was made")
 
 
class Entity(BaseModel):
    Type: OWLType = Field(
        description="The OWL type of the entity: owl:Class, owl:ObjectProperty, or owl:DatatypeProperty"
    )
    Name: str = Field(
        description="The local name (no spaces, camelCase). Will be combined with the base URI."
    )
    Comment: str = Field(
        description=(
            "The definition of the term, as plain text (no XML wrapper). "
            "It will be emitted as: "
            "<rdfs:comment xml:lang=\"en\">your text</rdfs:comment>."
        )
    )
    Label: str = Field(
        description=(
            "Human readable label, as plain text (no XML wrapper). "
            "It will be emitted as: "
            "<rdfs:label xml:lang=\"en\">your text</rdfs:label>."
        )
    )
    Rationale: List[RationaleEntry] = Field(
        default_factory=list,
        description=(
            "Ordered history of changes to this entity. Each entry records what changed and why. "
            "Always append new entries — never remove existing ones. "
            "Optional: leave empty if rationale tracking is not required."
        ),
    )
    Source: List[SourceEntry] = Field(
        default_factory=list,
        description=(
            "Ordered list of sources (competency questions, pitfalls, errors) that triggered creation "
            "or modification. Always append new entries, never remove existing ones. "
            "Optional: leave empty if source tracking is not required."
        ),
    )
    Domain: Optional[str] = None
    Range: Optional[str] = None
    Functional: Optional[str] = Field(
        default=None,
        description=(
            "Whether the property is functional. Accepts a boolean or a string; "
            "normalized to 'true' / 'false'."
        ),
    )
    Axiom: Optional[str] = Field(
        default=None,
        description=(
            "Optional raw RDF/XML fragment to embed directly inside the OWL entity element. "
            "For owl:Class this can include <rdfs:subClassOf>, <owl:disjointWith>, etc. "
            "For properties this can include <owl:inverseOf>, <rdfs:subPropertyOf>, etc."
        ),
    )
 
    @field_validator("Functional", mode="before")
    @classmethod
    def coerce_functional(cls, v):

        if v is None:
            return None
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return "true" if v else "false"
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "yes", "1", "t"):
                return "true"
            if s in ("false", "no", "0", "f", ""):
                return "false"
            return s 
        return str(v).lower()
 
    @field_validator("Domain", "Range", "Axiom", mode="before")
    @classmethod
    def coerce_optional_str(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            return v
        return str(v)
 
    @field_validator("Source", mode="before")
    @classmethod
    def coerce_source_entries(cls, v):
        if not isinstance(v, list):
            return [SourceEntry(sourcetype="other", content=str(v))]
        coerced = []
        for item in v:
            if isinstance(item, str):
                coerced.append(SourceEntry(sourcetype="other", content=item))
            elif isinstance(item, dict):
                coerced.append(
                    SourceEntry(
                        sourcetype=item.get("sourcetype") or item.get("type", "other"),
                        content=item.get("content", str(item)),
                    )
                )
            else:
                coerced.append(item)
        return coerced
 
    @field_validator("Rationale", mode="before")
    @classmethod
    def coerce_rationale_entries(cls, v):
        if not isinstance(v, list):
            return [RationaleEntry(agent="Ontology Generation Agent", change="unknown", reason=str(v))]
        coerced = []
        for item in v:
            if isinstance(item, str):
                coerced.append(
                    RationaleEntry(agent="Ontology Generation Agent", change="unknown", reason=item)
                )
            elif isinstance(item, dict):
                coerced.append(
                    RationaleEntry(
                        agent=item.get("agent", "Ontology Generation Agent"),
                        change=item.get("change", "unknown"),
                        reason=item.get("reason", str(item)),
                    )
                )
            else:
                coerced.append(item)
        return coerced
 
    def _serialize_rationale(self) -> str:
        if not self.Rationale:
            return "<vaem:rationale></vaem:rationale>"
        entries = "\n    ".join(
            f"[{entry.agent}] {entry.change}: {entry.reason}" for entry in self.Rationale
        )
        return f"<vaem:rationale>{entries}</vaem:rationale>"
 
    def _serialize_source(self) -> str:
        if not self.Source:
            return "<dc:source></dc:source>"
        entries = "\n    ".join(
            f"({entry.sourcetype}) {entry.content}" for entry in self.Source
        )
        return f"<dc:source>{entries}</dc:source>"
 
    @staticmethod
    def _resolve_uri(value: str, base_uri: str) -> str:

        v = value.strip()
        if not v:
            return base_uri
 
        if "://" in v or v.startswith("urn:"):
            return v
 
        if v.startswith("#") or v.startswith(":"):
            v = v[1:]
        return f"{base_uri}{v}"

    @staticmethod
    def _wrap_comment(value: str, lang: str = "en") -> str:
        v = (value or "").strip()
        if not v:
            return ""
        if v.startswith("<rdfs:comment") and v.endswith("</rdfs:comment>"):
            return v
        return f'<rdfs:comment xml:lang="{lang}">{v}</rdfs:comment>'

    @staticmethod
    def _wrap_label(value: str, lang: str = "en") -> str:
        v = (value or "").strip()
        if not v:
            return ""
        if v.startswith("<rdfs:label") and v.endswith("</rdfs:label>"):
            return v
        return f'<rdfs:label xml:lang="{lang}">{v}</rdfs:label>'

    def to_owl(self, base_uri: str) -> str:
        uri = self._resolve_uri(self.Name, base_uri)
        comment_xml = self._wrap_comment(self.Comment)
        label_xml = self._wrap_label(self.Label)
        rationale_xml = self._serialize_rationale() if self.Rationale else None
        source_xml = self._serialize_source() if self.Source else None
 
        if self.Type == "owl:Class":
            lines = [f'<owl:Class rdf:about="{uri}">']
            if comment_xml:
                lines.append(f"  {comment_xml}")
            if label_xml:
                lines.append(f"  {label_xml}")
            if rationale_xml:
                lines.append(f"  {rationale_xml}")
            if source_xml:
                lines.append(f"  {source_xml}")
            if self.Axiom:
                lines.append(f"  {self.Axiom}")
            lines.append("</owl:Class>")
 
        elif self.Type == "owl:ObjectProperty":
            lines = [f'<owl:ObjectProperty rdf:about="{uri}">']
            if comment_xml:
                lines.append(f"  {comment_xml}")
            if label_xml:
                lines.append(f"  {label_xml}")
            if rationale_xml:
                lines.append(f"  {rationale_xml}")
            if source_xml:
                lines.append(f"  {source_xml}")
            if self.Domain:
                lines.append(
                    f'  <rdfs:domain rdf:resource="{self._resolve_uri(self.Domain, base_uri)}"/>'
                )
            if self.Range:
                lines.append(
                    f'  <rdfs:range rdf:resource="{self._resolve_uri(self.Range, base_uri)}"/>'
                )
            if self.Functional and self.Functional.lower() == "true":
                lines.append(
                    '  <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#FunctionalProperty"/>'
                )
            if self.Axiom:
                lines.append(f"  {self.Axiom}")
            lines.append("</owl:ObjectProperty>")
 
        elif self.Type == "owl:DatatypeProperty":
            lines = [f'<owl:DatatypeProperty rdf:about="{uri}">']
            if comment_xml:
                lines.append(f"  {comment_xml}")
            if label_xml:
                lines.append(f"  {label_xml}")
            if rationale_xml:
                lines.append(f"  {rationale_xml}")
            if source_xml:
                lines.append(f"  {source_xml}")
            if self.Domain:
                lines.append(
                    f'  <rdfs:domain rdf:resource="{self._resolve_uri(self.Domain, base_uri)}"/>'
                )
            if self.Range:
                lines.append(
                    f'  <rdfs:range rdf:resource="{self._resolve_uri(self.Range, base_uri)}"/>'
                )
            if self.Functional and self.Functional.lower() == "true":
                lines.append(
                    '  <rdf:type rdf:resource="http://www.w3.org/2002/07/owl#FunctionalProperty"/>'
                )
            if self.Axiom:
                lines.append(f"  {self.Axiom}")
            lines.append("</owl:DatatypeProperty>")
 
        else:
            raise ValueError(f"Unsupported OWL type: {self.Type}")
 
        return "\n".join(lines)
 
class Answer(BaseModel):
    reason: str = Field(description="The reasoning process of the entire generation")
    OWL: List[Entity] = Field(description="List of ontology entities to be serialized as OWL")
 
    @staticmethod
    def _sanitize_uris(owl_text: str, base_uri: str) -> str:
        if not owl_text:
            return owl_text
 

        escaped_base = re.escape(base_uri)

        owl_text = re.sub(rf"({escaped_base})[#/]+", r"\1", owl_text)

        owl_text = re.sub(r"##+", "#", owl_text)
 

        def _fix_bare_hash(match):
            quote = match.group("q")
            local = match.group("local")
            return f'{match.group("attr")}={quote}{base_uri}{local}{quote}'
 
        owl_text = re.sub(
            r'(?P<attr>rdf:(?:resource|about))=(?P<q>["\'])#(?P<local>[^"\'<>#]+)(?P=q)',
            _fix_bare_hash,
            owl_text,
        )
 
        return owl_text
 
    def to_owl_document(self, base_uri: str) -> str:
        header = f"""<?xml version="1.0"?>
<rdf:RDF xml:base="{base_uri}"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:xsd="http://www.w3.org/2001/XMLSchema#"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:vaem="http://www.linkedmodel.org/schema/vaem#">
 
  <owl:Ontology rdf:about="{base_uri}"/>"""
 
        footer = "</rdf:RDF>"
 
        order = ["owl:Class", "owl:ObjectProperty", "owl:DatatypeProperty"]
        grouped = {t: [] for t in order}
        for entity in self.OWL:
            grouped[entity.Type].append(entity)
 
        sections = []
        for owl_type in order:
            entities = grouped[owl_type]
            if entities:
                comment = f"  <!-- {owl_type} declarations -->"
                block = "\n\n".join(entity.to_owl(base_uri) for entity in entities)
                sections.append(f"{comment}\n\n{block}")
 
        body = "\n\n".join(sections)
        document = f"{header}\n\n{body}\n\n{footer}"
        return self._sanitize_uris(document, base_uri)