from __future__ import annotations

import pytest
from jarvis_gpt.document_runtime import DocumentRuntimeError, _parse_xml


def test_parse_xml_rejects_dtd_and_entity_expansion() -> None:
    payload = """<!DOCTYPE root [<!ENTITY x "expanded">]><root>&x;</root>"""

    with pytest.raises(DocumentRuntimeError, match="DTD and entity"):
        _parse_xml(payload, "test document")


def test_parse_xml_accepts_plain_office_xml() -> None:
    root = _parse_xml("<root><value>safe</value></root>", "test document")

    assert root.findtext("value") == "safe"
