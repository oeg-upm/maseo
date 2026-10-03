import json
import re
from typing import Any, Dict, List, Literal, Optional
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field, field_validator, model_validator

OWLType = Literal[
    "owl:Class",
    "owl:ObjectProperty",
    "owl:DatatypeProperty",
    "owl:NamedIndividual",
]

XSD = "http://www.w3.org/2001/XMLSchema#"
OWL_NS = "http://www.w3.org/2002/07/owl#"
NAME = re.compile(r"^[A-Za-z_][\w\-]*$")

# the closed list of datatype names the pipeline supports (see the
# prompts.common.datatypes block in config.yaml); every alias below maps
# onto exactly one of these nine canonical names
XSD_NAMES = ("string", "integer", "float", "double", "long", "boolean",
             "dateTime", "dateTimeStamp", "anyURI")

XSD_TYPES = {
    # canonical names
    "string": "string", "integer": "integer", "float": "float",
    "double": "double", "long": "long", "boolean": "boolean",
    "datetime": "dateTime", "datetimestamp": "dateTimeStamp",
    "anyuri": "anyURI",
    # aliases and OWL 2-excluded XSD types, mapped to the closest
    # supported name
    "int": "integer", "short": "integer", "byte": "integer",
    "nonnegativeinteger": "integer", "positiveinteger": "integer",
    "negativeinteger": "integer", "nonpositiveinteger": "integer",
    "unsignedint": "integer", "unsignedshort": "integer",
    "unsignedbyte": "integer", "unsignedlong": "long",
    "decimal": "float",
    "date": "dateTime", "time": "dateTime",
    "gyear": "integer", "gyearmonth": "dateTime",
    "gmonth": "integer", "gday": "integer",
    "literal": "string", "langstring": "string", "plainliteral": "string",
    "normalizedstring": "string", "token": "string",
}

AXIOM_TAGS = {
    "subclassof": "rdfs:subClassOf",
    "equivalentto": "owl:equivalentClass",
    "disjointwith": "owl:disjointWith",
    "subpropertyof": "rdfs:subPropertyOf",
    "inverseof": "owl:inverseOf",
    "domain": "rdfs:domain",
    "range": "rdfs:range",
}

CARDINALITIES = {
    "min": "owl:minQualifiedCardinality",
    "max": "owl:maxQualifiedCardinality",
    "exactly": "owl:qualifiedCardinality",
}

CHARACTERISTICS = {
    "functional": "Functional", "inversefunctional": "InverseFunctional",
    "transitive": "Transitive", "symmetric": "Symmetric",
    "asymmetric": "Asymmetric", "reflexive": "Reflexive",
    "irreflexive": "Irreflexive",
}

THEMIS_FORMS = [re.compile(p, re.I) for p in (
    r"^\S+ type \S+$",
    r"^\S+ subclassof \S+( and \S+)?$",
    r"^\S+ subclassof \S+ that \S+ some \S+$",
    r"^\S+ (domain|range) \S+$",
    r"^\S+ \S+ \S+$",
    r"^\S+ subclassof \S+ only \S+( or \S+)?$",
    r"^\S+ subclassof \S+ (min|max|exactly) \d+ \S+$",
    r"^\S+ disjointwith \S+$",
    r"^\S+ equivalentto \S+$",
    r"^\S+ characteristic symmetricproperty$",
)]


def string_list(v):
    if v is None:
        return []
    if not isinstance(v, list):
        v = [v]
    return [re.sub(r"\s+", " ", str(x)).strip() for x in v if str(x).strip()]


def normalize_key(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def remap_keys(data, fields, aliases=None):
    """Map the keys of a model reply onto canonical field names, ignoring
    case, spaces, hyphens and underscores. An exact canonical key wins
    over an alias; unknown keys are kept untouched."""
    if not isinstance(data, dict):
        return data
    lookup = {normalize_key(f): f for f in fields}
    for alias, field in (aliases or {}).items():
        lookup.setdefault(normalize_key(alias), field)
    out = {}
    for k, v in data.items():
        field = lookup.get(normalize_key(k))
        if field is None:
            out.setdefault(str(k), v)
        elif field == k or field not in out or not out[field]:
            out[field] = v
    return out


def unwrap_reply(data, key, entry_field=None, id_field="cq_id"):
    """Accept the expected {key: [entries]} shape as well as a bare list,
    a wrongly spelled key, or a dict keyed by id."""
    if isinstance(data, list):
        return {key: data}
    if not isinstance(data, dict):
        return data
    data = remap_keys(data, ["reason", key])
    if key not in data:
        for k, v in list(data.items()):
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                data[key] = v
                break
            if isinstance(v, dict) and v and all(
                    isinstance(x, (dict, list)) for x in v.values()):
                data[key] = v
                break
    entries = data.get(key)
    if isinstance(entries, dict):
        unpacked = []
        for entry_id, entry in entries.items():
            if isinstance(entry, dict):
                entry = dict(entry)
                entry.setdefault(id_field, entry_id)
            elif entry_field:
                entry = {id_field: entry_id, entry_field: entry}
            else:
                entry = {id_field: entry_id}
            unpacked.append(entry)
        data[key] = unpacked
    return data


ENTITY_TYPES = {
    "namedindividual": "owl:NamedIndividual",
    "individual": "owl:NamedIndividual",
    "instance": "owl:NamedIndividual",
    "class": "owl:Class",
    "objectproperty": "owl:ObjectProperty",
    "objectprop": "owl:ObjectProperty",
    "datatypeproperty": "owl:DatatypeProperty",
    "dataproperty": "owl:DatatypeProperty",
    "datatypeprop": "owl:DatatypeProperty",
}


def canonical_type(v):
    """The canonical owl: type literal for a model-written Type value,
    or None when the value maps to no modelable type."""
    key = re.sub(r"[^a-z]", "", str(v or "").lower())
    if key.startswith("owl"):
        key = key[3:]
    return ENTITY_TYPES.get(key)


CQ_ID_ALIASES = {"id": "cq_id", "cq": "cq_id", "question_id": "cq_id",
                 "question": "cq_id", "cq_number": "cq_id"}


def resolve_uri(value: str, base_uri: str) -> str:
    v = (value or "").strip()
    if "://" in v or v.startswith("urn:"):
        return v
    return base_uri + re.sub(r"\s+", "", v.lstrip("#:"))


def datatype_key(name: str) -> str:
    key = re.sub(r"\s+", "", str(name or "").lower())
    for prefix in ("xsd:", "xs:", "rdfs:", "rdf:"):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    return key


def is_datatype(name: str) -> bool:
    return datatype_key(name) in XSD_TYPES


def resolve_datatype(value: str, base_uri: str) -> str:
    key = datatype_key(value)
    if key in XSD_TYPES:
        return XSD + XSD_TYPES[key]
    return resolve_uri(value, base_uri)


class Reasoned(BaseModel):
    """Base for agent replies: an optional reasoning text. The agent may
    return its result with or without a 'reason' field - when absent or
    null it stays an empty string, when given its content is kept."""

    reason: str = Field(
        default="",
        description="The reasoning behind the returned result; "
                    "may be omitted",
    )

    @field_validator("reason", mode="before")
    @classmethod
    def coerce_reason(cls, v):
        return "" if v is None else str(v)


class CQTerms(BaseModel):
    cq_id: str = Field(default="", description="The competency question id, exactly as given")
    classes: List[str] = Field(default_factory=list, description="Class names in CamelCase")
    object_properties: List[str] = Field(default_factory=list, description="Object property names in camelCase")
    data_properties: List[str] = Field(default_factory=list, description="Data property names in camelCase")
    individuals: List[str] = Field(default_factory=list, description="Named values of the domain vocabulary in CamelCase, each an instance of one of the classes")
    instances: List[str] = Field(default_factory=list, description="Particular named things the question supplies as its given subject or example, in CamelCase, each an instance of one of the classes")

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data):
        return remap_keys(data, ["cq_id", "classes", "object_properties",
                                 "data_properties", "individuals",
                                 "instances"],
                          {**CQ_ID_ALIASES,
                           "named_individuals": "individuals",
                           "example_instances": "instances",
                           "examples": "instances"})

    @field_validator("cq_id", mode="before")
    @classmethod
    def coerce_id(cls, v):
        return str(v if v is not None else "").strip()

    @field_validator("classes", "object_properties", "data_properties",
                     "individuals", "instances", mode="before")
    @classmethod
    def coerce_lists(cls, v):
        return string_list(v)


class Terms(Reasoned):
    terms: List[CQTerms] = Field(description="One entry per competency question")

    @model_validator(mode="before")
    @classmethod
    def unwrap(cls, data):
        return unwrap_reply(data, "terms")

    def for_cq(self, cq_id: str) -> Optional[CQTerms]:
        return next((t for t in self.terms if t.cq_id == cq_id), None)


ATOMIC_ID_ALIASES = {"id": "atomic_cq_id", "atomic_id": "atomic_cq_id",
                     "atomiccqid": "atomic_cq_id",
                     "sub_cq_id": "atomic_cq_id"}


class AtomicCQ(BaseModel):
    """One atomic competency question written by the CQAtomization Agent."""

    atomic_cq_id: str = Field(default="", description="The atomic question id, '<cq_id>_a<n>'")
    atomic_cq: str = Field(default="", description="The atomic competency question text")
    template: str = Field(default="", description="The CLaRO template the question instantiates, or 'none'")

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data):
        return remap_keys(data, ["atomic_cq_id", "atomic_cq", "template"],
                          {**ATOMIC_ID_ALIASES, "question": "atomic_cq",
                           "text": "atomic_cq", "cq": "atomic_cq",
                           "value": "atomic_cq",
                           "template_id": "template",
                           "pattern": "template"})

    @field_validator("atomic_cq_id", "atomic_cq", "template",
                     mode="before")
    @classmethod
    def coerce_text(cls, v):
        return str(v if v is not None else "").strip()


class CQAtomization(BaseModel):
    cq_id: str = Field(default="", description="The original competency question id, exactly as given")
    question_type: str = Field(default="domain", description="'domain' when the question asks about the subject matter itself, 'specification' when it asks how something is expressed, defined, provided or composed")
    recomposition: str = Field(default="", description="How the atomic answers combine into the original answer")
    atomic_cqs: List[AtomicCQ] = Field(default_factory=list, description="The atomic questions, in answering order")

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data):
        return remap_keys(data, ["cq_id", "question_type", "recomposition",
                                 "atomic_cqs"],
                          {**CQ_ID_ALIASES,
                           "type": "question_type",
                           "cq_type": "question_type",
                           "questiontype": "question_type",
                           "question_kind": "question_type",
                           "kind": "question_type",
                           "recombination": "recomposition",
                           "composition": "recomposition",
                           "atomic": "atomic_cqs",
                           "atomic_questions": "atomic_cqs",
                           "atomic_cq_mapping": "atomic_cqs"})

    @field_validator("cq_id", "recomposition", mode="before")
    @classmethod
    def coerce_text(cls, v):
        return str(v if v is not None else "").strip()

    @field_validator("question_type", mode="before")
    @classmethod
    def coerce_question_type(cls, v):
        # anything the model writes collapses onto the two labels the
        # downstream prompts read; an unrecognised label is treated as a
        # domain question, the safe default that adds no restriction
        text = str(v if v is not None else "").strip().lower()
        if text.startswith(("spec", "design", "meta", "model")):
            return "specification"
        return "domain"

    @field_validator("atomic_cqs", mode="before")
    @classmethod
    def coerce_atomic(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            v = [v]
        return [{"atomic_cq": x} if isinstance(x, str) else x for x in v]


class Atomization(Reasoned):
    atomization: List[CQAtomization] = Field(description="One entry per competency question")

    @model_validator(mode="before")
    @classmethod
    def unwrap(cls, data):
        if isinstance(data, dict):
            data = remap_keys(data, ["reason", "atomization"],
                              {"atomizations": "atomization",
                               "atomic_cqs": "atomization",
                               "decomposition": "atomization"})
        return unwrap_reply(data, "atomization")

    def for_cq(self, cq_id: str) -> Optional[CQAtomization]:
        return next((a for a in self.atomization if a.cq_id == cq_id), None)

    def atomic_ids(self) -> List[str]:
        return [a.atomic_cq_id for entry in self.atomization
                for a in entry.atomic_cqs if a.atomic_cq_id]


class AtomicTerms(BaseModel):
    """The terms of one atomic competency question."""

    atomic_cq_id: str = Field(default="", description="The atomic question id, exactly as given")
    atomic_cq: str = Field(default="", description="The atomic question text; may be omitted")
    classes: List[str] = Field(default_factory=list, description="Class names in CamelCase")
    object_properties: List[str] = Field(default_factory=list, description="Object property names in camelCase")
    data_properties: List[str] = Field(default_factory=list, description="Data property names in camelCase")
    individuals: List[str] = Field(default_factory=list, description="Named values of the domain vocabulary in CamelCase, each an instance of one of the classes")
    instances: List[str] = Field(default_factory=list, description="Particular named things the question supplies as its given subject or example, in CamelCase, each an instance of one of the classes")

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data):
        return remap_keys(data, ["atomic_cq_id", "atomic_cq", "classes",
                                 "object_properties", "data_properties",
                                 "individuals", "instances"],
                          {**ATOMIC_ID_ALIASES, "question": "atomic_cq",
                           "cq": "atomic_cq",
                           "named_individuals": "individuals",
                           "example_instances": "instances",
                           "examples": "instances"})

    @field_validator("atomic_cq_id", "atomic_cq", mode="before")
    @classmethod
    def coerce_text(cls, v):
        return str(v if v is not None else "").strip()

    @field_validator("classes", "object_properties", "data_properties",
                     "individuals", "instances", mode="before")
    @classmethod
    def coerce_lists(cls, v):
        return string_list(v)


class AtomicTermsReply(Reasoned):
    """The TermExtraction Agent's reply: one entry per atomic question."""

    terms: List[AtomicTerms] = Field(description="One entry per atomic competency question")

    @model_validator(mode="before")
    @classmethod
    def unwrap(cls, data):
        return unwrap_reply(data, "terms", id_field="atomic_cq_id")

    def for_atomic(self, atomic_cq_id: str) -> Optional[AtomicTerms]:
        return next((t for t in self.terms
                     if t.atomic_cq_id == atomic_cq_id), None)


class CQMapping(BaseModel):
    """One original question with its atomic questions and their terms."""

    cq_id: str = Field(default="", description="The original competency question id, exactly as given")
    cq: str = Field(default="", description="The original question text; may be omitted")
    atomic_cq_mapping: List[AtomicTerms] = Field(default_factory=list, description="The atomic questions with their terms")

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data):
        return remap_keys(data, ["cq_id", "cq", "atomic_cq_mapping"],
                          {"id": "cq_id", "question_id": "cq_id",
                           "question": "cq", "value": "cq",
                           "atomic_cqs": "atomic_cq_mapping",
                           "atomic_mapping": "atomic_cq_mapping",
                           "mapping": "atomic_cq_mapping",
                           "atomic_terms": "atomic_cq_mapping"})

    @field_validator("cq_id", "cq", mode="before")
    @classmethod
    def coerce_text(cls, v):
        return str(v if v is not None else "").strip()

    @field_validator("atomic_cq_mapping", mode="before")
    @classmethod
    def coerce_mapping(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            v = [v]
        return v


class TermMap(Reasoned):
    """The grouped term mapping returned by the TermIdentification and
    TermRefinement Agents: every original question with its atomic
    questions and their (filtered, then refined) terms."""

    terms: List[CQMapping] = Field(description="One entry per original competency question")

    @model_validator(mode="before")
    @classmethod
    def unwrap(cls, data):
        return unwrap_reply(data, "terms")

    def for_cq(self, cq_id: str) -> Optional[CQMapping]:
        return next((t for t in self.terms if t.cq_id == cq_id), None)

    def to_terms(self) -> Terms:
        """The per-question union of the atomic term lists, in the flat
        shape the validation tools and the OntoGeneration context use."""
        entries = []
        for m in self.terms:
            union = {"classes": [], "object_properties": [],
                     "data_properties": [], "individuals": [],
                     "instances": []}
            for a in m.atomic_cq_mapping:
                for field in union:
                    for value in getattr(a, field):
                        if value not in union[field]:
                            union[field].append(value)
            entries.append(CQTerms(cq_id=m.cq_id, **union))
        return Terms(terms=entries)


class CQAxioms(BaseModel):
    cq_id: str = Field(default="", description="The competency question id, exactly as given")
    axioms: List[str] = Field(default_factory=list, description="Atomic axiom statements '<Subject> <predicate> <Object>', one statement per string")

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data):
        return remap_keys(data, ["cq_id", "axioms"],
                          {**CQ_ID_ALIASES, "axiom": "axioms",
                           "statements": "axioms"})

    @field_validator("cq_id", mode="before")
    @classmethod
    def coerce_id(cls, v):
        return str(v if v is not None else "").strip()

    @field_validator("axioms", mode="before")
    @classmethod
    def coerce_axioms(cls, v):
        return string_list(v)


class Axioms(Reasoned):
    axioms: List[CQAxioms] = Field(description="One entry per competency question")

    @model_validator(mode="before")
    @classmethod
    def unwrap(cls, data):
        return unwrap_reply(data, "axioms", entry_field="axioms")

    def for_cq(self, cq_id: str) -> Optional[CQAxioms]:
        return next((a for a in self.axioms if a.cq_id == cq_id), None)


class CQTests(BaseModel):
    cq_id: str = Field(default="", description="The competency question id, exactly as given")
    tests: List[str] = Field(default_factory=list, description="Themis test expressions, one self-contained statement per string")

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data):
        return remap_keys(data, ["cq_id", "tests"],
                          {**CQ_ID_ALIASES, "test": "tests"})

    @field_validator("cq_id", mode="before")
    @classmethod
    def coerce_id(cls, v):
        return str(v if v is not None else "").strip()

    @field_validator("tests", mode="before")
    @classmethod
    def coerce_tests(cls, v):
        return string_list(v)

    @field_validator("tests")
    @classmethod
    def keep_themis_forms(cls, v):
        return [t for t in v if any(f.match(t) for f in THEMIS_FORMS)]


class Tests(Reasoned):
    tests: List[CQTests] = Field(description="One entry per competency question")

    @model_validator(mode="before")
    @classmethod
    def unwrap(cls, data):
        return unwrap_reply(data, "tests", entry_field="tests")

    def for_cq(self, cq_id: str) -> Optional[CQTests]:
        return next((t for t in self.tests if t.cq_id == cq_id), None)


TOOL_SOURCETYPES = {
    "syntax_check": "error_message",
    "cq_coverage": "error_message",
    "oops_scan": "pitfall",
    "hermit_consistency": "error_message",
    "themis_test": "error_message",
}


class SourceEntry(BaseModel):
    sourcetype: Literal["competency_question", "pitfall", "error_message", "other"] = Field(
        default="other",
        description="The type of source that triggered this entity or change",
    )
    tool: Optional[str] = Field(default=None, description="The validation tool that reported this pitfall or error")
    content: str = Field(default="", description="The competency question id, pitfall description, or error message")
    event: Optional[str] = Field(default=None, description="Loop position of the correction event, stamped by the framework")

    @field_validator("event", mode="before")
    @classmethod
    def coerce_event(cls, v):
        s = str(v).strip() if v is not None else ""
        return s or None

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data):
        return remap_keys(data, ["sourcetype", "tool", "content"],
                          {"type": "sourcetype", "source_type": "sourcetype",
                           "text": "content", "message": "content",
                           "value": "content"})

    @field_validator("sourcetype", mode="before")
    @classmethod
    def coerce_sourcetype(cls, v):
        s = str(v or "").strip().lower().replace(" ", "_").replace("-", "_")
        if "competency" in s or s in ("cq", "cqs", "question"):
            return "competency_question"
        if "pitfall" in s or s == "oops":
            return "pitfall"
        if s in ("error", "error_message", "failing_test", "test", "themis",
                 "hermit", "syntax", "warning"):
            return "error_message"
        return s if s in ("competency_question", "pitfall", "error_message") else "other"

    @field_validator("content", mode="before")
    @classmethod
    def coerce_content(cls, v):
        return "" if v is None else str(v)

    @field_validator("tool", mode="before")
    @classmethod
    def coerce_tool(cls, v):
        s = str(v).strip() if v is not None else ""
        return s or None


class RationaleEntry(BaseModel):
    agent: str = Field(default="OntoGeneration Agent", description="The agent that made this change")
    tool: Optional[str] = Field(default=None, description="The validation tool whose report triggered this change")
    change: str = Field(default="", description="What was changed or added on this entity")
    reason: str = Field(default="", description="Why this change was made")
    event: Optional[str] = Field(default=None, description="Loop position of the correction event, stamped by the framework")

    @field_validator("event", mode="before")
    @classmethod
    def coerce_event(cls, v):
        s = str(v).strip() if v is not None else ""
        return s or None

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data):
        return remap_keys(data, ["agent", "tool", "change", "reason"],
                          {"why": "reason", "what": "change",
                           "modification": "change"})

    @field_validator("agent", "change", "reason", mode="before")
    @classmethod
    def coerce_text(cls, v):
        return "" if v is None else str(v)

    @field_validator("tool", mode="before")
    @classmethod
    def coerce_tool(cls, v):
        s = str(v).strip() if v is not None else ""
        return s or None


def restriction_xml(tag, prop, quant, number, filler, base_uri):
    filler_uri = (resolve_datatype(filler, base_uri) if is_datatype(filler)
                  else resolve_uri(filler, base_uri))
    lines = [f"<{tag}>", "    <owl:Restriction>",
             f'      <owl:onProperty rdf:resource="{resolve_uri(prop, base_uri)}"/>']
    if quant == "some":
        lines.append(f'      <owl:someValuesFrom rdf:resource="{filler_uri}"/>')
    elif quant == "only":
        lines.append(f'      <owl:allValuesFrom rdf:resource="{filler_uri}"/>')
    else:
        card = CARDINALITIES[quant]
        on = "owl:onDataRange" if is_datatype(filler) else "owl:onClass"
        lines.append(f'      <{card} rdf:datatype="{XSD}nonNegativeInteger">{number}</{card}>')
        lines.append(f'      <{on} rdf:resource="{filler_uri}"/>')
    lines += ["    </owl:Restriction>", f"  </{tag}>"]
    return "\n".join(lines)


def axiom_statement(axiom: str, base_uri: str) -> Optional[str]:
    toks = axiom.split()
    if len(toks) < 3:
        return None
    rest = toks[1:]
    pred = rest[0].lower()
    if pred == "is" and len(rest) == 2:
        kind = CHARACTERISTICS.get(rest[1].lower().replace("property", ""))
        return f'<rdf:type rdf:resource="{OWL_NS}{kind}Property"/>' if kind else None
    if pred in AXIOM_TAGS and len(rest) == 2 and NAME.match(rest[1]):
        target = (resolve_datatype(rest[1], base_uri) if pred == "range"
                  else resolve_uri(rest[1], base_uri))
        return f'<{AXIOM_TAGS[pred]} rdf:resource="{target}"/>'
    tag = AXIOM_TAGS.get(pred) if pred in ("subclassof", "equivalentto") else None
    if tag and len(rest) == 4 and rest[2].lower() in ("some", "only") \
            and NAME.match(rest[3]):
        return restriction_xml(tag, rest[1], rest[2].lower(), None, rest[3], base_uri)
    if tag and len(rest) == 5 and rest[2].lower() in CARDINALITIES \
            and rest[3].isdigit() and NAME.match(rest[4]):
        return restriction_xml(tag, rest[1], rest[2].lower(), rest[3], rest[4], base_uri)
    if pred not in AXIOM_TAGS and pred != "is" and NAME.match(rest[0]):
        if len(rest) == 3 and rest[1].lower() in ("some", "only") and NAME.match(rest[2]):
            return restriction_xml("rdfs:subClassOf", rest[0], rest[1].lower(),
                                   None, rest[2], base_uri)
        if len(rest) == 4 and rest[1].lower() in CARDINALITIES \
                and rest[2].isdigit() and NAME.match(rest[3]):
            return restriction_xml("rdfs:subClassOf", rest[0], rest[1].lower(),
                                   rest[2], rest[3], base_uri)
        if len(rest) == 2 and NAME.match(rest[1]):
            return restriction_xml("rdfs:subClassOf", rest[0], "some",
                                   None, rest[1], base_uri)
    return None


ENTITY_FIELDS = ["Type", "Name", "Comment", "Label", "Rationale", "Source",
                 "Domain", "Range", "InstanceOf", "PropertyCharacteristics",
                 "Axioms"]

ENTITY_ALIASES = {"uri": "Name", "iri": "Name", "entity_type": "Type",
                  "owl_type": "Type", "description": "Comment",
                  "definition": "Comment", "rationales": "Rationale",
                  "sources": "Source", "axiom": "Axioms",
                  "functional": "PropertyCharacteristics",
                  "is_functional": "PropertyCharacteristics",
                  "characteristic": "PropertyCharacteristics",
                  "characteristics": "PropertyCharacteristics",
                  "property_characteristic": "PropertyCharacteristics",
                  "instance_of": "InstanceOf", "member_of": "InstanceOf",
                  "individual_of": "InstanceOf", "belongs_to": "InstanceOf"}


class Entity(BaseModel):
    Type: OWLType = Field(description="The OWL type of the entity")
    Name: str = Field(description="The local name, no spaces; classes in CamelCase, properties in camelCase")
    Comment: str = Field(default="", description="A short plain-text definition of the term")
    Label: str = Field(default="", description="A human readable label")
    Rationale: List[RationaleEntry] = Field(
        default_factory=list,
        description="Ordered history of changes to this entity. Always append new entries, never remove existing ones.",
    )
    Source: List[SourceEntry] = Field(
        default_factory=list,
        description="The competency questions, pitfalls or errors that triggered this entity. Always append new entries, never remove existing ones.",
    )
    Domain: Optional[str] = Field(default=None, description="Domain class, for properties only")
    Range: Optional[str] = Field(default=None, description="Range class for object properties, plain XSD datatype name for data properties")
    InstanceOf: Optional[str] = Field(default=None, description="The class this named individual is an instance of, for owl:NamedIndividual only")
    PropertyCharacteristics: List[str] = Field(
        default_factory=list,
        description="Object property traits, each one of FunctionalProperty, "
                    "InverseFunctionalProperty, TransitiveProperty, "
                    "SymmetricProperty, AsymmetricProperty, ReflexiveProperty, "
                    "IrreflexiveProperty, or 'InverseOf otherProperty'",
    )
    Axioms: List[str] = Field(
        default_factory=list,
        description="Atomic axiom statements '<Subject> <predicate> <Object>' materialized on this entity",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data):
        data = remap_keys(data, ENTITY_FIELDS, ENTITY_ALIASES)
        if isinstance(data, dict) \
                and canonical_type(data.get("Type")) in (
                    "owl:ObjectProperty", "owl:DatatypeProperty") \
                and "PropertyCharacteristics" not in data:
            raise ValueError(
                "every property entity must state PropertyCharacteristics; "
                "write null when the property has none")
        if isinstance(data, dict) \
                and canonical_type(data.get("Type")) == "owl:NamedIndividual":
            member = data.get("InstanceOf") or data.get("instance_of") \
                or data.get("member_of")
            if isinstance(member, list):
                member = member[0] if member else None
            if not str(member if member is not None else "").strip():
                raise ValueError(
                    "every owl:NamedIndividual must state InstanceOf, the "
                    "class it is an instance of")
        if isinstance(data, dict) \
                and canonical_type(data.get("Type")) == "owl:DatatypeProperty":
            rng = data.get("Range")
            if isinstance(rng, list):
                rng = rng[0] if rng else None
            if not str(rng if rng is not None else "").strip():
                raise ValueError(
                    "every data property must state a Range datatype, one "
                    "of: " + ", ".join(XSD_NAMES))
        return data

    @model_validator(mode="after")
    def canonical_range(self):
        # normalize a data property's range onto the closed datatype list
        # (e.g. 'xsd:date' -> 'dateTime'), so the same property keeps an
        # identical range across CQs and iterations
        if self.Type == "owl:DatatypeProperty" and self.Range \
                and is_datatype(self.Range):
            self.Range = XSD_TYPES[datatype_key(self.Range)]
        return self

    @field_validator("Type", mode="before")
    @classmethod
    def coerce_type(cls, v):
        # normalize variants such as 'Class', 'owl:class', 'OWL:ObjectProperty'
        # or 'DataProperty' to the canonical owl: literal
        return canonical_type(v) or v

    @field_validator("Name", mode="before")
    @classmethod
    def coerce_name(cls, v):
        s = str(v if v is not None else "").strip()
        if "://" in s:
            s = re.split(r"[#/]", s.rstrip("#/"))[-1]
        s = re.sub(r"\s+", "", s.lstrip("#:"))
        if not s:
            raise ValueError("Entity Name must not be empty")
        return s

    @field_validator("Comment", "Label", mode="before")
    @classmethod
    def coerce_text(cls, v):
        return "" if v is None else str(v)

    @field_validator("Domain", "Range", "InstanceOf", mode="before")
    @classmethod
    def coerce_optional(cls, v):
        if isinstance(v, list):
            v = v[0] if v else None
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("PropertyCharacteristics", mode="before")
    @classmethod
    def coerce_characteristics(cls, v):
        if v is None or v is False:
            return []
        if v is True:
            return ["FunctionalProperty"]
        if not isinstance(v, list):
            v = [v]
        out = []
        for item in v:
            text = str(item).strip()
            m = re.match(r"(?:owl:)?inverse\s*of\s+[#:]?(\S+)", text, re.I)
            if m:
                entry = f"InverseOf {m.group(1)}"
                if entry not in out:
                    out.append(entry)
                continue
            key = re.sub(r"[^a-z]", "", text.lower())
            key = key[3:] if key.startswith("owl") else key
            key = key[:-8] if key.endswith("property") else key
            name = CHARACTERISTICS.get(key)
            if name and f"{name}Property" not in out:
                out.append(f"{name}Property")
        return out

    @field_validator("Axioms", mode="before")
    @classmethod
    def coerce_axioms(cls, v):
        if isinstance(v, list):
            v = [x.get("axioms", "") if isinstance(x, dict) else x for x in v]
        return string_list(v)

    @field_validator("Source", mode="before")
    @classmethod
    def coerce_source(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            v = [v]
        coerced = []
        for item in v:
            if isinstance(item, str):
                for line in item.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    m = re.match(r"\(\s*(\w+)\s*\)\s*(.*)", line)
                    if m and m.group(1).lower() in TOOL_SOURCETYPES:
                        tool = m.group(1).lower()
                        coerced.append(SourceEntry(
                            sourcetype=TOOL_SOURCETYPES[tool], tool=tool,
                            content=m.group(2)))
                    elif m:
                        coerced.append(SourceEntry(sourcetype=m.group(1),
                                                   content=m.group(2)))
                    else:
                        coerced.append(SourceEntry(sourcetype="other",
                                                   content=line))
            else:
                coerced.append(item)
        return coerced

    @field_validator("Rationale", mode="before")
    @classmethod
    def coerce_rationale(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            v = [v]
        pattern = re.compile(r"\[([^\]]+)\]\s*([^:]*):\s*(.*)")
        coerced = []
        for item in v:
            if isinstance(item, str):
                for line in item.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    m = pattern.match(line)
                    while m and m.group(2).strip().lower() in ("", "unknown") \
                            and pattern.match(m.group(3)):
                        m = pattern.match(m.group(3))
                    if m:
                        head = [p.strip() for p in m.group(1).split("|")]
                        tool, event = None, None
                        for part in head[1:]:
                            if re.search(r"loop_rounds|tool_retries|"
                                         r"verify_rounds|iteration",
                                         part, re.I):
                                event = part
                            elif tool is None:
                                tool = part
                        coerced.append(RationaleEntry(
                            agent=head[0], tool=tool, event=event,
                            change=m.group(2).strip() or "unknown",
                            reason=m.group(3)))
                    else:
                        # free-text line without a 'change: reason' split -
                        # drop any model-made '[...]' tag, keep the text
                        text = re.sub(r"^(?:\[[^\]]*\]\s*)+", "", line).strip()
                        coerced.append(RationaleEntry(change=text or line,
                                                      reason=""))
            else:
                coerced.append(item)
        return coerced

    def _rationale_xml(self) -> str:
        entries = []
        for r in self.Rationale:
            head = r.agent
            if r.event:
                head += f" | {r.event}"
            if r.tool:
                head += f" | {r.tool}"
            entries.append(f"[{escape(head)}] " + escape(r.change)
                           + (f": {escape(r.reason)}" if r.reason.strip()
                              else ""))
        return "<vaem:rationale>" + "\n    ".join(entries) + "</vaem:rationale>"

    def _source_xml(self, cq_texts, cq_only=False) -> str:
        entries = []
        for s in self.Source:
            if cq_only and s.sourcetype != "competency_question":
                continue
            content = s.content.strip()
            if s.sourcetype == "competency_question" and cq_texts \
                    and content in cq_texts and cq_texts[content]:
                content = f"{content}: {cq_texts[content]}"
            entries.append(f"({escape(s.tool) if s.tool else s.sourcetype}) "
                           f"{escape(content)}")
        if not entries:
            return ""
        return "<dc:source>" + "\n    ".join(entries) + "</dc:source>"

    def to_owl(self, base_uri: str, cq_texts=None) -> str:
        uri = resolve_uri(self.Name, base_uri)
        lines = [f'<{self.Type} rdf:about="{uri}">']
        if self.Comment.strip():
            lines.append(f'  <rdfs:comment>{escape(self.Comment.strip())}</rdfs:comment>')
        if self.Label.strip():
            lines.append(f'  <rdfs:label>{escape(self.Label.strip())}</rdfs:label>')
        if self.Rationale:
            lines.append(f"  {self._rationale_xml()}")
        if self.Source:
            source = self._source_xml(cq_texts)
            if source:
                lines.append(f"  {source}")
        if self.Type == "owl:NamedIndividual" and self.InstanceOf:
            lines.append('  <rdf:type rdf:resource='
                         f'"{resolve_uri(self.InstanceOf, base_uri)}"/>')
        if self.Type in ("owl:ObjectProperty", "owl:DatatypeProperty"):
            if self.Domain:
                lines.append(f'  <rdfs:domain rdf:resource="{resolve_uri(self.Domain, base_uri)}"/>')
            if self.Range:
                rng = (resolve_datatype(self.Range, base_uri)
                       if self.Type == "owl:DatatypeProperty"
                       else resolve_uri(self.Range, base_uri))
                lines.append(f'  <rdfs:range rdf:resource="{rng}"/>')
            for trait in self.PropertyCharacteristics:
                if trait.startswith("InverseOf "):
                    if self.Type == "owl:ObjectProperty":
                        target = resolve_uri(trait.split(None, 1)[1], base_uri)
                        lines.append(f'  <owl:inverseOf rdf:resource="{target}"/>')
                elif self.Type == "owl:ObjectProperty" \
                        or trait == "FunctionalProperty":
                    lines.append(f'  <rdf:type rdf:resource="{OWL_NS}{trait}"/>')
        disjoints, notes = [], []
        for axiom in self.Axioms:
            statement = axiom_statement(axiom, base_uri)
            if statement and f"  {statement}" not in lines:
                lines.append(f"  {statement}")
            toks = axiom.split()
            if len(toks) == 3 and toks[1].lower() == "disjointwith":
                disjoints.append(toks[2])
            else:
                text = " ".join(toks[1:]) if toks and toks[0] == self.Name else axiom
                notes.append(text.replace("--", "-"))
        if disjoints:
            lines.append(f"  <!-- Axiom: Disjoint with {', '.join(disjoints)} -->")
        for note in notes:
            lines.append(f"  <!-- Axiom: {note} -->")
        lines.append(f"</{self.Type}>")
        return "\n".join(lines)


class Answer(Reasoned):
    OWL: List[Entity] = Field(description="Every entity of the ontology")

    @model_validator(mode="before")
    @classmethod
    def unwrap(cls, data):
        if isinstance(data, dict):
            data = remap_keys(data, ["reason", "OWL", "Removed"],
                              {"entities": "OWL", "entity_list": "OWL",
                               "owl_entities": "OWL", "deleted": "Removed",
                               "removed_entities": "Removed"})
        return unwrap_reply(data, "OWL", id_field="Name")

    @field_validator("OWL", mode="before")
    @classmethod
    def drop_unmodelable(cls, v):
        # keep every entity the serializer can model; drop the rest with a
        # terminal note instead of failing the whole reply: the echoed
        # <owl:Ontology> document header (written by the pipeline itself),
        # entities of unknown Type, and entities without a Name
        if not isinstance(v, list):
            return v
        kept = []
        for item in v:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            item = remap_keys(item, ENTITY_FIELDS, ENTITY_ALIASES)
            raw_type = item.get("Type", "")
            name = str(item.get("Name") or "").strip()
            if normalize_key(raw_type) in ("owlontology", "ontology"):
                print(f"Note: dropped owl:Ontology entry '{name}' from the "
                      "reply; the pipeline writes the ontology header itself")
                continue
            ctype = canonical_type(raw_type)
            if ctype is None:
                print(f"Note: dropped entity '{name}' with unsupported "
                      f"Type '{raw_type}'")
                continue
            if not name:
                print(f"Note: dropped {ctype} entity without a Name")
                continue
            item["Type"] = ctype
            kept.append(item)
        return kept
    Removed: List[str] = Field(
        default_factory=list,
        description="Names of entities deliberately deleted from the previous version",
    )

    @field_validator("Removed", mode="before")
    @classmethod
    def coerce_removed(cls, v):
        return string_list(v)

    @staticmethod
    def _sanitize_uris(owl_text: str, base_uri: str) -> str:
        if not owl_text:
            return owl_text
        owl_text = re.sub(rf"({re.escape(base_uri)})[#/]+", r"\1", owl_text)
        owl_text = re.sub(r"##+", "#", owl_text)
        owl_text = re.sub(
            r'(?P<attr>rdf:(?:resource|about))=(?P<q>["\'])#(?P<local>[^"\'<>#]+)(?P=q)',
            lambda m: f'{m.group("attr")}={m.group("q")}{base_uri}{m.group("local")}{m.group("q")}',
            owl_text,
        )
        return owl_text

    def to_owl_document(self, base_uri: str, cq_texts=None) -> str:
        # the plain ontology document; provenance is injected into a
        # copy of this file at the end of the run by add_prov_generic.py
        header = f"""<?xml version="1.0"?>
<rdf:RDF xml:base="{base_uri}"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:xsd="http://www.w3.org/2001/XMLSchema#"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:vaem="http://www.linkedmodel.org/schema/vaem#">

  <owl:Ontology rdf:about="{base_uri}"/>"""
        order = ["owl:Class", "owl:ObjectProperty", "owl:DatatypeProperty",
                 "owl:NamedIndividual"]
        grouped = {t: [] for t in order}
        for entity in self.OWL:
            grouped[entity.Type].append(entity)
        sections = []
        for owl_type in order:
            if grouped[owl_type]:
                block = "\n\n".join(e.to_owl(base_uri, cq_texts)
                                     for e in grouped[owl_type])
                sections.append(f"  <!-- {owl_type} declarations -->\n\n{block}")
        document = f"{header}\n\n" + "\n\n".join(sections) + "\n\n</rdf:RDF>"
        return self._sanitize_uris(document, base_uri)


def stamp_answer(answer: Answer, agent_name: str, tool: str = None,
                 event: str = None) -> Answer:
    for entity in answer.OWL:
        for entry in entity.Rationale:
            entry.agent = agent_name
            entry.tool = tool
            entry.event = event
        if tool:
            for s in entity.Source:
                if s.sourcetype in ("pitfall", "error_message"):
                    if not s.tool:
                        s.tool = tool
                    if s.tool == tool and s.event is None:
                        s.event = event
    return answer


def rationale_key(r: RationaleEntry) -> tuple:
    return (r.change.strip().lower(), r.reason.strip().lower())


def source_key(s: SourceEntry) -> tuple:
    content = s.content.strip()
    if s.sourcetype == "competency_question":
        content = content.split(":")[0].strip()
    return (s.sourcetype, content.lower())


def merge_answers(old: Answer, new: Answer) -> Answer:
    old_rationale = {e.Name: list(e.Rationale) for e in old.OWL}
    old_source = {e.Name: list(e.Source) for e in old.OWL}
    for entity in new.OWL:
        merged = list(old_rationale.get(entity.Name, []))
        seen = {rationale_key(r) for r in merged}
        for r in entity.Rationale:
            if rationale_key(r) not in seen:
                merged.append(r)
                seen.add(rationale_key(r))
        entity.Rationale = merged
        merged_s = list(old_source.get(entity.Name, []))
        seen_s = {source_key(s) for s in merged_s}
        for s in entity.Source:
            if source_key(s) not in seen_s:
                merged_s.append(s)
                seen_s.add(source_key(s))
        entity.Source = merged_s
    removed = set(new.Removed)
    names = {e.Name for e in new.OWL}
    new.OWL += [e for e in old.OWL
                if e.Name not in names and e.Name not in removed]
    return new


# =============================================================================
# Provenance: the live recorder behind <domain>_ontology_prov.owl
# =============================================================================
# The pipeline hands the recorder every step record the moment it is logged
# (record), tells it which ontology was actually saved after each
# OntoGeneration / OntoCorrection response (record_version), and asks it to
# render the provenance ontology right after that response is saved
# (inject) - so the prov file exists from the first generation on and is
# rewritten after every correction. Nothing is read back from the steps log.
#
# The graph injected into the plain ontology keeps that document's triples,
# comments and layout intact and uses only standard prefixes: owl rdf rdfs
# xsd dc vaem earl prov. Provenance individuals live in their own
# <base>_prov# namespace so the domain ontology stays clean.
#
# GENERIC BY DESIGN - nothing about the pipeline is hard-coded:
#   * every step is classified by the SHAPE OF ITS OUTPUT, not by agent name:
#         {"OWL":[...]}            -> an ontology version (generation / correction)
#         {"terms":[...]}          -> a term stage
#         {"atomization":[...]}    -> a CQ-atomization stage
#         {"axioms":[...]}         -> an axiom-proposal stage
#         {"tests":[...]}          -> a test-proposal stage
#         {"passed":...}           -> a validation check (tool)
#         {"removed_classes":[..]} -> a term-pruning step
#         error + no usable output -> a failed attempt (an activity + earl:failed)
#         anything else            -> a generic stage with a <Agent>Output entity
#   * agents / tools are discovered from the records; known agent names get
#     the canonical activity names (AtomizationActivity ... GenerationActivity,
#     OntoCorrectionActivity); unknown agents get <AgentName>Activity.
#     Repeats get _2, _3 ...
#   * any number of ontology versions: V0 -> V1 -> ... ; versions are diffed
#     element-by-element and axiom-by-axiom, and every added / removed /
#     changed element or axiom is linked to the activity that did it and to
#     the failing tool report that triggered it.
#   * validation rounds are delimited by ontology versions (round k checks
#     version k); a correction is prov:wasInformedBy the failing checks of
#     the preceding round and prov:used their earl:TestResult reports.
#   * missing stages simply produce nothing; extra stages are chained in order.

PROV = "http://www.w3.org/ns/prov#"
EARL = "http://www.w3.org/ns/earl#"
PROV_XSD_TYPES = {"string", "boolean", "integer", "int", "float", "double", "decimal", "dateTime",
             "date", "anyURI", "nonNegativeInteger", "positiveInteger", "long", "short"}
TERM_KEYS = ("classes", "object_properties", "data_properties", "individuals", "instances")
# term kind -> provenance predicate local name (camelCase, like the other
# evaluation predicates). A term stage links to each of its terms once per kind
# it had AT THAT STAGE, so a term reclassified between stages shows up under a
# different predicate in each.
TERM_KIND_PRED = {"classes": "class", "object_properties": "objectProperty",
                  "data_properties": "dataProperty", "individuals": "individual",
                  "instances": "instance"}

# canonical activity / output names for agent names we recognise (substring, ordered)
STAGE_MATCH = [
    ("atomization", ["atomization", "atomize"], "Atomization", "AtomicCQSet"),
    ("extraction", ["termextraction", "extraction"], "Extraction", "CandidateTerms"),
    ("identification", ["identification", "identify"], "Identification", "IdentifiedTerms"),
    ("refinement", ["refinement", "refine"], "Refinement", "RefinedTerms"),
    ("axiomgen", ["axiomgeneration", "axiomgen"], "AxiomGeneration", "ProposedAxioms"),
    ("testgen", ["testgeneration", "testgen"], "TestGeneration", "ProposedTests"),
    ("correction", ["correction", "correct", "repair", "fix"], "OntoCorrection", None),
    ("generation", ["ontogeneration", "generation", "generate"], "Generation", None),
]


def slug(t): return re.sub(r"[^A-Za-z0-9_]+", "_", str(t)).strip("_")
def root_cq(c): return re.sub(r"_a\d+$", "", str(c))
def split_ids(content): return [c for c in re.split(r"[,\s;]+", str(content or "")) if re.match(r"^CQ\w+$", c)]


# ---- agent roles -------------------------------------------------------------
# GeneratorRole : agents that CREATE new content from the CQs (atomization,
#                 term extraction, ontology generation)
# RefinementRole: agents that FILTER / RESHAPE existing content (identification,
#                 refinement, axiom & test proposal, any unknown LLM stage)
# FixRole       : agents that CORRECT the ontology after a validation failure
# ValidatorRole : tools that CHECK the ontology (syntax, coverage, OOPS, HermiT, Themis)
ROLE_OF_STAGE = {"atomization": "GeneratorRole", "extraction": "GeneratorRole", "generation": "GeneratorRole",
                 "identification": "RefinementRole", "refinement": "RefinementRole",
                 "axiomgen": "RefinementRole", "testgen": "RefinementRole", "correction": "FixRole"}
ROLE_DOC = {"GeneratorRole": "Creates new content from the competency questions: CQ atomization, term extraction, ontology generation.",
            "RefinementRole": "Filters or reshapes existing content: term identification/refinement, axiom and test proposal, other LLM stages.",
            "FixRole": "Corrects the ontology in response to a validation-tool failure (ontology correction).",
            "ValidatorRole": "Checks the ontology without changing it: syntax, CQ coverage, OOPS, HermiT, Themis."}
ROLE_OVERRIDE = {}   # filled from --roles: {"agent name substring": "RoleName"}


def role_for(agent, is_correction=False, is_tool=False):
    low = (agent or "").lower()
    for sub, r in ROLE_OVERRIDE.items():
        if sub.lower() in low: return r
    if is_tool and not agent: return "ValidatorRole"
    if is_correction: return "FixRole"
    key, _, _ = canonical(agent)
    return ROLE_OF_STAGE.get(key, "RefinementRole")


def canonical(agent):
    low = (agent or "").lower().replace(" ", "").replace("_", "")
    for key, subs, base, out in STAGE_MATCH:
        if any(s in low for s in subs):
            return key, base, out
    base = slug(re.sub(r"\s*agent\s*$", "", agent or "Unknown", flags=re.I)) or "Unknown"
    return None, base, None


def flatten_terms(out):
    """term -> set(atomic cq ids); works for {terms:[{atomic_cq_mapping:[...]}]} and flat lists."""
    m = {}
    for cq in (out or {}).get("terms") or []:
        if not isinstance(cq, dict): continue
        maps = cq.get("atomic_cq_mapping") if isinstance(cq.get("atomic_cq_mapping"), list) else [cq]
        for a in maps:
            if not isinstance(a, dict): continue
            aid = a.get("atomic_cq_id") or cq.get("cq_id") or ""
            for k in TERM_KEYS:
                for t in a.get(k) or []:
                    m.setdefault(str(t), set()).add(str(aid))
    return m


def typed_terms(out):
    """(root_cq, type) -> set(terms) — for replacement detection."""
    m = {}
    for cq in (out or {}).get("terms") or []:
        if not isinstance(cq, dict): continue
        maps = cq.get("atomic_cq_mapping") if isinstance(cq.get("atomic_cq_mapping"), list) else [cq]
        for a in maps:
            if not isinstance(a, dict): continue
            rc = root_cq(a.get("atomic_cq_id") or cq.get("cq_id") or "")
            for k in TERM_KEYS:
                m.setdefault((rc, k), set()).update(map(str, a.get(k) or []))
    return m


# a step whose error_message starts one of these is a REJECTED model call,
# never a pipeline stage - even when its raw reply happens to parse as JSON
# carrying a stage-shaped key, which would otherwise mint a phantom
# IdentifiedTerms_2 / AtomicCQSet_2 entity in the provenance graph.
# OntoCorrection steps also fill error_message (with the triggering tool
# report), so the test has to be on the prefix, not on emptiness.
FAILED_CALL_RE = re.compile(r"^(model call \d+ failed:"
                            r"|parsed reply maps to no real id)")


def classify(s):
    out = s.get("output")
    err = (s.get("error_message") or "").strip()
    if FAILED_CALL_RE.match(err): return "failed"
    if isinstance(out, dict):
        if isinstance(out.get("OWL"), list) and out["OWL"]: return "ontology"
        if "passed" in out: return "check"
        if isinstance(out.get("terms"), list) and out["terms"]: return "terms"
        if isinstance(out.get("atomization"), list): return "atomization"
        if isinstance(out.get("axioms"), list): return "axioms"
        if isinstance(out.get("tests"), list): return "tests"
        if isinstance(out.get("removed_classes"), list): return "prune"
        if out: return "other"
    if err: return "failed"
    return "other"


def elem_sig(el):
    return json.dumps({k: el.get(k) for k in ("Comment", "Label", "Domain", "Range", "InstanceOf",
                                               "PropertyCharacteristics")}, sort_keys=True) + \
        "|" + "|".join(sorted(map(str, el.get("Axioms") or [])))


def parse_axiom(ax):
    p = str(ax).split()
    if "SubClassOf" in p:
        i = p.index("SubClassOf"); rest = p[i + 1:]
        if len(rest) == 1: return ("rdfs:subClassOf", rest[0])
        if len(rest) >= 3 and rest[1] == "some": return ("rdfs:subClassOf", f"{rest[0]} some {rest[2]}")
    if "DisjointWith" in p: return ("owl:disjointWith", p[p.index("DisjointWith") + 1])
    if "domain" in p: return ("rdfs:domain", p[p.index("domain") + 1])
    if "range" in p: return ("rdfs:range", p[p.index("range") + 1])
    return (None, None)


PURI = {"rdfs:subClassOf": "http://www.w3.org/2000/01/rdf-schema#subClassOf",
        "owl:disjointWith": "http://www.w3.org/2002/07/owl#disjointWith",
        "rdfs:domain": "http://www.w3.org/2000/01/rdf-schema#domain",
        "rdfs:range": "http://www.w3.org/2000/01/rdf-schema#range",
        "rdf:type": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"}


def fail_map_from_report(text):
    """'CQ06:\\n  has range XmlEditing -> Absent' -> {'has range XmlEditing': 'CQ06'}; also 'CQ06: x -> y; CQ14: …'"""
    fm, cur = {}, None
    for chunk in re.split(r"[;\n]", str(text or "")):
        m = re.match(r"\s*(CQ\w+):\s*(.*)$", chunk)
        if m:
            cur = m.group(1); chunk = m.group(2)
        m2 = re.match(r"\s*(.+?)\s*->\s*\w+\s*$", chunk)
        if m2 and cur: fm[m2.group(1).strip()] = cur
    return fm



class ProvenanceRecorder:
    """Collects the run as it happens and renders its provenance graph.

    record(step)            - one step record, exactly as written to the
                              steps log, the moment it is logged
    record_version(answer)  - the ontology actually saved after the latest
                              ontology-producing step (the merged answer),
                              so version diffs see what was written, not
                              only the agent's raw reply
    inject(plain_document)  - the plain ontology document with the graph
                              recorded so far injected; the latest version
                              is the ontology itself, earlier ones are
                              superseded drafts"""

    def __init__(self, ns: str, run_id: str, pipeline: str = "MASEO",
                 atomic: bool = True, prov_ns: str = None, roles: dict = None):
        self.ns = ns if ns.endswith(("#", "/")) else ns + "#"
        self.run_id = run_id
        self.pipeline = pipeline
        self.no_atomic = not atomic
        self.prov_ns = prov_ns
        self.steps: List[Dict[str, Any]] = []
        self.versions: Dict[int, list] = {}   # step index -> saved OWL element dicts
        self.stats: Dict[str, Any] = {}
        if roles:
            ROLE_OVERRIDE.update(roles)

    def record(self, step: Dict[str, Any]) -> None:
        self.steps.append(step)

    def record_version(self, answer) -> None:
        """The ontology saved after the latest ontology step: the agent's
        reply merged with the entities it did not repeat."""
        for idx in range(len(self.steps) - 1, -1, -1):
            out = self.steps[idx].get("output")
            if isinstance(out, dict) and isinstance(out.get("OWL"), list):
                self.versions[idx] = [e.model_dump() for e in answer.OWL]
                return

    def inject(self, owl: str) -> str:

        run_id = self.run_id
        ns = self.ns or re.search(r'xml:base="([^"]+)"', owl).group(1)
        if not ns.endswith(("#", "/")): ns += "#"
        # domain namespace (the ontology's own) vs provenance namespace (separate by default)
        if self.prov_ns in ("same",):
            pns = ns                                        # opt back into mixing prov with the domain ns
        elif self.prov_ns in (None, "", "auto"):
            pns = ns.rstrip("#/") + "_prov#"                # default: <base>_prov#, a clean separate ns
        else:
            pns = self.prov_ns if self.prov_ns.endswith(("#", "/")) else self.prov_ns + "#"
        def DNS(x=""): return ns + x       # domain elements (classes/properties/individuals)
        def NS(x=""): return pns + x       # provenance / evaluation individuals
        separate_prov = (pns != ns)
        # the step records fed by the pipeline as they happened (copied, so the
        # log entries themselves are never touched); an ontology record whose
        # saved version was supplied through record_version() is diffed on that
        # saved (merged) ontology rather than on the agent's raw reply
        steps = [dict(s) for s in self.steps]
        for idx, s in enumerate(steps):                        # normalise string outputs
            if isinstance(s.get("output"), str):
                try: s["output"] = json.loads(s["output"])
                except Exception: s["output"] = {}
            if idx in self.versions and isinstance(s.get("output"), dict):
                s["output"] = dict(s["output"], OWL=self.versions[idx])
            s["_kind"] = classify(s)

        # ------------------------------------------------------------------ CQ texts
        cqtext = {}
        for s in steps:
            for m in re.finditer(r"\b(CQ\w+?)(?:\s*\([^)]*\))?:\s*([^\n]+)", str(s.get("input") or "")):
                cid = root_cq(m.group(1)); t = m.group(2).strip()
                if cid == m.group(1) and cid not in cqtext and t and not t.startswith("["): cqtext[cid] = t
            out = s.get("output") if isinstance(s.get("output"), dict) else {}
            for cq in out.get("terms") or []:
                if isinstance(cq, dict) and cq.get("cq_id") and cq.get("cq"): cqtext.setdefault(root_cq(cq["cq_id"]), cq["cq"])

        # ------------------------------------------------------------------ build activity list
        agents, tools = [], []
        for s in steps:
            if s.get("agent") and s["agent"] not in agents: agents.append(s["agent"])
            if s.get("tool") and s["tool"] not in tools: tools.append(s["tool"])
        used_names = {}
        def uniq(base):
            n = used_names.get(base, 0) + 1; used_names[base] = n
            return base if n == 1 else f"{base}_{n}"

        acts = []           # ordered activities: dict(iri,label,kind,agent,tool,steps,used,generated,informed,...)
        versions = []       # ontology versions in order: dict(iri, act, step, elements{name:el})
        latest = {}         # kind -> entity iri (terms/axioms/tests/ontology)
        termsets = []       # ordered term stages: dict(act, agent, terms{term:set(acq)}, typed, entity)
        atom_detail = {}    # root cq -> {recomposition, atomics}
        checks = []         # dict(iri, report_iri, step, tool, passed, report, error, round)
        prev_stage_act = None
        pending_failed = []  # failed attempts feeding the next successful same-agent activity
        i = 0
        while i < len(steps):
            s = steps[i]; kind = s["_kind"]; agent = s.get("agent") or ""; tool = s.get("tool") or ""
            key, base, outname = canonical(agent) if agent else (None, slug(tool) or "Tool", None)
            # ---- group consecutive same-agent/tool/kind steps into one stage activity
            j = i
            if kind in ("atomization", "terms", "axioms", "tests", "other") and agent:
                while j + 1 < len(steps) and steps[j + 1].get("agent") == agent and steps[j + 1].get("tool") == tool \
                        and steps[j + 1]["_kind"] == kind:
                    j += 1
            group = steps[i:j + 1]

            if kind == "check" and not agent:
                rno = len(versions)  # checks validate the latest version
                iri = f"Check_r{max(rno,1)}_{slug(tool)}_{i}"
                out = s.get("output") or {}
                err = (s.get("error_message") or "").strip()
                report = str(out.get("report") or out.get("errors") or "").strip()
                checks.append({"iri": iri, "report_iri": f"Report_r{max(rno,1)}_{slug(tool)}_{i}", "step": i,
                               "tool": tool, "passed": bool(out.get("passed")) and not err, "report": report,
                               "error": err, "round": max(rno, 1), "version": versions[-1]["iri"] if versions else None,
                               "meta": {k: s[k] for k in ("round", "phase", "attempt") if k in s}})
                i = j + 1; continue

            if kind == "failed":
                iri = uniq(f"{base}Attempt")
                prior = [f for f in pending_failed if f["agent"] == agent]
                # a failed attempt is informed by the stage before it AND by any earlier
                # failed attempt by the same agent (model call 1 failed -> call 2 failed -> …)
                informed = ([prev_stage_act] if prev_stage_act else []) + [f["iri"] for f in prior]
                act = {"iri": iri, "report_iri": "Report_" + iri, "label": f"{agent or tool} attempt {len(prior)+1} (rejected, step {i})",
                       "kind": "failed", "agent": agent, "tool": tool, "steps": [i], "used": [], "generated": [],
                       "informed": informed, "error": (s.get("error_message") or "").strip(),
                       "role": role_for(agent), "plan": "GenerationPrompt",
                       "attempt_no": len(prior) + 1, "stage": base}
                acts.append(act); pending_failed.append(act); i = j + 1; continue

            if kind == "ontology":
                k = len(versions)
                act_iri = uniq(f"{base}Activity")
                ver_iri = f"OntologyDraft_iteration{k}"   # last one is re-pointed to the ontology IRI below
                used = [latest[x] for x in ("terms", "axioms", "tests") if x in latest]
                # a successful (re)generation is informed by, and reads the error report of,
                # every failed attempt that preceded it (retry read the previous error)
                retries = [f for f in pending_failed if f["agent"] == agent]
                informed = ([prev_stage_act] if prev_stage_act else []) + [f["iri"] for f in retries]
                used += [f["report_iri"] for f in retries]
                trigger_checks = []
                if versions:
                    used = [versions[-1]["iri"]] + used
                    failed = [c for c in checks if c["round"] == k and not c["passed"]]
                    trigger_checks = [c for c in failed if c["tool"] == tool] or failed
                    informed += [versions[-1]["act"]] + [c["iri"] for c in trigger_checks]
                    used += [c["report_iri"] for c in trigger_checks]
                act = {"iri": act_iri, "label": f"{'Ontology generation' if k == 0 else 'Ontology correction'} "
                                                f"by {agent}" + (f" (triggered by {tool})" if tool else "") + f" (step {i})",
                       "kind": "ontology", "agent": agent, "tool": tool, "steps": [i], "used": used, "generated": [ver_iri],
                       "informed": informed, "error": (s.get("error_message") or "").strip(),
                       "reason": str((s.get("output") or {}).get("reason") or ""),
                       "role": role_for(agent, is_correction=bool(versions)),
                       "plan": "GenerationPrompt" if not versions else "CorrectionPrompt",
                       "trigger_checks": trigger_checks, "retries": [f["iri"] for f in retries],
                       "correction_round": k if versions else 0,        # 0 = first generation, 1.. = correction rounds
                       "triggered_by": [c["iri"] for c in trigger_checks],
                       "trigger_tools": sorted({c["tool"] for c in trigger_checks}),
                       "correction_outcome": "succeeded"}   # produced usable OWL; agent-level failures are separate 'failed' attempts
                acts.append(act); pending_failed = [f for f in pending_failed if f["agent"] != agent]
                versions.append({"iri": ver_iri, "act": act_iri, "step": i, "agent": agent, "tool": tool,
                                 "elements": {el["Name"]: el for el in s["output"]["OWL"] if isinstance(el, dict) and el.get("Name")},
                                 "removed_list": list((s.get("output") or {}).get("Removed") or []), "act_obj": act})
                latest["ontology"] = ver_iri; prev_stage_act = act_iri; i = j + 1; continue

            # ---- prune folded into a term stage --------------------------------
            # A pruning step (removed_classes) is not a stage of its own: it is the
            # same agent continuing to filter terms. Fold it into the preceding
            # term stage (same agent, or any last term stage) so the removed
            # classes are attributed to that stage's activity (e.g. RefinementActivity)
            # and no PruneActivity / PrunedTerms entity is emitted.
            if kind == "prune" and termsets:
                host = next((t for t in reversed(termsets) if t["agent"] == agent), termsets[-1])
                removed = list(map(str, (s.get("output") or {}).get("removed_classes") or []))
                host["terms"] = {t: v for t, v in host["terms"].items() if t not in removed}
                host["typed"] = {k: v - set(removed) for k, v in host["typed"].items()}
                host.setdefault("pruned", []).extend(removed)
                host["prune_note"] = str(s.get("input") or "")
                latest["terms"] = host["entity"]
                prev_stage_act = host["act"]; i = j + 1; continue

            # ---- stage activities (atomization / terms / axioms / tests / other) --
            if tool and agent:                                    # agent step driven by a tool -> name it after the tool
                base = "".join(w.capitalize() for w in re.split(r"[^A-Za-z0-9]+", tool) if w) or base
                outname = None
            act_iri = uniq(f"{base}Activity")
            ent_iri = uniq(outname or (f"{base}Output"))
            merged = {}
            for g in group:
                if isinstance(g.get("output"), dict):
                    for kk, v in g["output"].items():
                        if isinstance(v, list): merged.setdefault(kk, []).extend(v)
            used = []
            if kind == "atomization": used = []      # CQs added below
            elif kind == "terms": used = [latest.get("terms") or latest.get("atomization")] if (latest.get("terms") or latest.get("atomization")) else []
            elif kind in ("axioms", "tests"): used = [latest[x] for x in ("terms",) if x in latest]
            else: used = [latest[x] for x in ("ontology", "terms") if x in latest][:1]
            used = [u for u in used if u]
            act = {"iri": act_iri, "label": f"{base} ({agent}{' | ' + tool if tool else ''}; steps {i}-{j})" if j > i
                   else f"{base} ({agent}{' | ' + tool if tool else ''}; step {i})",
                   "kind": kind, "agent": agent, "tool": tool, "steps": list(range(i, j + 1)), "used": used,
                   "generated": [ent_iri],
                   "informed": ([prev_stage_act] if prev_stage_act else []) + [f["iri"] for f in pending_failed if f["agent"] == agent],
                   "error": "", "role": role_for(agent), "plan": "GenerationPrompt", "entity_label": None,
                   "retries": [f["iri"] for f in pending_failed if f["agent"] == agent]}
            for f in list(pending_failed):
                if f["agent"] == agent: pending_failed.remove(f)
            if kind == "atomization":
                for cq in merged.get("atomization") or []:
                    if isinstance(cq, dict) and cq.get("cq_id"):
                        atom_detail[cq["cq_id"]] = {"recomposition": cq.get("recomposition", ""),
                                                    "atomics": [{"id": x.get("atomic_cq_id"), "text": x.get("atomic_cq", ""),
                                                                 "template": x.get("template", "")}
                                                                for x in cq.get("atomic_cqs") or [] if x.get("atomic_cq_id")]}
                latest["atomization"] = ent_iri; act["entity_label"] = "Atomic competency questions"
            elif kind == "terms":
                tm = flatten_terms({"terms": merged.get("terms")})
                termsets.append({"act": act_iri, "agent": agent, "tool": tool, "terms": tm,
                                 "typed": typed_terms({"terms": merged.get("terms")}), "entity": ent_iri})
                latest["terms"] = ent_iri; act["entity_label"] = f"Term mapping ({base} output)"
            elif kind == "axioms":
                act["axioms"] = []
                for rec in merged.get("axioms") or []:
                    if isinstance(rec, dict) and isinstance(rec.get("axioms"), list):
                        act["axioms"] += [{"axiom": str(x), "cq": rec.get("cq_id")} for x in rec["axioms"]]
                    elif isinstance(rec, dict): act["axioms"].append({"axiom": str(rec.get("axiom") or rec.get("text") or ""), "cq": rec.get("cq_id")})
                    else: act["axioms"].append({"axiom": str(rec), "cq": None})
                latest["axioms"] = ent_iri; act["entity_label"] = "Axioms proposed by axiom generation"
            elif kind == "tests":
                act["tests"] = []
                for rec in merged.get("tests") or []:
                    if isinstance(rec, dict) and isinstance(rec.get("tests"), list):
                        act["tests"] += [{"test": str(x), "cq": rec.get("cq_id")} for x in rec["tests"]]
                    elif isinstance(rec, dict): act["tests"].append({"test": str(rec.get("test") or rec.get("query") or ""), "cq": rec.get("cq_id") or rec.get("atomic_cq")})
                    else: act["tests"].append({"test": str(rec), "cq": None})
                latest["tests"] = ent_iri; act["entity_label"] = "Tests proposed by test generation"
            else:
                act["entity_label"] = f"Output of {base}"
                tm = flatten_terms({"terms": merged.get("terms")}) if merged.get("terms") else {}
                if tm:
                    termsets.append({"act": act_iri, "agent": agent, "tool": tool, "terms": tm,
                                     "typed": typed_terms({"terms": merged.get("terms")}), "entity": ent_iri})
                    latest["terms"] = ent_iri
            acts.append(act); prev_stage_act = act_iri; i = j + 1

        if not versions:
            return owl          # nothing generated yet: the document stays plain
        final = versions[-1]; final["iri"] = ns                     # the final version IS this ontology
        for c in checks:
            if c["version"] == f"OntologyDraft_iteration{len(versions)-1}": c["version"] = ns
        for act in acts:
            act["generated"] = [ns if g == f"OntologyDraft_iteration{len(versions)-1}" else g for g in act["generated"]]
            act["used"] = [ns if u == f"OntologyDraft_iteration{len(versions)-1}" else u for u in act["used"]]
        elements = final["elements"]
        iteration = len(versions) - 1

        # ------------------------------------------------------------------ per-element analyses
        def elem_cqs(el):
            raw, others = [], []
            for src in el.get("Source") or []:
                if not isinstance(src, dict): continue
                if src.get("sourcetype") == "competency_question": raw += split_ids(src.get("content"))
                else: others.append(src)
            roots = []
            for c in raw:
                r = root_cq(c)
                if r not in roots: roots.append(r)
            return roots, raw, others

        # term-stage lineage per term: ordered stages containing it; first = introducing
        def stage_chain(name):
            chain, prev = [], False
            for ts in termsets:
                here = name in ts["terms"]
                if here: chain.append((ts, not prev))
                prev = here
            return chain
        # (term, root cq) -> first term-stage activity
        tcq_stage = {}
        for ts in termsets:
            for t, acqs in ts["terms"].items():
                for ac in acqs: tcq_stage.setdefault((t, root_cq(ac)), ts["act"])
        # removed / added / replaced between consecutive term stages
        removed_terms, added_terms, replaced_by, replaced_at = {}, {}, {}, {}
        for p, q in zip(termsets, termsets[1:]):
            for t in set(p["terms"]) - set(q["terms"]): removed_terms.setdefault(t, (q, p))
            for t in set(q["terms"]) - set(p["terms"]): added_terms.setdefault(t, q)
            for kk in set(p["typed"]) | set(q["typed"]):
                r = p["typed"].get(kk, set()) - q["typed"].get(kk, set()); ad = q["typed"].get(kk, set()) - p["typed"].get(kk, set())
                if len(r) == 1 and len(ad) == 1:
                    replaced_by.setdefault(next(iter(ad)), set()).add(next(iter(r))); replaced_at.setdefault(next(iter(ad)), q)
        # classes removed by an in-stage prune (folded into the host term stage):
        # attribute the removal to the host stage's activity, unless the class is
        # (re)introduced in the final ontology.
        for ts in termsets:
            for t in ts.get("pruned", []):
                if t not in elements and t not in removed_terms:
                    removed_terms[t] = (ts, ts)
        term_acqs = {} if self.no_atomic or not atom_detail else (termsets[0]["terms"] if termsets else {})
        # element origin & changes across ontology versions
        origin, changes, removed_elems = {}, {}, {}        # name -> version idx ; name -> [version idx] ; name -> version idx
        for vi, v in enumerate(versions):
            for name, el in v["elements"].items():
                if name not in origin: origin[name] = vi
                elif name in versions[vi - 1]["elements"] and elem_sig(el) != elem_sig(versions[vi - 1]["elements"][name]):
                    changes.setdefault(name, []).append(vi)
            if vi:
                for name in set(versions[vi - 1]["elements"]) - set(v["elements"]):
                    removed_elems.setdefault(name, vi)
        # axioms: (owner, axiom) -> first version ; removed axioms -> version idx
        ax_origin, ax_removed = {}, {}
        def axset(v):
            s = {}
            for name, el in v["elements"].items():
                for ax in el.get("Axioms") or []: s[(name, str(ax))] = True
                if el.get("InstanceOf"): s[(name, f"{name} type {el['InstanceOf']}")] = True
            return s
        prev_ax = {}
        for vi, v in enumerate(versions):
            cur = axset(v)
            for kk in cur: ax_origin.setdefault(kk, vi)
            for kk in set(prev_ax) - set(cur): ax_removed.setdefault(kk, vi)
            prev_ax = cur
        # failing reports per version idx -> {axiom fragment: cq}
        fail_map = {}
        for c in checks:
            if not c["passed"]:
                fail_map.setdefault(c["round"], {}).update(fail_map_from_report(c["error"] + "\n" + c["report"]))
        def trigger_reports(vi):
            return [c["report_iri"] for c in versions[vi]["act_obj"].get("trigger_checks", [])]
        def act_agent(vi): return versions[vi]["agent"]

        # ------------------------------------------------------------------ header
        owl = owl.replace('xmlns:vaem="http://www.linkedmodel.org/schema/vaem#">',
                          f'xmlns:vaem="http://www.linkedmodel.org/schema/vaem#"\n         xmlns:earl="{EARL}"\n         xmlns:prov="{PROV}">', 1)
        if 'xmlns:prov=' not in owl:
            owl = owl.replace('<rdf:RDF ', f'<rdf:RDF xmlns:earl="{EARL}" xmlns:prov="{PROV}" ', 1)
        # bind a prefix to the PROVENANCE namespace so evaluation predicates can be written as
        # qualified element tags (e.g. <swo_prov:correctionRound>). When prov shares the domain
        # ns, reuse the domain prefix; otherwise declare a dedicated one (default 'swo_prov').
        root_open = re.search(r'<rdf:RDF[^>]*>', owl).group(0)
        existing_pfx = re.search(r'xmlns:([\w-]+)="' + re.escape(pns) + '"', root_open)
        if existing_pfx:
            EVP = existing_pfx.group(1)
        else:
            base_pfx = re.search(r'xmlns:([\w-]+)="' + re.escape(ns) + '"', root_open)
            # no prefix declared for the domain namespace (the ontology uses
            # xml:base only): derive one from its last path segment, so
            # http://www.semanticweb.org/wine# gives wine / wine_prov
            domain_pfx = base_pfx.group(1) if base_pfx else \
                (slug(re.sub(r"[#/]+$", "", ns).rsplit("/", 1)[-1]) or "onto")
            if not separate_prov:
                EVP = domain_pfx                                        # prov shares the domain ns
            else:
                EVP = slug(domain_pfx) + "_prov"
            owl = owl.replace('xmlns:prov="http://www.w3.org/ns/prov#">',
                              f'xmlns:prov="http://www.w3.org/ns/prov#"\n         xmlns:{EVP}="{pns}">', 1)
        hdr = [f'  <owl:Ontology rdf:about="{ns}">', f'    <owl:versionInfo>iteration {iteration}</owl:versionInfo>',
               f'    <prov:wasAttributedTo rdf:resource="{NS(self.pipeline)}"/>',
               f'    <prov:wasGeneratedBy rdf:resource="{NS(final["act"])}"/>']
        if iteration: hdr.append(f'    <prov:wasRevisionOf rdf:resource="{NS(versions[-2]["iri"])}"/>')
        for ag in dict.fromkeys(v["agent"] for v in versions if v["agent"]):
            hdr.append(f'    <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(ag))}"/>')
        hdr += ['  </owl:Ontology>', '',
                '  <!-- ================================================================ -->',
                '  <!-- Provenance vocabulary used in this file.                          -->',
                '  <!-- PROV-O / EARL terms are declared once so the file is self-contained;-->',
                '  <!-- prov:* and evaluation predicates are declared as ANNOTATION       -->',
                '  <!-- properties so the domain ontology stays inside OWL 2 DL.          -->',
                '  <!-- ================================================================ -->', '']
        def decl(kind, iri, comment):
            hdr.append(f'  <{kind} rdf:about="{iri}">'); hdr.append(f'    <rdfs:comment>{escape(comment)}</rdfs:comment>'); hdr.append(f'  </{kind}>')
        hdr.append('  <!-- PROV-O classes -->')
        for c, cm in [("Entity", "A thing with provenance: competency questions, term sets, axioms, ontology versions, reports."),
                      ("Activity", "Something that happened over time: an agent stage, a tool check, a validation round."),
                      ("SoftwareAgent", "An LLM agent or a validation tool of the pipeline."),
                      ("Plan", "The prompt / procedure an agent followed (generation prompt, correction prompt, validation plan)."),
                      ("Role", "The function an agent had in an activity: GeneratorRole, RefinementRole, FixRole, ValidatorRole."),
                      ("Association", "Qualified link agent-activity carrying the role the agent played."),
                      ("Derivation", "Qualified link element-CQ recording the activity in which the element was first derived from that CQ."),
                      ("Invalidation", "Event instance: a term or element was removed by an activity."),
                      ("Generation", "Event instance: a term was introduced by an activity."),
                      ("Collection", "An entity whose members are other entities: a term stage and its set of terms.")]:
            decl("owl:Class", PROV + c, cm)
        hdr.append(''); hdr.append('  <!-- PROV-O relations (as annotation properties) -->')
        for p, cm in [("wasDerivedFrom", "Element/axiom/test <- competency question or failing report it was derived from."),
                      ("wasGeneratedBy", "Entity <- activity that created it."),
                      ("wasInfluencedBy", "Entity <- activity that kept or changed it without creating it (weaker than wasGeneratedBy)."),
                      ("wasInformedBy", "Activity <- earlier activity whose output it built on (pipeline order)."),
                      ("used", "Activity -> entity it consumed as input."),
                      ("generated", "Activity -> entity it produced (inverse of wasGeneratedBy)."),
                      ("wasAssociatedWith", "Activity -> agent/tool that performed it."),
                      ("actedOnBehalfOf", "Agent -> the pipeline it acts for."),
                      ("wasAttributedTo", "Entity -> agent credited with it."),
                      ("wasRevisionOf", "Entity -> earlier version it revises (ontology after correction; term replacing another)."),
                      ("wasInvalidatedBy", "Entity -> activity that made it obsolete (old ontology draft superseded by a correction)."),
                      ("wasQuotedFrom", "Reified axiom -> the element it is quoted from."),
                      ("hadRole", "Activity/association/agent -> the role played (GeneratorRole, RefinementRole, FixRole, ValidatorRole)."),
                      ("hadPlan", "Activity -> the plan/prompt followed."),
                      ("hadActivity", "Event or derivation -> the activity in which it happened."),
                      ("qualifiedDerivation", "Element -> Derivation node (which CQ, in which activity)."),
                      ("qualifiedAssociation", "Activity -> Association node (which agent, in which role)."),
                      ("entity", "Derivation -> the CQ it derives from."),
                      ("agent", "Association -> the agent."),
                      ("value", "Literal payload: CQ text, test text, tool report."),
                      ("type", "PROV sub-typing hook (unused unless a step supplies one)."),
                      ("hadMember", "Collection -> one of its members (term stage -> a term it contains)."),
                      ("alternateOf", "Term <-> the ontology element presenting the same thing (symmetric in PROV-O)."),
                      ("generatedAtTime", "Timestamp of generation (only if the log supplies one).")]:
            decl("owl:AnnotationProperty", PROV + p, cm)
        hdr.append(''); hdr.append('  <!-- Other standard vocabularies -->')
        decl("owl:AnnotationProperty", "http://purl.org/dc/elements/1.1/source", "Original source text kept verbatim (CQ, tool error) when no report entity carries it.")
        decl("owl:AnnotationProperty", "http://www.linkedmodel.org/schema/vaem#rationale", "Human-readable reason: why an element exists, what a stage changed, why an axiom was added.")
        decl("owl:AnnotationProperty", "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate", "With rdf:subject/rdf:object: the reified axiom (S P O) an ax_* entity stands for.")
        decl("owl:Class", EARL + "TestResult", "Result of one validation-tool run or of a whole validation round.")
        decl("owl:AnnotationProperty", EARL + "outcome", "earl:passed or earl:failed.")
        hdr += [f'  <owl:NamedIndividual rdf:about="{EARL}passed"/>', f'  <owl:NamedIndividual rdf:about="{EARL}failed"/>', '',
                '  <!-- ================================================================ -->',
                '  <!-- Evaluation layer: run-quality facts, queryable per run.           -->',
                '  <!-- Success is NOT stored; it is derived by query as:                 -->',
                '  <!--   (1) the final verify round has roundPassed=true  AND            -->',
                '  <!--   (2) no failed attempt is left with supersededByRetry=false.     -->',
                '  <!-- ================================================================ -->']
        decl("owl:Class", NS("PipelineRun"), "One execution of the pipeline (one steps log). All rounds/checks/corrections point to it via ofRun.")
        for p, cm in [("runId", "Identifier of the run (steps file name unless --run-id given). Group by it when merging many runs."),
                      ("ofRun", "Check / round / correction / attempt -> the PipelineRun it belongs to."),
                      ("roundIndex", "Validation round number = ontology version being validated (1 = first draft)."),
                      ("phase", "'loop' (find errors) or 'verify' (confirm after correction), from the log."),
                      ("attempt", "Attempt counter from the log (tool re-run, or n-th failed agent attempt)."),
                      ("isVerifyRound", "true on validation rounds whose phase is verify."),
                      ("isFinalRound", "true on the last validation round of the run; on PipelineRun, points to it."),
                      ("roundPassed", "true when every tool in the round passed."),
                      ("toolName", "Name of the validation tool that ran the check."),
                      ("checkOutcome", "'passed' or 'failed' for a check or a failed agent attempt."),
                      ("correctionRound", "n-th ontology correction of the run (1-based)."),
                      ("correctionOutcome", "'succeeded' when the correction produced a usable ontology."),
                      ("triggeredBy", "Correction -> the tool whose failure triggered it."),
                      ("stage", "Pipeline stage of a failed attempt (e.g. Generation)."),
                      ("supersededByRetry", "On a failed attempt's report: true if a later successful attempt replaced it.")]:
            decl("owl:AnnotationProperty", NS(p), cm)
        hdr.append('')
        hdr.append('  <!-- ================================================================ -->')
        hdr.append('  <!-- Term layer: every term of every term stage, split by kind.        -->')
        hdr.append('  <!-- One Term individual per distinct name, shared by the stages that   -->')
        hdr.append('  <!-- held it; the kind is carried by the STAGE -> term link, because a  -->')
        hdr.append('  <!-- term can be reclassified from one stage to the next.               -->')
        hdr.append('  <!-- ================================================================ -->')
        decl("owl:Class", NS("Term"),
             "A vocabulary term proposed by a term stage, before (or without) becoming an ontology element. "
             "prov:alternateOf points at the ontology element that presents the same thing, when the term reached the ontology.")
        decl("owl:AnnotationProperty", NS("termName"), "Local name of the term exactly as the agent wrote it.")
        decl("owl:AnnotationProperty", NS("reachedOntology"), "true when the term is an element of the final ontology.")
        for _k in TERM_KEYS:
            decl("owl:AnnotationProperty", NS(TERM_KIND_PRED[_k]),
                 f"Term stage -> a term it classified as {TERM_KIND_PRED[_k]} ({_k}). "
                 f"Every such term is also a prov:hadMember of the stage.")
        m = re.search(r'  <owl:Ontology rdf:about="' + re.escape(ns) + r'"\s*/>', owl) or \
            re.search(r'  <owl:Ontology rdf:about="' + re.escape(ns) + r'">.*?</owl:Ontology>', owl, re.S)
        if m: owl = owl[:m.start()] + "\n".join(hdr) + owl[m.end():]
        else: owl = owl.replace("\n\n", "\n\n" + "\n".join(hdr) + "\n\n", 1)

        # ------------------------------------------------------------------ element injection
        warned = []
        def prov_links(nm):
            L = []; A = L.append
            el = elements.get(nm)
            if el is None:
                warned.append(nm)
                A(f'  <prov:wasGeneratedBy rdf:resource="{NS(final["act"])}"/>')
                A(f'  <vaem:rationale>not present in the pipeline log; attributed to the final ontology version</vaem:rationale>')
                return "\n".join(L)
            roots, raw, others = elem_cqs(el)
            creator_act = versions[origin[nm]]["act"]
            def cq_link(cq, rc):
                A(f'  <prov:wasDerivedFrom rdf:resource="{NS(slug(cq))}"/>')
                act = tcq_stage.get((nm, rc)) or creator_act
                A('  <prov:qualifiedDerivation>'); A('    <prov:Derivation>')
                A(f'      <prov:entity rdf:resource="{NS(slug(cq))}"/>'); A(f'      <prov:hadActivity rdf:resource="{NS(act)}"/>')
                A('    </prov:Derivation>'); A('  </prov:qualifiedDerivation>')
            for c in roots: cq_link(c, c)
            for ac in sorted(term_acqs.get(nm, set())):
                if ac and ac != root_cq(ac): cq_link(ac, root_cq(ac))
            A(f'  <prov:wasGeneratedBy rdf:resource="{NS(creator_act)}"/>')
            for old in sorted(replaced_by.get(nm, set())): A(f'  <prov:wasRevisionOf rdf:resource="{NS("removed_"+slug(old))}"/>')
            seen = set()
            for ts, introduced in stage_chain(nm):
                A(f'  <prov:wasInfluencedBy rdf:resource="{NS(ts["act"])}"/>')
                credit = introduced or (replaced_at.get(nm) is ts)
                if credit and ts["agent"] and ts["agent"] not in seen:
                    A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(ts["agent"]))}"/>'); seen.add(ts["agent"])
            for ts in termsets:                       # pruned by a tool but re-introduced later
                if nm in ts.get("pruned", []):
                    A(f'  <prov:wasInfluencedBy rdf:resource="{NS(ts["act"])}"/>')
                    A(f'  <vaem:rationale>[{escape(ts["agent"])}{" | " + escape(ts["tool"]) if ts["tool"] else ""}] pruned: {escape(ts["prune_note"])}; '
                      f're-introduced by [{escape(act_agent(origin[nm]))}] at {creator_act}</vaem:rationale>')
            if act_agent(origin[nm]) and act_agent(origin[nm]) not in seen:
                A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(act_agent(origin[nm])))}"/>'); seen.add(act_agent(origin[nm]))
            if origin[nm] > 0:
                for r in trigger_reports(origin[nm]): A(f'  <prov:wasDerivedFrom rdf:resource="{NS(r)}"/>')
            for vi in changes.get(nm, []):
                v = versions[vi]
                A(f'  <prov:wasInfluencedBy rdf:resource="{NS(v["act"])}"/>')
                if v["agent"] and v["agent"] not in seen:
                    A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(v["agent"]))}"/>'); seen.add(v["agent"])
                for r in trigger_reports(vi): A(f'  <prov:wasDerivedFrom rdf:resource="{NS(r)}"/>')
                prev_el = versions[vi - 1]["elements"].get(nm) or {}
                a1, a2 = set(map(str, prev_el.get("Axioms") or [])), set(map(str, v["elements"][nm].get("Axioms") or []))
                diff = []
                if a2 - a1: diff.append("added axioms: " + "; ".join(sorted(a2 - a1)))
                if a1 - a2: diff.append("removed axioms: " + "; ".join(sorted(a1 - a2)))
                for f in ("Comment", "Label", "Domain", "Range", "InstanceOf"):
                    if prev_el.get(f) != v["elements"][nm].get(f): diff.append(f"{f} changed")
                A(f'  <vaem:rationale>[{escape(v["agent"])}{" | " + escape(v["tool"]) if v["tool"] else ""}] '
                  f'changed at {v["act"]}: {escape("; ".join(diff) or "revised")}</vaem:rationale>')
            # non-CQ sources (e.g. error_message from a tool) -> link to that tool's failing report
            for src in others:
                tl = src.get("tool")
                for c in checks:
                    if tl and c["tool"] == tl and not c["passed"]:
                        A(f'  <prov:wasDerivedFrom rdf:resource="{NS(c["report_iri"])}"/>'); break
            return "\n".join(L)

        block_re = re.compile(r'(<owl:(Class|ObjectProperty|DatatypeProperty|NamedIndividual) rdf:about="'
                              + re.escape(ns) + r'([^"]+)">)(.*?)(</owl:\2>)', re.S)
        seen_blocks = []
        def repl(mm):
            seen_blocks.append(mm.group(3))
            body = mm.group(4)
            if "<!-- PROV-O" in body or "prov:wasGeneratedBy" in body: return mm.group(0)   # idempotent
            return mm.group(1) + body.rstrip() + "\n" + prov_links(mm.group(3)) + "\n" + mm.group(5)
        owl = block_re.sub(repl, owl)
        # dedupe repeated lines inside each injected block (cheap and safe)
        def dedupe(mm):
            body = mm.group(4); seen = set(); out = []
            for line in body.split("\n"):
                key = line.strip()
                if key.startswith("<prov:was") and key in seen: continue
                seen.add(key); out.append(line)
            return mm.group(1) + "\n".join(out) + mm.group(5)
        owl = block_re.sub(dedupe, owl)

        # ------------------------------------------------------------------ PROV section
        L = []; A = L.append
        A(''); A('  <!-- PROV-O provenance -->'); A('')
        all_cqs = []
        for el in elements.values():
            for c in elem_cqs(el)[0]:
                if c not in all_cqs: all_cqs.append(c)
        for c in list(cqtext) + list(atom_detail):
            if root_cq(c) not in all_cqs: all_cqs.append(root_cq(c))
        for c in all_cqs:
            A(f'<prov:Entity rdf:about="{NS(slug(c))}">'); A(f'  <rdfs:label>{escape(c)}</rdfs:label>')
            if cqtext.get(c): A(f'  <prov:value>{escape(cqtext[c])}</prov:value>')
            A('</prov:Entity>'); A('')
        atom_act = next((x for x in acts if x["kind"] == "atomization"), None)
        if not self.no_atomic and atom_act:
            for rid, info in atom_detail.items():
                for at in info["atomics"]:
                    A(f'<prov:Entity rdf:about="{NS(slug(at["id"]))}">'); A(f'  <rdfs:label>{escape(at["id"])}</rdfs:label>')
                    if at["text"]: A(f'  <prov:value>{escape(at["text"])}</prov:value>')
                    A(f'  <prov:wasDerivedFrom rdf:resource="{NS(slug(root_cq(rid)))}"/>')
                    A(f'  <prov:wasGeneratedBy rdf:resource="{NS(atom_act["iri"])}"/>')
                    if atom_act["agent"]: A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(atom_act["agent"]))}"/>')
                    if at["template"]: A(f'  <vaem:rationale>template: {escape(at["template"])}</vaem:rationale>')
                    if info["recomposition"]: A(f'  <vaem:rationale>recomposition: {escape(info["recomposition"])}</vaem:rationale>')
                    A('</prov:Entity>'); A('')

        # ---- terms per stage, by the kind they had AT THAT STAGE ------------
        # ts["typed"] is (root cq, kind) -> terms; collapse the cq dimension so a
        # stage gives kind -> terms. Pruned terms are already out of both dicts.
        ts_by_entity = {ts["entity"]: ts for ts in termsets}
        stage_kinds = {}
        for ts in termsets:
            km = {k: set() for k in TERM_KEYS}
            for (_rc, k), tset in ts["typed"].items():
                if k in km: km[k] |= (set(tset) & set(ts["terms"]))
            stage_kinds[ts["entity"]] = km
        all_terms = {}                    # term -> every kind it ever had, any stage
        for ts in termsets:
            for t in ts["terms"]: all_terms.setdefault(t, set())
            for k, tset in stage_kinds[ts["entity"]].items():
                for t in tset: all_terms.setdefault(t, set()).add(k)
        atomic_ids = {x["id"] for info in atom_detail.values() for x in info["atomics"]} \
            if (not self.no_atomic and atom_act) else set()

        # stage output entities + ontology versions
        for act in acts:
            if act["kind"] in ("failed",): continue
            for g in act["generated"]:
                if g == ns: continue
                A(f'<prov:Entity rdf:about="{NS(g)}">')
                if act["kind"] == "ontology":
                    vi = int(re.search(r"(\d+)$", g).group(1))
                    A(f'  <rdfs:label>Ontology draft (iteration {vi})</rdfs:label>')
                    if vi: A(f'  <prov:wasRevisionOf rdf:resource="{NS(versions[vi-1]["iri"])}"/>')
                    A(f'  <prov:wasInvalidatedBy rdf:resource="{NS(versions[vi+1]["act"])}"/>')
                    bad = [c for c in checks if c["round"] == vi + 1 and not c["passed"]]
                    A(f'  <vaem:rationale>superseded by {versions[vi+1]["act"]}'
                      f'{"; failed: " + ", ".join(escape(c["tool"]) for c in bad) if bad else ""}</vaem:rationale>')
                else:
                    A(f'  <rdfs:label>{escape(act.get("entity_label") or g)}</rdfs:label>')
                    if act.get("note"): A(f'  <vaem:rationale>{escape(act["note"])}</vaem:rationale>')
                    ts = ts_by_entity.get(g)
                    if ts is not None:        # a term stage IS the collection of its terms
                        A(f'  <rdf:type rdf:resource="{PROV}Collection"/>')
                        for t in sorted(ts["terms"]):
                            A(f'  <prov:hadMember rdf:resource="{NS("term_"+slug(t))}"/>')
                        for k in TERM_KEYS:
                            for t in sorted(stage_kinds[g][k]):
                                A(f'  <{EVP}:{TERM_KIND_PRED[k]} rdf:resource="{NS("term_"+slug(t))}"/>')
                A(f'  <prov:wasGeneratedBy rdf:resource="{NS(act["iri"])}"/>')
                if act["agent"]: A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(act["agent"]))}"/>')
                A('</prov:Entity>'); A('')

        # ---- one node per distinct term, shared by every stage that held it --
        # Which stages held it is readable from the hadMember links above; here we
        # record the name, the kinds it was ever given, the CQs it came from, and
        # the ontology element presenting the same thing (prov:alternateOf) when the
        # term reached the ontology. alternateOf, not specializationOf: neither side is
        # the more constrained one, and alternateOf is symmetric so it reads both ways.
        # the ontology document writes element IRIs with the raw Name (swo#Version1.0),
        # so slug() here would point at an IRI that is not in the file
        def elem_iri(name):
            return DNS(name if not re.search(r'[\s<>"&]', name) else slug(name))
        for t in sorted(all_terms):
            A(f'<prov:Entity rdf:about="{NS("term_"+slug(t))}">')
            A(f'  <rdf:type rdf:resource="{NS("Term")}"/>')
            A(f'  <rdfs:label>{escape(t)}</rdfs:label>')
            A(f'  <{EVP}:termName>{escape(t)}</{EVP}:termName>')
            in_onto = t in elements
            A(f'  <{EVP}:reachedOntology rdf:datatype="{XSD}boolean">{"true" if in_onto else "false"}</{EVP}:reachedOntology>')
            if in_onto: A(f'  <prov:alternateOf rdf:resource="{elem_iri(t)}"/>')
            first = next((ts for ts in termsets if t in ts["terms"]), None)
            if first:
                A(f'  <prov:wasGeneratedBy rdf:resource="{NS(first["act"])}"/>')
                if first["agent"]: A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(first["agent"]))}"/>')
            for ts in termsets:
                if t in ts["terms"] and ts is not first:
                    A(f'  <prov:wasInfluencedBy rdf:resource="{NS(ts["act"])}"/>')
            src = sorted({a for ts in termsets for a in (ts["terms"].get(t) or ()) if a})
            for c in dict.fromkeys(root_cq(a) for a in src):
                if c in all_cqs: A(f'  <prov:wasDerivedFrom rdf:resource="{NS(slug(c))}"/>')
            for a in src:
                if a in atomic_ids: A(f'  <prov:wasDerivedFrom rdf:resource="{NS(slug(a))}"/>')
            kinds = sorted(TERM_KIND_PRED[k] for k in all_terms[t])
            if kinds: A(f'  <vaem:rationale>proposed as {escape(", ".join(kinds))}</vaem:rationale>')
            A('</prov:Entity>'); A('')

        # removed / added terms at term stages ; removed elements at ontology versions
        emitted = set()
        for t, (q, p) in sorted(removed_terms.items()):
            iri = "removed_" + slug(t); emitted.add(iri)
            A(f'<prov:Entity rdf:about="{NS(iri)}">'); A(f'  <rdf:type rdf:resource="{PROV}Invalidation"/>')
            A(f'  <rdfs:label>removal of {escape(t)} at {q["act"]}</rdfs:label>')
            A(f'  <prov:hadActivity rdf:resource="{NS(q["act"])}"/>')       # the activity that performed the removal
            for ts, introduced in stage_chain(t):
                A(f'  <prov:wasInfluencedBy rdf:resource="{NS(ts["act"])}"/>')
                if introduced and ts["agent"]: A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(ts["agent"]))}"/>')
            A(f'  <prov:wasInvalidatedBy rdf:resource="{NS(q["act"])}"/>')
            note = f'[{escape(q["agent"])}{" | " + escape(q["tool"]) if q["tool"] else ""}] removed: present after {p["act"]}, absent after {q["act"]}'
            if t in q.get("pruned", []): note += f' ({escape(q.get("prune_note", ""))})'
            if t in elements: note += "; re-introduced in the ontology"
            A(f'  <vaem:rationale>{note}</vaem:rationale>'); A('</prov:Entity>'); A('')
        for t, q in sorted(added_terms.items()):
            A(f'<prov:Entity rdf:about="{NS("added_"+slug(t))}">'); A(f'  <rdf:type rdf:resource="{PROV}Generation"/>')
            A(f'  <rdfs:label>introduction of {escape(t)} at {q["act"]}</rdfs:label>')
            A(f'  <prov:hadActivity rdf:resource="{NS(q["act"])}"/>')       # the activity that introduced the term
            A(f'  <prov:wasGeneratedBy rdf:resource="{NS(q["act"])}"/>')
            if q["agent"]: A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(q["agent"]))}"/>')
            for old in sorted(replaced_by.get(t, set())): A(f'  <prov:wasRevisionOf rdf:resource="{NS("removed_"+slug(old))}"/>')
            A(f'  <vaem:rationale>[{escape(q["agent"])}] introduced: absent before {q["act"]}, present after</vaem:rationale>')
            A('</prov:Entity>'); A('')
        removed_elem_iri = {}
        for nm, vi in sorted(removed_elems.items()):
            iri = "removed_" + slug(nm)
            if iri in emitted: iri = f"removed_{slug(nm)}_v{vi}"
            removed_elem_iri[nm] = iri
            v = versions[vi]; el = versions[vi - 1]["elements"][nm]
            A(f'<prov:Entity rdf:about="{NS(iri)}">'); A(f'  <rdf:type rdf:resource="{PROV}Invalidation"/>')
            A(f'  <rdfs:label>removal of {escape(nm)} ({escape(el.get("Type") or "element")}) at {v["act"]}</rdfs:label>')
            A(f'  <prov:hadActivity rdf:resource="{NS(v["act"])}"/>')
            for c in elem_cqs(el)[0]: A(f'  <prov:wasDerivedFrom rdf:resource="{DNS(slug(c)) if False else NS(slug(c))}"/>')
            A(f'  <prov:wasInvalidatedBy rdf:resource="{NS(v["act"])}"/>')
            for r in trigger_reports(vi): A(f'  <prov:wasDerivedFrom rdf:resource="{NS(r)}"/>')
            note = next((str(x) for x in v["removed_list"] if nm in str(x)), "")
            A(f'  <vaem:rationale>[{escape(v["agent"])}{" | " + escape(v["tool"]) if v["tool"] else ""}] removed{": " + escape(note) if note else ""}</vaem:rationale>')
            A('</prov:Entity>'); A('')

        # per-axiom provenance
        axiomgen = {}
        for act in acts:
            for ax in act.get("axioms") or []: axiomgen.setdefault(ax["axiom"].strip(), (act, ax["cq"]))
        ax_i = 0
        def emit_axiom(owner, text, vi_created, vi_removed=None):
            nonlocal ax_i
            iri = (f"ax_{ax_i:03d}_{slug(owner)}" if vi_removed is None else f"ax_removed_{ax_i:03d}_{slug(owner)}"); ax_i += 1
            pred, obj = parse_axiom(text) if " type " not in text else ("rdf:type", text.split(" type ", 1)[1])
            owner_live = owner in elements
            owner_ref = DNS(slug(owner)) if owner_live else NS(removed_elem_iri.get(owner, "removed_" + slug(owner)))
            A(f'<prov:Entity rdf:about="{NS(iri)}">'); A(f'  <rdfs:label>{escape(text)}</rdfs:label>')
            A(f'  <rdf:subject rdf:resource="{owner_ref}"/>')
            if pred: A(f'  <rdf:predicate rdf:resource="{PURI[pred]}"/>')
            if obj and " some " not in obj:
                obj_ref = XSD + obj if obj in PROV_XSD_TYPES else (DNS(slug(obj)) if obj in elements else NS(slug(obj)))
                A(f'  <rdf:object rdf:resource="{obj_ref}"/>')
            elif obj: A(f'  <rdf:object>{escape(obj)}</rdf:object>')
            A(f'  <prov:wasQuotedFrom rdf:resource="{owner_ref}"/>')
            v = versions[vi_created]
            A(f'  <prov:wasGeneratedBy rdf:resource="{NS(v["act"])}"/>')
            if v["agent"]: A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(v["agent"]))}"/>')
            cqs = []
            if text.strip() in axiomgen:
                g_act, g_cq = axiomgen[text.strip()]
                A(f'  <prov:wasGeneratedBy rdf:resource="{NS(g_act["iri"])}"/>')
                if g_act["agent"]: A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(g_act["agent"]))}"/>')
                if g_cq: cqs.append(root_cq(g_cq))
            if vi_created > 0:
                for r in trigger_reports(vi_created): A(f'  <prov:wasDerivedFrom rdf:resource="{NS(r)}"/>')
                fixes = [cq for frag, cq in fail_map.get(vi_created, {}).items() if frag in text or text in frag]
                cqs += fixes
                A(f'  <vaem:rationale>added by [{escape(v["agent"])}{" | " + escape(v["tool"]) if v["tool"] else ""}] at {v["act"]}'
                  f'{" to fix failing test(s): " + ", ".join(sorted(set(fixes))) if fixes else ""}</vaem:rationale>')
            owner_el = (versions[vi_removed - 1] if vi_removed is not None else final)["elements"].get(owner) or {}
            cqs += elem_cqs(owner_el)[0]
            for c in dict.fromkeys(cqs): A(f'  <prov:wasDerivedFrom rdf:resource="{NS(slug(c))}"/>')
            if vi_removed is not None:
                rv = versions[vi_removed]
                A(f'  <prov:wasInvalidatedBy rdf:resource="{NS(rv["act"])}"/>')
                for r in trigger_reports(vi_removed): A(f'  <prov:wasDerivedFrom rdf:resource="{NS(r)}"/>')
                A(f'  <vaem:rationale>removed by [{escape(rv["agent"])}{" | " + escape(rv["tool"]) if rv["tool"] else ""}] at {rv["act"]}</vaem:rationale>')
            A('</prov:Entity>'); A('')
        for name, el in elements.items():
            for ax in el.get("Axioms") or []: emit_axiom(name, str(ax), ax_origin[(name, str(ax))])
            if el.get("InstanceOf"): emit_axiom(name, f"{name} type {el['InstanceOf']}", ax_origin[(name, f"{name} type {el['InstanceOf']}")])
        for (name, ax), vi in ax_removed.items():
            if (name, ax) not in axset(final): emit_axiom(name, ax, ax_origin[(name, ax)], vi)
        present = {str(ax).strip() for el in elements.values() for ax in el.get("Axioms") or []}
        n_left = 0
        for act in acts:
            for ax in act.get("axioms") or []:
                if ax["axiom"].strip() and ax["axiom"].strip() not in present:
                    A(f'<prov:Entity rdf:about="{NS(f"axiom_{n_left:03d}")}">'); n_left += 1
                    A(f'  <rdfs:label>{escape(ax["axiom"])}</rdfs:label>'); A(f'  <prov:wasGeneratedBy rdf:resource="{NS(act["iri"])}"/>')
                    if act["agent"]: A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(act["agent"]))}"/>')
                    if ax["cq"]: A(f'  <prov:wasDerivedFrom rdf:resource="{NS(slug(root_cq(ax["cq"])))}"/>')
                    A(f'  <vaem:rationale>proposed by [{escape(act["agent"])}] but not materialised in the ontology</vaem:rationale>')
                    A('</prov:Entity>'); A('')
            n_t = 0
            for t in act.get("tests") or []:
                A(f'<prov:Entity rdf:about="{NS(f"test_{n_t:03d}")}">'); n_t += 1
                A(f'  <rdfs:label>{escape(t["test"])}</rdfs:label>'); A(f'  <prov:value>{escape(t["test"])}</prov:value>')
                A(f'  <prov:wasGeneratedBy rdf:resource="{NS(act["iri"])}"/>')
                if act["agent"]: A(f'  <prov:wasAttributedTo rdf:resource="{NS("agent_"+slug(act["agent"]))}"/>')
                if t["cq"]: A(f'  <prov:wasDerivedFrom rdf:resource="{NS(slug(root_cq(t["cq"])))}"/>')
                A('</prov:Entity>'); A('')

        # activities
        for act in acts:
            A(f'<prov:Activity rdf:about="{NS(act["iri"])}">'); A(f'  <rdfs:label>{escape(act["label"])}</rdfs:label>')
            used = list(act["used"])
            if act["kind"] in ("atomization",) or (act["kind"] in ("terms", "axioms", "tests", "ontology") and not used):
                used += [slug(c) for c in all_cqs]
            if act["kind"] in ("axioms", "tests"): used += [slug(c) for c in all_cqs if slug(c) not in used]
            for u in dict.fromkeys(used): A(f'  <prov:used rdf:resource="{u if u.startswith("http") else NS(u)}"/>')
            for g in act["generated"]: A(f'  <prov:generated rdf:resource="{g if g.startswith("http") else NS(g)}"/>')
            for ib in dict.fromkeys(act["informed"]): A(f'  <prov:wasInformedBy rdf:resource="{NS(ib)}"/>')
            if act["agent"]: A(f'  <prov:wasAssociatedWith rdf:resource="{NS("agent_"+slug(act["agent"]))}"/>')
            if act["tool"] and not (act["kind"] == "ontology" and act.get("correction_round")):
                A(f'  <prov:wasAssociatedWith rdf:resource="{NS("tool_"+slug(act["tool"]))}"/>')
            A(f'  <prov:hadRole rdf:resource="{NS(act["role"])}"/>'); A(f'  <prov:hadPlan rdf:resource="{NS(act["plan"])}"/>')
            if act["agent"]:                       # PROV-correct form: the role belongs to the agent-activity association
                A('  <prov:qualifiedAssociation>'); A('    <prov:Association>')
                A(f'      <prov:agent rdf:resource="{NS("agent_"+slug(act["agent"]))}"/>')
                A(f'      <prov:hadRole rdf:resource="{NS(act["role"])}"/>')
                A('    </prov:Association>'); A('  </prov:qualifiedAssociation>')
            if act.get("reason"): A(f'  <vaem:rationale>{escape(act["reason"])}</vaem:rationale>')
            if act.get("error") and act["kind"] != "failed" and not act.get("trigger_checks"):
                A(f'  <dc:source>(error_message{" | " + escape(act["tool"]) if act["tool"] else ""}) {escape(act["error"])}</dc:source>')
            if act["kind"] == "ontology" and act.get("correction_round"):     # a correction (round >= 1)
                A(f'  <{EVP}:correctionRound rdf:datatype="{XSD}integer">{act["correction_round"]}</{EVP}:correctionRound>')
                A(f'  <{EVP}:correctionOutcome>{escape(act["correction_outcome"])}</{EVP}:correctionOutcome>')
                A(f'  <{EVP}:ofRun rdf:resource="{NS("run_"+slug(run_id))}"/>')
                for tl in act.get("trigger_tools", []): A(f'  <{EVP}:triggeredBy rdf:resource="{NS("tool_"+slug(tl))}"/>')
            if act["kind"] == "failed":
                A(f'  <{EVP}:attempt rdf:datatype="{XSD}integer">{act.get("attempt_no", 1)}</{EVP}:attempt>')
                A(f'  <{EVP}:stage>{escape(act.get("stage",""))}</{EVP}:stage>')
                A(f'  <{EVP}:checkOutcome>failed</{EVP}:checkOutcome>')
                A(f'  <{EVP}:ofRun rdf:resource="{NS("run_"+slug(run_id))}"/>')
            A('</prov:Activity>')
            if act["kind"] == "failed":
                # the successful (re)try that superseded this attempt, if any
                superseded_by = next((x["iri"] for x in acts if x["kind"] in ("ontology", "terms", "axioms", "tests", "atomization", "other")
                                      and x["agent"] == act["agent"] and act["iri"] in x.get("retries", [])), None)
                A(f'<earl:TestResult rdf:about="{NS(act["report_iri"])}">'); A(f'  <rdf:type rdf:resource="{PROV}Entity"/>')
                A(f'  <earl:outcome rdf:resource="{EARL}failed"/>'); A(f'  <prov:wasGeneratedBy rdf:resource="{NS(act["iri"])}"/>')
                A(f'  <{EVP}:supersededByRetry rdf:datatype="{XSD}boolean">{"true" if superseded_by else "false"}</{EVP}:supersededByRetry>')
                if superseded_by:
                    A(f'  <prov:wasInvalidatedBy rdf:resource="{NS(superseded_by)}"/>')
                    A(f'  <vaem:rationale>rejected attempt; superseded by {superseded_by}</vaem:rationale>')
                if act["error"]: A(f'  <prov:value>{escape(act["error"])}</prov:value>')
                A('</earl:TestResult>')
            A('')
        # validation rounds
        rounds = {}
        for c in checks: rounds.setdefault(c["round"], []).append(c)
        # a "round" in the log means an ontology version; but loop vs verify (the phase
        # field) are distinct validation passes over the same version. Split each round
        # by phase so "the last verify round" is a well-defined, queryable node.
        round_phase = {}   # (round, phase) -> [checks]  preserving order of first appearance
        order = []
        for c in checks:
            ph = c["meta"].get("phase") or "loop"
            key = (c["round"], ph)
            if key not in round_phase: round_phase[key] = []; order.append(key)
            round_phase[key].append(c)
        verify_keys = [k for k in order if k[1] == "verify"]
        last_verify = verify_keys[-1] if verify_keys else None
        vround_iri = {k: f"ValidationRound{k[0]}_{slug(k[1])}" for k in order}
        for k in order:
            rno, ph = k; cs = round_phase[k]; ver = versions[min(rno - 1, len(versions) - 1)]
            idx = order.index(k)
            prev = [ver["act"]] + ([vround_iri[order[idx-1]]] if idx > 0 else [])
            passed = all(c["passed"] for c in cs)
            A(f'<prov:Activity rdf:about="{NS(vround_iri[k])}">')
            A(f'  <rdfs:label>Validation round {rno} / {ph} of {ver["iri"] if ver["iri"] != ns else "the final ontology"}</rdfs:label>')
            A(f'  <prov:used rdf:resource="{ver["iri"] if ver["iri"].startswith("http") else NS(ver["iri"])}"/>')
            for p in prev: A(f'  <prov:wasInformedBy rdf:resource="{NS(p)}"/>')
            A(f'  <prov:hadRole rdf:resource="{NS("ValidatorRole")}"/>'); A(f'  <prov:hadPlan rdf:resource="{NS("ValidationPlan")}"/>')
            A(f'  <{EVP}:roundIndex rdf:datatype="{XSD}integer">{rno}</{EVP}:roundIndex>')
            A(f'  <{EVP}:phase>{escape(ph)}</{EVP}:phase>')
            A(f'  <{EVP}:isVerifyRound rdf:datatype="{XSD}boolean">{"true" if ph == "verify" else "false"}</{EVP}:isVerifyRound>')
            A(f'  <{EVP}:isFinalRound rdf:datatype="{XSD}boolean">{"true" if k == order[-1] else "false"}</{EVP}:isFinalRound>')
            A(f'  <{EVP}:roundPassed rdf:datatype="{XSD}boolean">{"true" if passed else "false"}</{EVP}:roundPassed>')
            A(f'  <{EVP}:ofRun rdf:resource="{NS("run_"+slug(run_id))}"/>')
            A('</prov:Activity>')
            A(f'<earl:TestResult rdf:about="{NS("Report_"+vround_iri[k])}">'); A(f'  <rdf:type rdf:resource="{PROV}Entity"/>')
            A(f'  <earl:outcome rdf:resource="{EARL}{"passed" if passed else "failed"}"/>')
            A(f'  <prov:wasGeneratedBy rdf:resource="{NS(vround_iri[k])}"/>'); A('</earl:TestResult>'); A('')
            for c in cs:
                mt = ", ".join(f"{kk} {v}" for kk, v in c["meta"].items())
                A(f'<prov:Activity rdf:about="{NS(c["iri"])}">')
                A(f'  <rdfs:label>{escape(c["tool"])} (round {rno} / {ph}{", " + escape(mt) if mt else ""}, step {c["step"]})</rdfs:label>')
                A(f'  <prov:wasInformedBy rdf:resource="{NS(vround_iri[k])}"/>')
                A(f'  <prov:used rdf:resource="{ver["iri"] if ver["iri"].startswith("http") else NS(ver["iri"])}"/>')
                if "test" in c["tool"].lower() and "tests" in latest: A(f'  <prov:used rdf:resource="{NS(latest["tests"])}"/>')
                A(f'  <prov:wasAssociatedWith rdf:resource="{NS("tool_"+slug(c["tool"]))}"/>')
                A(f'  <prov:hadRole rdf:resource="{NS("ValidatorRole")}"/>')
                A(f'  <{EVP}:toolName>{escape(c["tool"])}</{EVP}:toolName>')
                A(f'  <{EVP}:roundIndex rdf:datatype="{XSD}integer">{rno}</{EVP}:roundIndex>')
                A(f'  <{EVP}:phase>{escape(ph)}</{EVP}:phase>')
                if "attempt" in c["meta"]: A(f'  <{EVP}:attempt rdf:datatype="{XSD}integer">{c["meta"]["attempt"]}</{EVP}:attempt>')
                A(f'  <{EVP}:checkOutcome>{"passed" if c["passed"] else "failed"}</{EVP}:checkOutcome>')
                A(f'  <{EVP}:ofRun rdf:resource="{NS("run_"+slug(run_id))}"/>')
                A('</prov:Activity>')
                A(f'<earl:TestResult rdf:about="{NS(c["report_iri"])}">'); A(f'  <rdf:type rdf:resource="{PROV}Entity"/>')
                A(f'  <earl:outcome rdf:resource="{EARL}{"passed" if c["passed"] else "failed"}"/>')
                A(f'  <prov:wasGeneratedBy rdf:resource="{NS(c["iri"])}"/>')
                txt = (c["report"] + ("\n" + c["error"] if c["error"] and c["error"].strip() != c["report"].strip() else "")).strip()
                if txt: A(f'  <prov:value>{escape(txt)}</prov:value>')
                A('</earl:TestResult>'); A('')

        # ---- PipelineRun node: anchors this run; success is derived by query, not stored ----
        A(f'<owl:NamedIndividual rdf:about="{NS("run_"+slug(run_id))}">')
        A(f'  <rdf:type rdf:resource="{NS("PipelineRun")}"/>')
        A(f'  <{EVP}:runId>{escape(run_id)}</{EVP}:runId>')
        A(f'  <rdfs:label>pipeline run {escape(run_id)}</rdfs:label>')
        A(f'  <prov:wasAttributedTo rdf:resource="{NS(self.pipeline)}"/>')
        if last_verify: A(f'  <{EVP}:isFinalRound rdf:resource="{NS(vround_iri[last_verify])}"/>')
        A('</owl:NamedIndividual>'); A('')

        agent_roles = {}
        for act in acts:
            if act["agent"]: agent_roles.setdefault(act["agent"], []).append(act["role"])
        for name in agents:
            A(f'<prov:SoftwareAgent rdf:about="{NS("agent_"+slug(name))}">'); A(f'  <rdfs:label>{escape(name)}</rdfs:label>')
            A(f'  <prov:actedOnBehalfOf rdf:resource="{NS(self.pipeline)}"/>')
            for r in dict.fromkeys(agent_roles.get(name, [role_for(name)])):
                A(f'  <prov:hadRole rdf:resource="{NS(r)}"/>')
            A('</prov:SoftwareAgent>'); A('')
        A(f'<prov:SoftwareAgent rdf:about="{NS(self.pipeline)}">'); A(f'  <rdfs:label>{escape(self.pipeline)} pipeline</rdfs:label>'); A('</prov:SoftwareAgent>'); A('')
        for iri, cls, lbl in [("GeneratorRole", "Role", "generator"), ("RefinementRole", "Role", "refinement"),
                              ("FixRole", "Role", "fix"), ("ValidatorRole", "Role", "validator"),
                              ("GenerationPrompt", "Plan", "generation prompt"), ("CorrectionPrompt", "Plan", "correction prompt"),
                              ("ValidationPlan", "Plan", "validation plan")]:
            A(f'<owl:NamedIndividual rdf:about="{NS(iri)}">'); A(f'  <rdf:type rdf:resource="{PROV}{cls}"/>'); A(f'  <rdfs:label>{lbl}</rdfs:label>')
            if iri in ROLE_DOC: A(f'  <rdfs:comment>{escape(ROLE_DOC[iri])}</rdfs:comment>')
            A('</owl:NamedIndividual>'); A('')
        for t in tools:
            A(f'<prov:SoftwareAgent rdf:about="{NS("tool_"+slug(t))}">'); A(f'  <rdfs:label>{escape(t)}</rdfs:label>')
            A(f'  <prov:actedOnBehalfOf rdf:resource="{NS(self.pipeline)}"/>'); A('</prov:SoftwareAgent>'); A('')

        owl = owl.replace("</rdf:RDF>", "\n".join(L) + "\n</rdf:RDF>")
        self.stats = {"activities": [x["iri"] for x in acts], "versions": len(versions),
                      "iteration": iteration, "checks": len(checks),
                      "elements_in_owl": len(seen_blocks), "elements_in_log": len(elements),
                      "removed_terms": sorted(removed_terms), "added_terms": sorted(added_terms),
                      "warned": warned}
        return owl
