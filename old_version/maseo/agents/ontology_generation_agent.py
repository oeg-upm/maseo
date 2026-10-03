from agno.agent import Agent

from config import Config
from agents import build_agent


AGENT_KEY = "ontology_generation"


def create(config: Config) -> Agent:
    """Instantiate the Ontology Generation Agent from config."""
    return build_agent(config, AGENT_KEY)
