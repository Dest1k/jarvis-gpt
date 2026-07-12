#!/usr/bin/env python3
"""
Knowledge Graph - Improved version
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
        self.entities: Dict[str, Entity] = {}
        self.relations: List[Relation] = []

    def add_entity(self, entity: Entity):
        self.entities[entity.id] = entity

    def add_relation(self, relation: Relation):
        self.relations.append(relation)

    def query(self, query: str) -> List[Dict[str, Any]]:
        results = []
        for e in self.entities.values():
            if query.lower() in e.name.lower():
                results.append({"id": e.id, "name": e.name, "type": e.type})
        return results

    def build_from_documents(self, chunks: List[str]):
        # Placeholder for LLM-assisted extraction
        for i, chunk in enumerate(chunks[:5]):
            self.add_entity(Entity(f"ent_{i}", f"Entity from chunk {i}", "concept"))


def get_knowledge_graph_tools():
    kg = KnowledgeGraph()
    return {
        "memory.graph_query": kg.query,
        "memory.build_graph_from_docs": kg.build_from_documents,
    }

print("[knowledge_graph.py] Improved.")