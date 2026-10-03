from agno.agent import Agent

from config import Config
from agents import build_agent


AGENT_KEY = "logical_consistency"


def create(config: Config) -> Agent:
    """Instantiate the Logical Consistency Agent from config."""
    return build_agent(config, AGENT_KEY)
