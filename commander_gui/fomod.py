"""XML-based FOMOD parser and installer logic shared by both interfaces.

Covers the parts of the FOMOD format that real Anomaly/GAMMA installers use:
install steps with option groups, per-option condition flags, option types
that change with earlier choices (Required / Recommended / NotUsable ...),
steps shown only for some choices, conditional file installs, and file
priorities. :class:`FomodWizard` holds a run's selections and rules without
any Qt, so the desktop dialog and Deck Mode's screen drive the same logic.
"""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import safe_xml
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
    #: Install order: higher priority is copied later, so it wins a clash.
    priority: int = 0


@dataclass(frozen=True)
class FomodCondition:
    """A ``<dependencies>`` tree: flag and file checks joined And/Or."""

    operator: str = "And"
    #: ``(flag, value)`` - true when the flag currently holds that value.
    flags: tuple[tuple[str, str], ...] = ()
    #: ``(file, state)`` - plugin-file checks from Bethesda games. Anomaly
    #: has no plugin list, so only a "Missing" check can hold.
    files: tuple[tuple[str, str], ...] = ()
    children: tuple[FomodCondition, ...] = ()

    def holds(self, flags: dict[str, str]) -> bool:
        results = [flags.get(flag, "") == value for flag, value in self.flags]
        results += [state.lower() == "missing" for _file, state in self.files]
        results += [child.holds(flags) for child in self.children]
        if not results:
            return True
        return any(results) if self.operator.lower() == "or" else all(results)


#: Option types, as named by the FOMOD schema.
REQUIRED = "Required"
RECOMMENDED = "Recommended"
OPTIONAL = "Optional"
NOT_USABLE = "NotUsable"
COULD_BE_USABLE = "CouldBeUsable"


@dataclass(frozen=True)
class FomodOption:
    name: str
    description: str = ""
    files: tuple[FomodFile, ...] = ()
    #: Archive-relative preview image path, as written (may use "\\").
    image: str = ""
    #: ``(flag, value)`` pairs set while this option is selected.
    flags: tuple[tuple[str, str], ...] = ()
    default_type: str = OPTIONAL
    #: First matching ``(condition, type)`` overrides ``default_type``.
    type_patterns: tuple[tuple[FomodCondition, str], ...] = ()

    def option_type(self, flags: dict[str, str]) -> str:
        for condition, kind in self.type_patterns:
            if condition.holds(flags):
                return kind
        return self.default_type


@dataclass(frozen=True)
class FomodGroup:
    name: str
    group_type: str
    options: tuple[FomodOption, ...]


@dataclass(frozen=True)
class FomodStep:
    name: str
    groups: tuple[FomodGroup, ...]
    #: Shown only while this holds; None means always.
    visible: FomodCondition | None = None


@dataclass(frozen=True)
class FomodConfig:
    name: str
    author: str
    steps: tuple[FomodStep, ...]
    #: Files/folders installed unconditionally, independent of any step -
    #: <requiredInstallFiles>. Many small, no-choice FOMODs ship only
    #: this, with no <installSteps> at all.
    required_files: tuple[FomodFile, ...] = ()
    #: ``<conditionalFileInstalls>``: files installed when the final flags
    #: match, whatever was picked to set them.
    conditional_files: tuple[tuple[FomodCondition, tuple[FomodFile, ...]], ...] = ()
    #: Archive-relative ``<moduleImage>`` path.
    image: str = ""


def _text(element: ET.Element | None, default: str = "") -> str:
    return (element.text or default).strip() if element is not None else default


def _parse_files_node(files_node: ET.Element | None) -> tuple[FomodFile, ...]:
    """Parse a <files> or <requiredInstallFiles>-shaped node's children."""
    files: list[FomodFile] = []
    if files_node is None:
        return ()
    for node in files_node:
        if node.tag not in ("file", "folder"):
            continue
        dest_attr = node.attrib.get("destination")
        files.append(
            FomodFile(
                source=node.attrib.get("source", "").strip(),
                destination=None if dest_attr is None else dest_attr.strip(),
                is_folder=node.tag == "folder",
                priority=_int(node.attrib.get("priority")),
            )
        )
    return tuple(files)


def _int(value: str | None) -> int:
    try:
        return int((value or "0").strip())
    except ValueError:
        return 0


def _parse_condition(node: ET.Element | None) -> FomodCondition | None:
    """A ``<dependencies>``-shaped node (also ``<visible>``), or None."""
    if node is None:
        return None
    flags: list[tuple[str, str]] = []
    files: list[tuple[str, str]] = []
    children: list[FomodCondition] = []
    for child in node:
        if child.tag == "flagDependency":
            flags.append(
                (child.attrib.get("flag", "").strip(), child.attrib.get("value", "").strip())
            )
        elif child.tag == "fileDependency":
            files.append(
                (child.attrib.get("file", "").strip(), child.attrib.get("state", "").strip())
            )
        elif child.tag == "dependencies":
            nested = _parse_condition(child)
            if nested is not None:
                children.append(nested)
        # gameDependency / fommDependency: version checks for other games'
        # tools - nothing to compare against here, so they always hold.
    return FomodCondition(
        node.attrib.get("operator", "And"), tuple(flags), tuple(files), tuple(children)
    )


def _type_name(node: ET.Element | None, default: str = OPTIONAL) -> str:
    return (node.attrib.get("name", "").strip() or default) if node is not None else default


def _parse_type(option: ET.Element) -> tuple[str, tuple[tuple[FomodCondition, str], ...]]:
    descriptor = option.find("typeDescriptor")
    if descriptor is None:
        return OPTIONAL, ()
    simple = descriptor.find("type")
    if simple is not None:
        return _type_name(simple), ()
    dependency = descriptor.find("dependencyType")
    if dependency is None:
        return OPTIONAL, ()
    patterns: list[tuple[FomodCondition, str]] = []
    for pattern in dependency.findall("patterns/pattern"):
        condition = _parse_condition(pattern.find("dependencies"))
        if condition is not None:
            patterns.append((condition, _type_name(pattern.find("type"))))
    return _type_name(dependency.find("defaultType")), tuple(patterns)


def _named(element: ET.Element, child: str, default: str) -> str:
    """Read a standard name attribute, with compatibility for child elements."""
    return element.attrib.get("name", "").strip() or _text(element.find(child), default)


def parse_config(path: Path) -> FomodConfig:
    """Parse the common ModuleConfig.xml subset used by most FOMODs."""
    try:
        root = safe_xml.parse_file(path)
    except safe_xml.XML_ERRORS as exc:
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
                image = option.find("image")
                default_type, type_patterns = _parse_type(option)
                options.append(
                    FomodOption(
                        name=_named(option, "name", "Unnamed option"),
                        description=_text(option.find("description")),
                        files=files,
                        image=image.attrib.get("path", "").strip() if image is not None else "",
                        flags=tuple(
                            (flag.attrib.get("name", "").strip(), _text(flag))
                            for flag in option.findall("conditionFlags/flag")
                        ),
                        default_type=default_type,
                        type_patterns=type_patterns,
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
                _parse_condition(step.find("visible")),
            )
        )
    conditional: list[tuple[FomodCondition, tuple[FomodFile, ...]]] = []
    for pattern in root.findall("conditionalFileInstalls/patterns/pattern"):
        condition = _parse_condition(pattern.find("dependencies"))
        if condition is not None:
            conditional.append((condition, _parse_files_node(pattern.find("files"))))
    if not steps and not required_files and not conditional:
        raise ModInstallError("This FOMOD has no supported installation steps")
    module_image = root.find("moduleImage")
    return FomodConfig(
        _text(module, "FOMOD installation"),
        _text(author),
        tuple(steps),
        required_files,
        tuple(conditional),
        module_image.attrib.get("path", "").strip() if module_image is not None else "",
    )


Selections = dict[tuple[int, int], list[int]]


def flags_before(config: FomodConfig, selected: Selections, step_limit: int) -> dict[str, str]:
    """Condition flags set by the choices on visible steps before ``step_limit``.

    Steps are walked in order and a later option's flag overrides an
    earlier one's, as in MO2. A step hidden by the flags so far adds none.
    """
    flags: dict[str, str] = {}
    for step_index, step in enumerate(config.steps[:step_limit]):
        if step.visible is not None and not step.visible.holds(flags):
            continue
        for group_index, group in enumerate(step.groups):
            for option_index in selected.get((step_index, group_index), []):
                if 0 <= option_index < len(group.options):
                    flags.update(group.options[option_index].flags)
    return flags


def step_visible(config: FomodConfig, selected: Selections, step_index: int) -> bool:
    step = config.steps[step_index]
    return step.visible is None or step.visible.holds(
        flags_before(config, selected, step_index)
    )


def resolve_image(root: Path, relative: str) -> Path | None:
    """An option's preview image inside the extracted archive, or None."""
    if not relative:
        return None
    path = _find_case_insensitive(root, relative.replace("\\", "/").strip("/"))
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


#: What a group asks of the user, per FOMOD group type.
GROUP_HINTS = {
    "SelectExactlyOne": "Choose one",
    "SelectAtMostOne": "Choose one, or none",
    "SelectAtLeastOne": "Choose at least one",
    "SelectAll": "All of these are installed",
    "SelectAny": "Choose any",
}


class FomodWizard:
    """One run of a FOMOD installer: the selections and the rules on them.

    UI-free. A front end shows :meth:`visible_steps` one at a time, calls
    :meth:`enter` when it shows a step, :meth:`toggle` when an option is
    picked, and :meth:`errors` before moving on; :attr:`selected` then goes
    to :func:`apply_options`.
    """

    def __init__(self, config: FomodConfig) -> None:
        self.config = config
        self.selected: Selections = {}

    # -- state --------------------------------------------------------------
    def flags(self, step_index: int) -> dict[str, str]:
        return flags_before(self.config, self.selected, step_index)

    def visible_steps(self) -> list[int]:
        return [
            index
            for index in range(len(self.config.steps))
            if step_visible(self.config, self.selected, index)
        ]

    def option_type(self, step_index: int, group_index: int, option_index: int) -> str:
        option = self.config.steps[step_index].groups[group_index].options[option_index]
        return option.option_type(self.flags(step_index))

    def is_selected(self, step_index: int, group_index: int, option_index: int) -> bool:
        return option_index in self.selected.get((step_index, group_index), [])

    # -- changes ------------------------------------------------------------
    def enter(self, step_index: int) -> None:
        """Default a step's groups the first time, and re-apply the rules
        after an earlier choice changed (an option may have turned
        Required or NotUsable since)."""
        step = self.config.steps[step_index]
        for group_index, group in enumerate(step.groups):
            key = (step_index, group_index)
            types = [
                self.option_type(step_index, group_index, index)
                for index in range(len(group.options))
            ]
            if key not in self.selected:
                chosen = [
                    index for index, kind in enumerate(types)
                    if kind in (REQUIRED, RECOMMENDED)
                ]
                if group.group_type == "SelectAll":
                    chosen = list(range(len(group.options)))
            else:
                chosen = [i for i in self.selected[key] if types[i] != NOT_USABLE]
                chosen += [
                    i for i, kind in enumerate(types) if kind == REQUIRED and i not in chosen
                ]
            usable = [i for i, kind in enumerate(types) if kind != NOT_USABLE]
            if group.group_type in ("SelectExactlyOne", "SelectAtMostOne"):
                chosen = chosen[:1]
            if (
                group.group_type in ("SelectExactlyOne", "SelectAtLeastOne")
                and not chosen
                and usable
            ):
                chosen = [usable[0]]
            self.selected[key] = sorted(chosen)

    def toggle(self, step_index: int, group_index: int, option_index: int) -> None:
        group = self.config.steps[step_index].groups[group_index]
        kind = self.option_type(step_index, group_index, option_index)
        key = (step_index, group_index)
        chosen = list(self.selected.get(key, []))
        on = option_index in chosen
        if kind == NOT_USABLE and not on:
            return
        if group.group_type == "SelectAll" or (kind == REQUIRED and on):
            return
        flags_were = self.flags(len(self.config.steps))
        if group.group_type == "SelectExactlyOne":
            chosen = [option_index]
        elif group.group_type == "SelectAtMostOne":
            chosen = [] if on else [option_index]
        elif on:
            chosen.remove(option_index)
        else:
            chosen.append(option_index)
        self.selected[key] = sorted(chosen)
        # Later steps' options, types and visibility depend only on the
        # flags. When this choice changed them, those steps start over from
        # their new defaults; when it didn't, the user's choices there stay.
        if self.flags(len(self.config.steps)) != flags_were:
            for later in [k for k in self.selected if k[0] > step_index]:
                del self.selected[later]

    def errors(self, step_index: int) -> list[str]:
        """Groups on this step whose selection breaks their type's rule."""
        problems: list[str] = []
        for group_index, group in enumerate(self.config.steps[step_index].groups):
            count = len(self.selected.get((step_index, group_index), []))
            kind = group.group_type
            if (
                (kind == "SelectExactlyOne" and count != 1)
                or (kind == "SelectAtMostOne" and count > 1)
                or (kind == "SelectAtLeastOne" and count < 1)
            ):
                problems.append(group.name)
        return problems

    def selections(self) -> Selections:
        """What to install: only the steps the final choices leave visible."""
        visible = set(self.visible_steps())
        return {key: list(value) for key, value in self.selected.items() if key[0] in visible}


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
    items: list[FomodFile] = list(config.required_files)
    for step_index, step in enumerate(config.steps):
        visible = step_visible(config, selected, step_index)
        for group_index, group in enumerate(step.groups):
            for option_index in selected.get((step_index, group_index), []):
                if not isinstance(option_index, int) or not 0 <= option_index < len(group.options):
                    raise ModInstallError(
                        f"Invalid FOMOD option selection in step {step_index}, group {group_index}"
                    )
                if visible:
                    items.extend(group.options[option_index].files)
    final_flags = flags_before(config, selected, len(config.steps))
    for condition, files in config.conditional_files:
        if condition.holds(final_flags):
            items.extend(files)
    # Lowest priority first, so a higher-priority file overwrites it; equal
    # priorities keep the installer's own order (sorted() is stable).
    for item in sorted(items, key=lambda entry: entry.priority):
        _place_item(item, root, destination)


def _find_case_insensitive(root: Path, relative: str) -> Path:
    """``root/relative``, matching each component case-insensitively.

    FOMOD configs are written on Windows, where "Gamedata\\Scripts" and the
    archive's "gamedata/scripts" are the same folder. An exact match always
    wins; the path is returned unchanged when nothing matches, so the
    caller's "missing" error still names what the config asked for.
    """
    current = root
    for part in [p for p in relative.split("/") if p]:
        exact = current / part
        if exact.exists() or part in (".", ".."):
            current = exact
            continue
        try:
            match = next(
                (child for child in current.iterdir() if child.name.lower() == part.lower()),
                None,
            )
        except OSError:
            match = None
        current = match if match is not None else exact
    return current


def _place_item(item: FomodFile, root: Path, destination: Path) -> None:
    """Copy one FOMOD file/folder entry into ``destination``.

    ``root``/``destination`` are already resolved absolute paths (see
    ``apply_options``).
    """
    source_name = item.source.replace("\\", "/").strip("/")
    source = _find_case_insensitive(root, source_name).resolve()
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
        # For a <folder>, "its own source path" is the folder itself, so
        # its contents land back under the same relative folder
        # (<folder source="gamedata"/> -> gamedata/...). Using the parent
        # for folders too spilled a folder's contents into the mod root.
        if item.is_folder:
            dest_str = source_name
        else:
            parent = PurePosixPath(source_name).parent
            dest_str = "" if str(parent) == "." else str(parent)
    else:
        dest_str = item.destination
    if dest_str.startswith(("/", "\\")):
        raise ModInstallError("FOMOD destination contains unsafe components")
    # Windows-authored configs often end a folder destination with a
    # separator ("gamedata\\"); that trailing empty component is not unsafe.
    dest_str = dest_str.rstrip("/\\")
    components = dest_str.replace("\\", "/").split("/") if dest_str else []
    if any(
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
    # A <file> destination may name the target file itself
    # (destination="gamedata\\scripts\\foo.script", the common authoring
    # style) or just its folder (destination="gamedata"); only the latter
    # gets the source's own name appended.
    names_file = bool(components) and components[-1].lower() == source.name.lower()
    if item.is_folder:
        target = destination.joinpath(*components)
    elif names_file:
        target = destination.joinpath(*components)
        components = components[:-1]
    else:
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
        raise ModInstallError("FOMOD destination escapes the mod folder") from exc
    if target.exists() and target.is_symlink():
        raise ModInstallError("FOMOD destination is a symlink")
    if item.is_folder:
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)
