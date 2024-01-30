from importlib import import_module
from typing import Callable

from filzl.client_interface.builder import ClientBuilder
from filzl.watch import CallbackDefinition, CallbackType, PackageWatchdog
from filzl.webservice import UvicornProcess
from multiprocessing import Process


def handle_watch(
    *,
    package: str,
    webcontroller: str,
):
    """
    Watch the file directory and rebuild auto-generated files.

    :param client_package: "my_website"
    :param client_controller: "my_website.app:controller"

    """

    def update_build():
        # Import just within the scope of the build, so we can pick up changes that
        # are made over time
        client_builder = ClientBuilder(import_from_string(webcontroller))
        client_builder.build()

    watchdog = build_common_watchdog(package, update_build)
    watchdog.start_watching()


def restart_build_server(webcontroller: str):
    client_builder = ClientBuilder(import_from_string(webcontroller))
    client_builder.build()


def handle_runserver(
    *,
    package: str,
    webservice: str,
    webcontroller: str,
    port: int,
):
    """
    :param client_package: "my_website"
    :param client_webservice: "my_website.app:app"
    :param client_controller: "my_website.app:controller"

    """
    current_uvicorn_thread: UvicornProcess | None = None
    build_process: Process | None = None

    def restart_uvicorn():
        nonlocal current_uvicorn_thread
        if current_uvicorn_thread:
            current_uvicorn_thread.stop()
            current_uvicorn_thread.join()

        current_uvicorn_thread = UvicornProcess(
            webservice,
            port=port,
        )
        current_uvicorn_thread.start()

    def update_build():
        # Stop the current build process if it's still running
        nonlocal build_process
        if build_process:
            if build_process.is_alive():
                build_process.terminate()
                build_process.join()
        build_process = Process(target=restart_build_server, args=(webcontroller,))
        build_process.start()
        restart_uvicorn()

    # Initial launch - both build and run the server, since we may not have
    # any built client-side files yet
    update_build()

    watchdog = build_common_watchdog(package, update_build)
    watchdog.start_watching()


def import_from_string(import_string: str):
    """
    Given a string to the package (like "my_website.app:controller") import the
    actual variable
    """
    module_name, attribute_name = import_string.split(":")
    module = import_module(module_name)
    return getattr(module, attribute_name)


def build_common_watchdog(client_package: str, callback: Callable):
    """
    Useful creation class to build a watchdog the common client class
    and our internal package.

    """
    return PackageWatchdog(
        client_package,
        dependent_packages=["filzl"],
        callbacks=[
            CallbackDefinition(
                CallbackType.CREATED | CallbackType.MODIFIED,
                callback,
            )
        ],
    )
