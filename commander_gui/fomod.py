"""Small, XML-based FOMOD parser used by the Mod Manager wizard."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from xml.parsers import expat

from .mod_install import ModInstallError


@dataclass(frozen=True)
class FomodFile:
    source: str
    destination: str = ""
    is_folder: bool = False


@dataclass(frozen=True)
class FomodOption:
    name: str
    description: str = ""
    files: tuple[FomodFile, ...] = ()


@dataclass(frozen=True)
class FomodGroup:
    name: str
    group_type: str
    options: tuple[FomodOption, ...]


@dataclass(frozen=True)
class FomodStep:
    name: str
    groups: tuple[FomodGroup, ...]


@dataclass(frozen=True)
class FomodConfig:
    name: str
    author: str
    steps: tuple[FomodStep, ...]


def _text(element: ET.Element | None, default: str = "") -> str:
    return (element.text or default).strip() if element is not None else default


def _named(element: ET.Element, child: str, default: str) -> str:
    """Read a standard name attribute, with compatibility for child elements."""
    return element.attrib.get("name", "").strip() or _text(element.find(child), default)


def _reject_doctype(*_args: object, **_kwargs: object) -> None:
    raise ET.ParseError("DOCTYPE declarations are not allowed in a FOMOD ModuleConfig.xml")


def _parse_untrusted_xml(path: Path) -> ET.Element:
    """Parse a mod-archive-supplied XML file without expanding DTD entities.

    ``ModuleConfig.xml`` comes from a third-party mod archive - untrusted
    input. Plain ``ET.parse`` has no protection against a "billion laughs"
    style entity-expansion bomb in a malicious or corrupted archive, which
    can freeze or OOM the GUI thread. Parsing directly through ``expat``
    (rather than ``ET.XMLParser``, whose internal parser handle is not a
    stable public attribute across Python versions) disables parameter
    entities and refuses any DOCTYPE outright - real FOMOD configs never
    have one, so this rejects only malicious/malformed input.
    """
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _reject_doctype
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    with open(path, "rb") as handle:
        parser.ParseFile(handle)
    return builder.close()


def parse_config(path: Path) -> FomodConfig:
    """Parse the common ModuleConfig.xml subset used by most FOMODs."""
    try:
        root = _parse_untrusted_xml(path)
    except (OSError, ET.ParseError, expat.ExpatError) as exc:
        raise ModInstallError(f"Could not read FOMOD configuration: {exc}") from exc
    module = root.find("moduleName")
    author = root.find("author")
    steps: list[FomodStep] = []
    install_steps = root.find("installSteps")
    for step in (
        install_steps.findall("installStep") if install_steps is not None else []
    ):
        groups: list[FomodGroup] = []
        for group in step.findall("optionalFileGroups/group"):
            options: list[FomodOption] = []
            for option in group.findall("plugins/plugin"):
                files: list[FomodFile] = []
                files_node = option.find("files")
                for file_node in (
                    files_node.findall("file") if files_node is not None else []
                ):
                    files.append(
                        FomodFile(
                            source=file_node.attrib.get("source", "").strip(),
                            destination=file_node.attrib.get("destination", "").strip(),
                        )
                    )
                for folder_node in (
                    files_node.findall("folder") if files_node is not None else []
                ):
                    files.append(
                        FomodFile(
                            source=folder_node.attrib.get("source", "").strip(),
                            destination=folder_node.attrib.get(
                                "destination", ""
                            ).strip(),
                            is_folder=True,
                        )
                    )
                options.append(
                    FomodOption(
                        name=_named(option, "name", "Unnamed option"),
                        description=_text(option.find("description")),
                        files=tuple(files),
                    )
                )
            groups.append(
                FomodGroup(
                    name=_named(group, "name", "Options"),
                    group_type=group.attrib.get("type", "SelectAny"),
                    options=tuple(options),
                )
            )
        steps.append(
            FomodStep(
                _named(step, "name", "Installation options"),
                tuple(groups),
            )
        )
    if not steps:
        raise ModInstallError("This FOMOD has no supported installation steps")
    return FomodConfig(_text(module, "FOMOD installation"), _text(author), tuple(steps))


def apply_options(
    config: FomodConfig,
    root: Path,
    destination: Path,
    selected: dict[tuple[int, int], list[int]],
) -> None:
    """Copy the selected FOMOD files into the staged mod directory."""
    if root.is_symlink() or destination.is_symlink():
        raise ModInstallError("FOMOD destination root is a symlink")
    root = root.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    if not destination.is_dir():
        raise ModInstallError("FOMOD destination is not a directory")
    for step_index, step in enumerate(config.steps):
        for group_index, group in enumerate(step.groups):
            for option_index in selected.get((step_index, group_index), []):
                if not isinstance(option_index, int) or not 0 <= option_index < len(group.options):
                    raise ModInstallError(
                        f"Invalid FOMOD option selection in step {step_index}, group {group_index}"
                    )
                option = group.options[option_index]
                for item in option.files:
                    source_name = item.source.replace("\\", "/")
                    source = (root / source_name).resolve()
                    try:
                        source.relative_to(root)
                    except ValueError as exc:
                        raise ModInstallError(
                            "FOMOD file escapes the archive root"
                        ) from exc
                    if item.is_folder and not source.is_dir():
                        raise ModInstallError(f"FOMOD folder is missing: {item.source}")
                    if not item.is_folder and not source.is_file():
                        raise ModInstallError(f"FOMOD file is missing: {item.source}")
                    components = (
                        item.destination.replace("\\", "/").split("/")
                        if item.destination
                        else []
                    )
                    if (
                        item.destination.startswith(("/", "\\"))
                        or any(
                            not component
                            or component in {".", ".."}
                            or any(ord(char) < 32 or ord(char) == 127 for char in component)
                            for component in components
                        )
                    ):
                        raise ModInstallError("FOMOD destination contains unsafe components")
                    target = destination.joinpath(*components, source.name)
                    current = destination
                    for component in components:
                        current = current / component
                        if current.is_symlink():
                            raise ModInstallError("FOMOD destination contains a symlink")
                        try:
                            current.mkdir(exist_ok=True)
                        except FileExistsError as exc:
                            raise ModInstallError(
                                f"FOMOD destination path component '{component}' "
                                "already exists as a file"
                            ) from exc
                        if current.is_symlink():
                            raise ModInstallError("FOMOD destination contains a symlink")
                    target = target.resolve()
                    try:
                        target.relative_to(destination)
                    except ValueError as exc:
                        raise ModInstallError(
                            "FOMOD destination escapes the mod folder"
                        ) from exc
                    if target.exists() and target.is_symlink():
                        raise ModInstallError("FOMOD destination is a symlink")
                    if item.is_folder:
                        shutil.copytree(source, target, dirs_exist_ok=True)
                    else:
                        shutil.copy2(source, target)
