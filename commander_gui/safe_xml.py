"""Parse XML that came from outside COMMANDER without expanding entities.

Used for anything not written by COMMANDER itself: a mod archive's FOMOD
``ModuleConfig.xml`` and the GitHub release feed. Plain ``ElementTree`` has
no protection against a "billion laughs" entity-expansion bomb, which can
freeze or run the GUI out of memory. Parsing straight through ``expat``
(rather than ``ET.XMLParser``, whose internal parser handle is not a stable
public attribute across Python versions) turns off parameter entities and
refuses any DOCTYPE outright - neither real FOMOD configs nor Atom feeds
have one, so only malicious or malformed input is turned away.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.parsers import expat

#: What a caller catches: a read or parse failure of any kind.
XML_ERRORS = (OSError, ET.ParseError, expat.ExpatError)


def _reject_doctype(*_args: object, **_kwargs: object) -> None:
    raise ET.ParseError("DOCTYPE declarations are not allowed here")


def _parser(namespaces: bool) -> tuple[expat.XMLParserType, ET.TreeBuilder]:
    builder = ET.TreeBuilder()
    if namespaces:
        # ElementTree's own spelling: "{uri}tag". expat reports "uri}tag"
        # with this separator, so only the opening brace is missing.
        parser = expat.ParserCreate(namespace_separator="}")

        def _name(name: str) -> str:
            return "{" + name if "}" in name else name

        def _start(tag: str, attrs: dict[str, str]) -> None:
            builder.start(_name(tag), {_name(k): v for k, v in attrs.items()})

        def _end(tag: str) -> None:
            builder.end(_name(tag))
    else:
        parser = expat.ParserCreate()
        _start, _end = builder.start, builder.end
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _reject_doctype
    parser.StartElementHandler = _start
    parser.EndElementHandler = _end
    parser.CharacterDataHandler = builder.data
    return parser, builder


def parse_file(path: Path, *, namespaces: bool = False) -> ET.Element:
    """``namespaces`` off by default: FOMOD configs are read by plain tag
    names, as they always have been."""
    parser, builder = _parser(namespaces)
    with open(path, "rb") as handle:
        parser.ParseFile(handle)
    return builder.close()


def parse_bytes(data: bytes, *, namespaces: bool = False) -> ET.Element:
    """With ``namespaces``, tags read ``{uri}name`` exactly as
    ``ElementTree.fromstring`` gives them - which the Atom release feed's
    lookups need."""
    parser, builder = _parser(namespaces)
    parser.Parse(data, True)
    return builder.close()
