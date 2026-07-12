# Integration Guide — Ideal Jarvis / Document Surfer

## Production (ready)

### 1. Document surfer is already wired

`backend/src/jarvis_gpt/tools.py` registers the full `documents.*` surface.
`backend/src/jarvis_gpt/agent.py` includes the new tools in the safe agent allowlist
and mission executor prompt.

No extra registration call is required for document capabilities.

### 2. Optional low-level API

```python
from pathlib import Path
from jarvis_gpt.document_surfer import DocumentSurferConfig, JarvisDocumentSurfer

with JarvisDocumentSurfer(DocumentSurferConfig(output_dir=Path("out"))) as surfer:
    report = surfer.analyze("contract.docx")
    pack = surfer.generate(title="Summary", body=report["text_preview"], output_format="docx")
```

### 3. Document agent facade

```python
from jarvis_gpt.document_agent import DocumentAgent, DocumentGenerationRequest

agent = DocumentAgent(output_dir="D:/jarvis/data/document-outputs")
doc = agent.generate(DocumentGenerationRequest(task="Weekly report", body="...", output_format="md"))
```

## Experimental modules

`vision`, `calendar_integration`, `email_integration`, `voice`, `plugins`,
`proactive_briefing`, `knowledge_graph` are **not** production Command Center tools.

`ideal_tools_registration.register_all_ideal_tools(registry)` is a best-effort helper
for diagnostics only. Prefer native `ToolRegistry` specs for anything release-critical.

## System prompt / routing

Agent prompts already mention document_surfer tools. When adding new UI routes:

1. Prefer existing `documents.*` tools over ad-hoc file reads.
2. Never instruct the model to overwrite source documents.
3. Use `documents.generate` / `documents.convert` for deliverables.

## Verification

```powershell
$env:PYTHONPATH = "backend/src"
python -m pytest backend/tests/test_document_surfer.py -q
python -c "from jarvis_gpt.tools import ToolRegistry; r=ToolRegistry(); print([t for t in r.names() if t.startswith('documents.')])"
```
