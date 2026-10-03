from agno.agent import Agent

from config import Config
from agents import build_agent


AGENT_KEY = "pitfall_resolution"


def create(config: Config) -> Agent:
    """Instantiate the Pitfall Resolution Agent from config."""
    return build_agent(config, AGENT_KEY)
