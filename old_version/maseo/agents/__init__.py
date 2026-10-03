from agno.agent import Agent
 
from config import Config
from model_factory import build_model
from models import Answer
 
def build_agent(config: Config, agent_key: str) -> Agent:

    block = config.prompt(agent_key)
    instructions = block["instructions"].format(base_uri=config.base_uri)
    return Agent(
        model=build_model(config),
        name=block["name"],
        role=block["role"],
        instructions=instructions,
        output_schema=Answer,
    )