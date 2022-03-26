import os
import re
import shutil
import functools
import traceback
from urllib.parse import urlparse
from contextlib import contextmanager

import click
from flask import url_for

from .__meta__ import __version__
from . import (
    app,
    config,
    load_entries,
    root_folder,
    static_folder,
    theme_static_folder,
    pages_folder,
    raw_folder,
)


echo = click.echo
echo_green = functools.partial(click.secho, fg="green")
echo_red = functools.partial(click.secho, fg="red")
echo_yellow = functools.partial(click.secho, fg="yellow")


@contextmanager
def step(op_name: str):
    echo(f"{op_name}...", nl=False)
    yield
    echo_green("OK")


@click.group(name="purepress", short_help="A simple static blog generator.")
@click.version_option(version=__version__)
def cli():
    pass


@cli.command("preview", short_help="Preview the site.")
@click.option("--host", "-h", default="127.0.0.1", help="Host to preview the site.")
@click.option("--port", "-p", default=8080, help="Port to preview the site.")
@click.option("--no-debug", is_flag=True, default=False, help="Do not preview in debug mode.")
def preview_command(host, port, no_debug):
    app.config["ENV"] = "development"
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.run(host=host, port=port, debug=not no_debug, use_reloader=False)


@cli.command("build", short_help="Build the site.")
@click.option(
    "--url-root",
    prompt="Please enter the url root (used as prefix of generated url)",
    help='The url root of your site, e.g. "http://example.com/blog/".',
)
def build_command(url_root):
    res = urlparse(url_root)
    app.config["PREFERRED_URL_SCHEME"] = res.scheme or "http"
    app.config["SERVER_NAME"] = res.netloc or "localhost"
    if not res.netloc:
        echo_yellow('The url root does not contain a valid server name, "localhost" will be used.')
    app.config["APPLICATION_ROOT"] = res.path or "/"
    # mark as 'BUILDING' status, so that templates can react properly,
    app.config["BUILDING"] = True

    try:
        with app.test_client() as client:
            base_url = app.config["APPLICATION_ROOT"].rstrip("/")
            get = lambda url: client.get(url[len(base_url) :])
            build(get)
        echo_green('OK! Now you can find the built site in the "build" folder.')
    except Exception:
        traceback.print_exc()
        echo_red("Failed to build the site.")
        exit(1)


def build(get):
    # prepare folder paths
    build_folder = os.path.join(root_folder, "build")
    build_static_folder = os.path.join(build_folder, "static")
    build_static_theme_folder = os.path.join(build_static_folder, "theme")
    build_pages_folder = build_folder

    with step("Creating build folder"):
        if os.path.isdir(build_folder):
            shutil.rmtree(build_folder)
        elif os.path.exists(build_folder):
            os.remove(build_folder)
        os.mkdir(build_folder)

    with step("Copying raw files"):
        copy_folder_content(raw_folder, build_folder)

    with step("Copying theme static files"):
        os.makedirs(build_static_theme_folder, exist_ok=True)
        copy_folder_content(theme_static_folder, build_static_theme_folder)

    with step("Copying static files"):
        copy_folder_content(static_folder, build_static_folder)

    with step("Building custom pages"):
        for dirname, _, files in os.walk(pages_folder):
            if os.path.basename(dirname).startswith("."):
                continue
            rel_dirname = os.path.relpath(dirname, pages_folder)
            os.makedirs(os.path.join(build_pages_folder, rel_dirname), exist_ok=True)
            for file in filter(lambda f: not f.startswith("."), files):
                rel_path = os.path.join(rel_dirname, file)
                dst_rel_path = re.sub(r".md$", ".html", rel_path)
                dst_path = os.path.join(build_pages_folder, dst_rel_path)
                rel_url = "/".join(os.path.split(dst_rel_path))
                with app.test_request_context():
                    url = url_for("page", rel_url=rel_url)
                res = get(url)
                with open(dst_path, "wb") as f:
                    f.write(res.data)

    for mapping in config.get("mappings", []):
        title = mapping["title"]
        path_ = mapping["path"]
        assert path_.startswith("/")
        folder = os.path.join(root_folder, path_.lstrip("/"))
        index_url = mapping.get("index_url", path_).rstrip("/") + "/"
        assert index_url.startswith("/")
        detail_url = mapping.get("detail_url", path_).rstrip("/") + "/"
        assert detail_url.startswith("/")
        index_endpoint = index_url
        detail_endpoint = f"{detail_url}detail"

        with step(f'Building custom mapping "{title}"'):
            build_index_folder = os.path.join(build_pages_folder, index_url.lstrip("/"))
            build_detail_folder = os.path.join(build_pages_folder, detail_url.lstrip("/"))
            os.makedirs(build_index_folder, exist_ok=True)
            os.makedirs(build_detail_folder, exist_ok=True)

            with app.test_request_context():
                entries = load_entries(folder, meta_only=True)

            with app.test_request_context():
                url = url_for(index_endpoint)
            res = get(url)
            with open(os.path.join(build_index_folder, "index.html"), "wb") as f:
                f.write(res.data)

            for entry in entries:
                filename = os.path.splitext(os.path.basename(entry["file"]))[0]
                dst_dirname = os.path.join(build_detail_folder, filename)
                os.makedirs(dst_dirname, exist_ok=True)
                dst_path = os.path.join(dst_dirname, "index.html")
                with app.test_request_context():
                    url = url_for(detail_endpoint, name=filename)
                res = get(url)
                with open(dst_path, "wb") as f:
                    f.write(res.data)

    with step("Building 404"):
        with app.test_request_context():
            url = url_for("page_not_found")
        res = get(url)
        with open(os.path.join(build_folder, "404.html"), "wb") as f:
            f.write(res.data)


def copy_folder_content(src, dst):
    """
    Copy all content in src directory to dst directory.
    The src and dst must exist.
    """
    for file in os.listdir(src):
        file_path = os.path.join(src, file)
        dst_file_path = os.path.join(dst, file)
        if os.path.isdir(file_path):
            shutil.copytree(file_path, dst_file_path)
        else:
            shutil.copy(file_path, dst_file_path)
