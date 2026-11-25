from operator import itemgetter
from stashapi.stashapp import StashInterface
from stashapi import log
import os
import pathlib
import re


MOVE_FILE_MUTATION = """
mutation MoveFiles($input: MoveFilesInput!) {
    moveFiles(input: $input)
}
"""


def paths_equal(left: pathlib.Path | str, right: pathlib.Path | str) -> bool:
    """Case-insensitive path equality for Windows/macOS default filesystems."""
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def resolve_existing_path_casing(path: pathlib.Path) -> pathlib.Path:
    """Rewrite path components to match existing filesystem casing.

    Stash folder rows use exact basename/path strings. On case-insensitive
    filesystems, a template like ``I:/Video/studios/...`` can create an orphan
    ``studios`` folder beside ``Studios``, then UNIQUE-fail on
    ``(parent_folder_id, basename)`` when a studio child already exists under
    that orphan parent. Reusing on-disk casing lets moveFiles find the real
    library folders instead of inserting duplicates.
    """
    path = pathlib.Path(path)
    try:
        path = path.absolute()
    except OSError:
        return path

    parts = path.parts
    if len(parts) <= 1:
        return path

    current = pathlib.Path(parts[0])
    for idx in range(1, len(parts)):
        part = parts[idx]
        if not current.exists():
            return current.joinpath(*parts[idx:])

        matched_name = None
        try:
            for entry in current.iterdir():
                if entry.name.casefold() == part.casefold():
                    matched_name = entry.name
                    break
        except OSError as error:
            log.warning(
                f"Could not list {current} while resolving path casing: {error}"
            )
            return current.joinpath(*parts[idx:])

        if matched_name is None:
            return current.joinpath(*parts[idx:])

        current = current / matched_name

    return current


def get_parent_studio_chain(stash, scene):
    current_studio = scene.get("studio", {})
    parent_chain = [current_studio.get("name", "")]

    while current_studio.get("parent_studio"):
        current_studio = stash.find_studio(current_studio.get("parent_studio"))

        parent_chain.append(current_studio.get("name"))

    return "/".join(reversed(parent_chain))

def key_getter(key):
    return lambda _, data: data.get(key, "")

FILE_VARIABLES = {
    "audio_codec": key_getter("audio_codec"),
    "ext": lambda _, file: file.get("basename", "").split(".")[-1],
    "format": key_getter("format"),
    "height": key_getter("height"),
    "index": key_getter("index"),
    "video_codec": key_getter("video_codec"),
    "width": key_getter("width"),
}

SCENE_VARIABLES = {
    "scene_id": key_getter("id"),
    "title": key_getter("title"),
    "date": key_getter("date"),
    "director": key_getter("director"),
    "month": lambda _, scene: scene.get("date", "").split("-")[1] if scene.get("date") else "",
    "parent_studio_chain": get_parent_studio_chain,
    "studio_code": key_getter("code"),
    "studio_name": lambda _, scene: scene.get("studio", {}).get("name", ""),
    "year": lambda _, scene: scene.get("date", "").split("-")[0] if scene.get("date") else "",
}

def find_variables(format_template) -> list[str]:
    variables = []

    for variable in FILE_VARIABLES.keys():
        if f"${variable}$" in format_template:
            variables.append(variable)

    for variable in SCENE_VARIABLES.keys():
        if f"${variable}$" in format_template:
            variables.append(variable)

    return variables


def clean_optional_from_format(formatted_string: str) -> str:
    # Erase entire optional section if there is an unused variable
    formatted_string = re.sub(r"\{.*\$\w+\$.*\}", "", formatted_string)

    # Remove any remaining curly braces
    formatted_string = formatted_string.replace(r"{", "").replace(r"}", "")

    return formatted_string


def apply_format(format_template: str, stash: StashInterface, scene_data, file_data)-> str:
    variables = find_variables(format_template)

    formatted_template = format_template

    for variable in variables:
        if variable in FILE_VARIABLES:
            value = FILE_VARIABLES[variable](stash, file_data)
        elif variable in SCENE_VARIABLES:
            value = SCENE_VARIABLES[variable](stash, scene_data)

        if not value:
            continue

        formatted_template = formatted_template.replace(f"${variable}$", str(value))

    formatted_template = clean_optional_from_format(formatted_template)

    return formatted_template


class StashFile:
    def __init__(self, stash: StashInterface, config, scene_data, file_data):
        self.stash = stash
        self.config = config
        self.scene_data = scene_data
        self.file_data = file_data
        self.duplicate_index = 0

    def get_old_file_path(self) -> pathlib.Path:
        path = pathlib.Path(self.file_data["path"])

        return path.absolute()

    def get_new_file_folder(self) -> pathlib.Path:
        if self.config.default_directory_path_format:
            directory_path = apply_format(self.config.default_directory_path_format, self.stash, self.scene_data, self.file_data)
            directory_path = pathlib.Path(directory_path).absolute()
        else:
            path = pathlib.Path(self.file_data["path"])
            directory_path = path.parent.absolute()

        resolved = resolve_existing_path_casing(directory_path)
        if str(resolved) != str(directory_path):
            log.info(
                f"Adjusted destination folder casing from {directory_path} to {resolved}"
            )
        return resolved
    
    def get_new_file_name(self) -> str:
        if not self.config.default_file_name_format:
            return self.file_data["basename"]

        file_data = {**self.file_data, "index": self.duplicate_index}
        file_name = apply_format(self.config.default_file_name_format, self.stash, self.scene_data, file_data)

        if self.duplicate_index:
            duplicate_suffix = apply_format(self.config.duplicate_file_suffix, self.stash, self.scene_data, file_data)
            base_name = file_name.rsplit(".", 1)[0]
            extension = file_name.rsplit(".", 1)[1]

            file_name = f"{base_name}{duplicate_suffix}.{extension}"

        if not self.config.allow_unsafe_characters:
            file_name = re.sub(r"[<>:\"/\\|?*]", "", file_name)

        if self.config.remove_extra_spaces_from_file_name:
            file_name = re.sub(r"\s+", " ", file_name)

        return file_name

    def get_new_file_path(self) -> pathlib.Path:
        return self.get_new_file_folder() / self.get_new_file_name()

    def rename_related_files(self, old_path: pathlib.Path, new_path: pathlib.Path, dry_run: bool):
        if not self.config.rename_related_files:
            return

        old_directory = old_path.parent
        new_directory = new_path.parent
        # Match sidecars that share the video stem, including multi-part
        # subtitle tags: Video.en.srt, Video.en.forced.srt, Video.ass, etc.
        # Avoid pathlib.glob so brackets/special chars in titles are safe.
        stem_prefix = f"{old_path.stem}."
        related_files = []
        try:
            candidates = old_directory.iterdir()
        except OSError as error:
            log.error(f"Failed to list related files in {old_directory}: {error}")
            return

        for path in candidates:
            if not path.is_file() or path == old_path:
                continue
            if path.name.startswith(stem_prefix) or path.name.lower().startswith(stem_prefix.lower()):
                related_files.append(path)

        if not related_files:
            return

        for related_file in related_files:
            # Preserve everything after the video stem (lang/forced + extension)
            remainder = related_file.name[len(old_path.stem):]
            target_path = new_directory / f"{new_path.stem}{remainder}"

            if str(related_file).lower() == str(target_path).lower():
                continue

            if target_path.exists():
                log.warning(f"Related file already exists at {target_path}, skipping rename for {related_file}")
                continue

            log.info(f"Renaming related file from {related_file} to {target_path}")

            if dry_run:
                continue

            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                log.error(f"Failed to create directory for related file {target_path}: {error}")
                continue

            try:
                related_file.rename(target_path)
            except OSError as error:
                log.error(f"Failed to rename related file from {related_file} to {target_path}: {error}")

    def rename_file(self):
        old_path = self.get_old_file_path()
        new_folder = self.get_new_file_folder()
        new_name = self.get_new_file_name()
        new_path = new_folder / new_name

        if not old_path.exists():
            log.warning(f"File for scene does not exist on disk: {old_path}")
            return

        if paths_equal(old_path, new_path):
            log.info("File paths are the same, no renaming needed.")
            return

        log.debug(f"Checking if a file exists at {new_path}")
        while new_path.exists() and not paths_equal(old_path, new_path):
            self.duplicate_index += 1
            log.warning(f"File already exists at {new_path}, adding duplicate suffix: {self.duplicate_index}")
            new_name = self.get_new_file_name()
            new_path = new_folder / new_name

            if paths_equal(old_path, new_path):
                log.info("File paths are the same after adding duplicate suffix, no renaming needed.")
                return

        log.info(f"Renaming file from {old_path} to {new_path}")
        if self.config.dry_run:
            log.info("Dry run enabled, not actually renaming the file.")
            self.rename_related_files(old_path, new_path, dry_run=True)
            return

        moved_file = self.stash.call_GQL(
            MOVE_FILE_MUTATION,
            {"input": {
                    "ids": [self.file_data["id"]],
                    "destination_folder": str(new_folder),
                    "destination_basename": new_name,
                }
            }
        )

        log.info(f"File renamed successfully: {moved_file}")
        self.rename_related_files(old_path, new_path, dry_run=False)
