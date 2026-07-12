#!/usr/bin/env python3
"""
Knowledge Graph - Continued
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any


@dataclass
class Entity:
    id: str
    name: str
    type: str
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Relation:
    source: str
    target: str
    type: str
    strength: float = 1.0


class KnowledgeGraph:
    def __init__(self):
        self.entities = {}
        self.relations = []

    def add_entity(self, entity):
        self.entities[entity.id] = entity

    def add_relation(self, relation):
        self.relations.append(relation)

    def query(self, query: str):
        return [{"name": e.name} for e in self.entities.values() if query.lower() in e.name.lower()]

    def build_from_documents(self, chunks):
        for i in range(min(7, len(chunks))):
            self.add_entity(Entity(f"e{i}", f"Entity {i}", "concept"))


def get_knowledge_graph_tools():
    kg = KnowledgeGraph()
    return {
        "memory.graph_query": kg.query,
        "memory.build_graph_from_docs": kg.build_from_documents,
    }

print("[knowledge_graph.py] Continued.")