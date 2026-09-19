"""Small, XML-based FOMOD parser used by the Mod Manager wizard."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.parsers import expat

from .mod_install import ModInstallError


@dataclass(frozen=True)
class FomodFile:
    source: str
    #: None means the <file>/<folder> element omitted its destination
    #: attribute entirely - distinct from an explicit destination="", so
    #: apply_options() can tell "default to the source's own path" (the
    #: FOMOD spec's rule for an omitted attribute) apart from "explicitly
    #: place at the mod root".
    destination: str | None = None
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
    #: Files/folders installed unconditionally, independent of any step -
    #: <requiredInstallFiles>. Many small, no-choice FOMODs ship only
    #: this, with no <installSteps> at all.
    required_files: tuple[FomodFile, ...] = ()


def _text(element: ET.Element | None, default: str = "") -> str:
    return (element.text or default).strip() if element is not None else default


def _parse_files_node(files_node: ET.Element | None) -> tuple[FomodFile, ...]:
    """Parse a <files> or <requiredInstallFiles>-shaped node's children."""
    files: list[FomodFile] = []
    if files_node is None:
        return ()
    for file_node in files_node.findall("file"):
        dest_attr = file_node.attrib.get("destination")
        files.append(
            FomodFile(
                source=file_node.attrib.get("source", "").strip(),
                destination=None if dest_attr is None else dest_attr.strip(),
            )
        )
    for folder_node in files_node.findall("folder"):
        dest_attr = folder_node.attrib.get("destination")
        files.append(
            FomodFile(
                source=folder_node.attrib.get("source", "").strip(),
                destination=None if dest_attr is None else dest_attr.strip(),
                is_folder=True,
            )
        )
    return tuple(files)


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
    required_files = _parse_files_node(root.find("requiredInstallFiles"))
    steps: list[FomodStep] = []
    install_steps = root.find("installSteps")
    for step in (
        install_steps.findall("installStep") if install_steps is not None else []
    ):
        groups: list[FomodGroup] = []
        for group in step.findall("optionalFileGroups/group"):
            options: list[FomodOption] = []
            for option in group.findall("plugins/plugin"):
                files = _parse_files_node(option.find("files"))
                options.append(
                    FomodOption(
                        name=_named(option, "name", "Unnamed option"),
                        description=_text(option.find("description")),
                        files=files,
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
    if not steps and not required_files:
        raise ModInstallError("This FOMOD has no supported installation steps")
    return FomodConfig(
        _text(module, "FOMOD installation"), _text(author), tuple(steps), required_files
    )


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
    for item in config.required_files:
        _place_item(item, root, destination)
    for step_index, step in enumerate(config.steps):
        for group_index, group in enumerate(step.groups):
            for option_index in selected.get((step_index, group_index), []):
                if not isinstance(option_index, int) or not 0 <= option_index < len(group.options):
                    raise ModInstallError(
                        f"Invalid FOMOD option selection in step {step_index}, group {group_index}"
                    )
                option = group.options[option_index]
                for item in option.files:
                    _place_item(item, root, destination)


def _place_item(item: FomodFile, root: Path, destination: Path) -> None:
    """Copy one FOMOD file/folder entry into ``destination``.

    ``root``/``destination`` are already resolved absolute paths (see
    ``apply_options``).
    """
    source_name = item.source.replace("\\", "/")
    source = (root / source_name).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise ModInstallError("FOMOD file escapes the archive root") from exc
    if item.is_folder and not source.is_dir():
        raise ModInstallError(f"FOMOD folder is missing: {item.source}")
    if not item.is_folder and not source.is_file():
        raise ModInstallError(f"FOMOD file is missing: {item.source}")
    if item.destination is None:
        # FOMOD spec: an omitted destination defaults to the item's own
        # source path (preserving any subfolders), not the mod root -
        # e.g. a <file source="gamedata/scripts/foo.script"/> with no
        # destination must land at gamedata/scripts/foo.script, not be
        # flattened to just foo.script at the mod's top level (which is
        # exactly what used to strip a mod's gamedata folder away and
        # get it flagged INVALID/red-X by MO2's own data checker).
        parent = PurePosixPath(source_name).parent
        dest_str = "" if str(parent) == "." else str(parent)
    else:
        dest_str = item.destination
    components = dest_str.replace("\\", "/").split("/") if dest_str else []
    if dest_str.startswith(("/", "\\")) or any(
        not component
        or component in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 for char in component)
        for component in components
    ):
        raise ModInstallError("FOMOD destination contains unsafe components")
    # A <folder> entry installs the SOURCE FOLDER'S CONTENTS at the
    # destination, not the folder itself nested under its own name -
    # e.g. <folder source="00 - Core" destination=""/> (a real,
    # confirmed FOMOD shape: "00 - Core" is a meaningless packaging/step
    # label wrapping the mod's actual "gamedata" folder) must merge
    # "00 - Core"'s contents straight into the mod root, landing a
    # single "gamedata" there - not a "00 - Core/gamedata" wrapper, and
    # not a duplicated "gamedata/gamedata" if the destination already
    # happens to be named the same as the source. A <file> entry, by
    # contrast, needs an actual filename at its destination, so its own
    # basename is kept.
    target = (
        destination.joinpath(*components)
        if item.is_folder
        else destination.joinpath(*components, source.name)
    )
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
        raise ModInstallError("FOMOD destination escapes the mod folder") from exc
    if target.exists() and target.is_symlink():
        raise ModInstallError("FOMOD destination is a symlink")
    if item.is_folder:
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)
