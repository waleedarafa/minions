from __future__ import annotations

from enum import Enum

import yaml
from pydantic import BaseModel, Field, model_validator

import structlog

logger = structlog.get_logger()


class NodeType(str, Enum):
    deterministic = "deterministic"
    agentic = "agentic"


class BlueprintNode(BaseModel):
    name: str
    type: NodeType
    # Deterministic fields
    action: str | None = None
    timeout: str | None = None
    template: str | None = None
    # Agentic fields
    prompt: str | None = None
    tools: list[str] = Field(default_factory=list)
    max_iterations: int = 50
    # Shared
    condition: str | None = None

    @model_validator(mode="after")
    def check_type_fields(self) -> BlueprintNode:
        if self.type == NodeType.deterministic and not self.action:
            raise ValueError(f"Deterministic node '{self.name}' must have an 'action'")
        if self.type == NodeType.agentic and not self.prompt:
            raise ValueError(f"Agentic node '{self.name}' must have a 'prompt'")
        return self


class Blueprint(BaseModel):
    name: str
    description: str = ""
    max_ci_rounds: int = 2
    max_tokens: int = 200_000
    max_wall_clock_seconds: int = 3600
    steps: list[BlueprintNode]

    @classmethod
    def from_yaml(cls, yaml_content: str) -> Blueprint:
        data = yaml.safe_load(yaml_content)
        return cls(**data)

    @classmethod
    def from_file(cls, path: str) -> Blueprint:
        with open(path) as f:
            return cls.from_yaml(f.read())

    def get_step(self, name: str) -> BlueprintNode | None:
        return next((s for s in self.steps if s.name == name), None)


class BlueprintRegistry:
    def __init__(self) -> None:
        self._blueprints: dict[str, Blueprint] = {}

    def register(self, blueprint: Blueprint) -> None:
        self._blueprints[blueprint.name] = blueprint
        logger.info("blueprint_registered", name=blueprint.name, steps=len(blueprint.steps))

    def get(self, name: str) -> Blueprint | None:
        return self._blueprints.get(name)

    def list(self) -> list[str]:
        return list(self._blueprints.keys())

    def load_from_directory(self, directory: str) -> int:
        """Load all .yaml files from a directory. Returns count loaded."""
        import os
        count = 0
        for filename in os.listdir(directory):
            if filename.endswith(".yaml") or filename.endswith(".yml"):
                path = os.path.join(directory, filename)
                try:
                    bp = Blueprint.from_file(path)
                    self.register(bp)
                    count += 1
                except Exception as exc:
                    logger.error("blueprint_load_error", file=filename, error=str(exc))
        return count
