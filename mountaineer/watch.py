import importlib.metadata
import os
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Flag, auto
from json import loads as json_loads
from pathlib import Path
from re import search as re_search
from threading import Timer
from typing import Callable

from click import secho
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class CallbackType(Flag):
    CREATED = auto()
    MODIFIED = auto()
    DELETED = auto()


@dataclass
class CallbackDefinition:
    action: CallbackType
    callback: Callable


class ChangeEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        callbacks: list[CallbackDefinition],
        ignore_list=["__pycache__", "_ssr", "_static", "_server"],
        ignore_hidden=True,
        debounce_interval=0.1,
    ):
        """
        :param debounce_interval: Seconds to wait for more events. Will only send one event per batched
        interval to avoid saturating clients with one action that results in many files.

        """
        super().__init__()
        self.callbacks = callbacks
        self.ignore_changes = False
        self.ignore_list = ignore_list
        self.ignore_hidden = ignore_hidden
        self.debounce_interval = debounce_interval
        self.debounce_timer: Timer | None = None

    def on_modified(self, event):
        super().on_modified(event)
        if self.should_ignore_path(event.src_path):
            return
        if not event.is_directory:
            secho(f"File modified: {event.src_path}", fg="yellow")
            self._debounce(CallbackType.MODIFIED)

    def on_created(self, event):
        super().on_created(event)
        if self.should_ignore_path(event.src_path):
            return
        if not event.is_directory:
            secho(f"File created: {event.src_path}", fg="yellow")
            self._debounce(CallbackType.CREATED)

    def on_deleted(self, event):
        super().on_deleted(event)
        if self.should_ignore_path(event.src_path):
            return
        if not event.is_directory:
            secho(f"File deleted: {event.src_path}", fg="yellow")
            self._debounce(CallbackType.DELETED)

    def _debounce(self, action: CallbackType):
        if self.debounce_timer is not None:
            self.debounce_timer.cancel()
        self.debounce_timer = Timer(
            self.debounce_interval, self.handle_callbacks, [action]
        )
        self.debounce_timer.start()

    def handle_callbacks(self, action: CallbackType):
        """
        Runs all callbacks for the given action. Since callbacks are allowed to make
        modifications to the filesystem, we temporarily disable the event handler to avoid
        infinite loops.

        """
        self.ignore_changes = True
        for callback in self.callbacks:
            if action in callback.action:
                callback.callback()
        self.ignore_changes = False

    def should_ignore_path(self, path: Path):
        if self.ignore_changes:
            return True

        path_components = set(str(path).split("/"))

        # Check for any nested hidden directories and ignored directories
        if self.ignore_hidden and any(
            (component for component in path_components if component.startswith("."))
        ):
            return True
        elif path_components & set(self.ignore_list) != set():
            return True
        return False


class WatchdogLockError(Exception):
    def __init__(self, lock_path: Path):
        super().__init__(
            f"Watch lock file exists, another process may be running. If you're sure this is not the case, run:\n"
            f"`$ rm {lock_path}`",
        )
        self.lock_path = lock_path


class PackageWatchdog:
    def __init__(
        self,
        main_package: str,
        dependent_packages: list[str],
        callbacks: list[CallbackDefinition] | None = None,
        run_on_bootup: bool = False,
    ):
        """
        :param run_on_bootup: Typically, we will only notify callback if there has been
            a change to the filesystem. If this is set to True, we will run all callbacks
            on bootup as well.
        """
        self.main_package = main_package
        self.packages = [main_package] + dependent_packages
        self.paths: list[str] = []
        self.callbacks: list[CallbackDefinition] = callbacks or []
        self.run_on_bootup = run_on_bootup

        self.check_packages_installed()
        self.get_package_paths()

    def start_watching(self):
        with self.acquire_watchdog_lock():
            if self.run_on_bootup:
                for callback_definition in self.callbacks:
                    callback_definition.callback()

            event_handler = ChangeEventHandler(callbacks=self.callbacks)
            observer = Observer()

            for path in self.paths:
                secho(f"Watching {path}", fg="green")
                if os.path.isdir(path):
                    observer.schedule(event_handler, path, recursive=True)
                else:
                    observer.schedule(
                        event_handler, os.path.dirname(path), recursive=False
                    )

            observer.start()

            try:
                secho("Starting observer...")
                observer.join()
            except KeyboardInterrupt:
                observer.stop()
                observer.join()

    def check_packages_installed(self):
        for package in self.packages:
            try:
                importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                raise ValueError(
                    f"Package '{package}' is not installed in the current environment"
                )

    def get_package_paths(self):
        paths: list[str] = []
        for package in self.packages:
            dist = importlib.metadata.distribution(package)
            package_path = self.resolve_package_path(dist)
            paths.append(str(package_path))
        self.paths = self.merge_paths(paths)

    def resolve_package_path(self, dist: importlib.metadata.Distribution):
        """
        Given a package distribution, returns the local file directory that should be watched
        """
        # Recent versions of poetry install development packages (-e .) as direct URLs
        # https://the-hitchhikers-guide-to-packaging.readthedocs.io/en/latest/introduction.html
        # "Path configuration files have an extension of .pth, and each line must
        # contain a single path that will be appended to sys.path."
        package_name = dist.name.replace("-", "_").lower()
        symbolic_links = [
            path
            for path in (dist.files or [])
            if path.name.lower() == f"{package_name}.pth"
        ]
        dist_links = [
            path
            for path in (dist.files or [])
            if path.name == "direct_url.json"
            and re_search(
                package_name + r"-[0-9-.]+\.dist-info", path.parent.name.lower()
            )
        ]
        explicit_links = [
            path
            for path in (dist.files or [])
            if path.parent.name.lower() == package_name
            and (
                # Sanity check that the parent is the high level project directory
                # by looking for common base files
                path.name == "__init__.py"
            )
        ]

        if symbolic_links:
            direct_url_path = symbolic_links[0]
            return dist.locate_file(direct_url_path.read_text().strip())

        if dist_links:
            dist_link = dist_links[0]
            direct_metadata = json_loads(dist_link.read_text())
            package_path = "/" + direct_metadata["url"].lstrip("file://").lstrip("/")
            return dist.locate_file(package_path)

        if explicit_links:
            # Since we found the __init__.py file for the root, we should be able to go up
            # to the main path
            explicit_link = explicit_links[0]
            return dist.locate_file(explicit_link.parent)

        raise ValueError(
            f"Could not find a valid path for package {dist.name}, found files: {dist.files}"
        )

    @contextmanager
    def acquire_watchdog_lock(self):
        """
        We only want one watchdog running at a time or otherwise we risk stepping
        on each other or having infinitely looping file change notifications.

        """
        dist = importlib.metadata.distribution(self.main_package)
        package_path = self.resolve_package_path(dist)

        lock_path = (Path(package_path) / ".watchdog.lock").absolute()
        if lock_path.exists():
            raise WatchdogLockError(lock_path=lock_path)

        try:
            # Create the lock - this caller should now have exclusive access to the watchdog
            lock_path.touch()
            yield
        finally:
            # Remove the lock
            lock_path.unlink()

    def merge_paths(self, raw_paths: list[str]):
        """
        If one path is a subdirectory of another, we only want to watch the parent
        directory. This function merges the paths to avoid duplicate watchers.

        """
        paths = [Path(path).resolve() for path in raw_paths]

        # Parents should come before their subdirectories
        paths.sort(key=lambda path: len(path.parts))

        merged: list[Path] = []

        for path in paths:
            # Check if the current path is a subdirectory of any path in the merged list
            if not any(path.is_relative_to(parent) for parent in merged):
                merged.append(path)

        # Convert Path objects back to strings
        return [str(path) for path in merged]
