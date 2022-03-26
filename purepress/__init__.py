import os
import re
import functools
import sys
import xml.etree.ElementTree as etree
from os import path
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

import yaml
import toml
import markdown.extensions
import markdown.treeprocessors
from markdown import Markdown
from mdx_gfm import GithubFlavoredMarkdownExtension
from flask import (
    Flask,
    render_template,
    abort,
    url_for,
    Blueprint,
    send_from_directory,
)
from werkzeug.security import safe_join

# calculate some folder path
root_folder = os.getenv("PUREPRESS_INSTANCE", os.getcwd())
static_folder = path.join(root_folder, "static")
template_folder = path.join(root_folder, "theme", "templates")
theme_static_folder = path.join(root_folder, "theme", "static")
pages_folder = path.join(root_folder, "pages")
raw_folder = path.join(root_folder, "raw")

# load configurations
try:
    purepress_config = toml.load(path.join(root_folder, "purepress.toml"))
except FileNotFoundError:
    purepress_config = {"site": {}, "config": {}}
site, config = purepress_config["site"], purepress_config["config"]

app = Flask(
    __name__,
    instance_path=root_folder,
    template_folder=template_folder,
    static_folder=static_folder,
    instance_relative_config=True,
)

# handle static files for theme
theme_bp = Blueprint(
    "theme",
    __name__,
    static_url_path="/static/theme",
    static_folder=theme_static_folder,
)
app.register_blueprint(theme_bp)

# prepare markdown parser
class HookImageSrcProcessor(markdown.treeprocessors.Treeprocessor):
    def run(self, root: etree.Element):
        static_url = url_for("static", filename="")
        for el in root.iter("img"):
            src = el.get("src", "")
            if src.startswith("/static/"):
                el.set("src", re.sub(r"^/static/", static_url, src))


class HookLinkHrefProcessor(markdown.treeprocessors.Treeprocessor):
    @staticmethod
    def path_to_url(path: str) -> str:
        root = url_for("index").rstrip("/")
        url = path
        if path.startswith("/posts/"):
            # /posts/2021-08-23-hello-world.md -> /post/2021/08/23/hello-world/
            url = re.sub(r"^/posts/", f"{root}/post/", url)
            url = re.sub(r"-", "/", url, count=3)
            url = re.sub(r"\.md$", "/", url)
        elif path.startswith("/pages/"):
            # /pages/about/ -> /about/
            # /pages/about/index.md -> /about/
            # /pages/foo/bar.md -> /foo/bar.html
            url = re.sub(r"^/pages/", f"{root}/", url)
            url = re.sub(r"index\.md$", "", url)
            url = re.sub(r"\.md$", ".html", url)
        elif path.startswith("/raw/"):
            # /raw/foo/baz.html -> /foo/baz.html
            url = re.sub(r"^/raw/", f"{root}/", url)
        return url

    def run(self, root: etree.Element):
        for el in root.iter("a"):
            href = el.get("href", "")
            if href.startswith("/"):
                el.set("href", self.path_to_url(href))


class Extension(markdown.extensions.Extension):
    def extendMarkdown(self, md) -> None:
        md.treeprocessors.register(HookImageSrcProcessor(), "hook-image-src", 5)
        md.treeprocessors.register(HookLinkHrefProcessor(), "hook-link-href", 5)


_md = Markdown(extensions=[GithubFlavoredMarkdownExtension(), Extension(), "footnotes"])


def markdown_convert(text: str) -> str:
    _md.reset()
    return _md.convert(text)


# inject site and config into template context
@app.context_processor
def inject_objects() -> Dict[str, Any]:
    return {"global": {"site": site, "config": config}}


def load_entry(fullpath: str, *, meta_only: bool) -> Optional[Dict[str, Any]]:
    # read frontmatter and content
    frontmatter, content = "", ""
    try:
        with open(fullpath, mode="r", encoding="utf-8") as f:
            firstline = f.readline().strip()
            remained = f.read().strip()
            if firstline == "---":
                frontmatter, remained = remained.split("---", maxsplit=1)
                content = remained.strip()
            else:
                content = "\n\n".join([firstline, remained]).strip()
    except FileNotFoundError:
        return None
    # construct the entry object
    entry: Dict[str, Any] = yaml.load(frontmatter, Loader=yaml.FullLoader) or {}
    entry["file"] = fullpath
    # figure out the title
    if "title" not in entry:
        if content.startswith("# "):
            title, content = content.split("\n", maxsplit=1)
            entry["title"] = title[2:].strip()
            content = content.strip()
        else:
            entry["title"] = " ".join(path.splitext(path.basename(fullpath))[0].split("-"))
    # ensure datetime fields are real datetime
    for k in ("created", "updated"):
        if isinstance(entry.get(k), date) and not isinstance(entry.get(k), datetime):
            entry[k] = datetime.combine(entry[k], datetime.min.time())
    # if should, convert markdown content to html
    if not meta_only:
        entry["content"] = markdown_convert(content)
    return entry


def load_entries(dirpath: str, *, meta_only: bool) -> List[Dict[str, Any]]:
    try:
        entry_files = os.listdir(dirpath)
    except FileNotFoundError:
        return []

    def gen_entries():
        for entry_file in entry_files:
            if not entry_file.endswith(".md"):
                continue
            entry_fullpath = safe_join(dirpath, entry_file)
            if not entry_fullpath:
                continue
            entry = load_entry(entry_fullpath, meta_only=meta_only)
            if entry is None:
                continue
            yield entry

    entries = list(filter(lambda x: x and not x.get("hide", False), gen_entries()))
    entries.sort(key=lambda x: x.get("order", sys.maxsize))
    return entries


def load_page(rel_url: str) -> Optional[Dict[str, Any]]:
    # convert relative url to full file path
    pathnames = rel_url.split("/")
    fullpath = safe_join(pages_folder, *pathnames)
    if fullpath is None:
        return None
    if fullpath.endswith(path.sep):  # /foo/bar/
        fullpath = path.join(fullpath, "index.md")
    elif fullpath.endswith(".html"):  # /foo/bar.html
        fullpath = path.splitext(fullpath)[0] + ".md"
    else:  # /foo/bar
        fullpath += ".md"
    # load page entry
    page = load_entry(fullpath, meta_only=False)
    if page is None:
        return None
    page["url"] = url_for("page", rel_url=rel_url)
    return page


def templated(template: str) -> Callable:
    if not template.endswith(".html"):
        template += ".html"

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            res = func(*args, **kwargs)
            if isinstance(res, dict):
                return render_template([f"custom/{template}", template], **res)
            return res

        return wrapper

    return decorator


def index_view_for(title: str, folder: str, detail_endpoint: str, index_template: str):
    def index_view():
        entries = load_entries(folder, meta_only=True)
        for entry in entries:
            entry["url"] = url_for(detail_endpoint, name=path.splitext(path.basename(entry["file"]))[0])
        return {"entries": entries, "title": title}

    return templated(index_template)(index_view)


def detail_view_for(folder: str, detail_endpoint: str, detail_template: str):
    def detail_view(name):
        entry_file = safe_join(folder, f"{name}.md")
        if not entry_file:
            abort(404)
        entry = load_entry(entry_file, meta_only=False)
        if entry is None:
            abort(404)
        entry["url"] = url_for(detail_endpoint, name=path.splitext(path.basename(entry["file"]))[0])
        return {"entry": entry}

    return templated(detail_template)(detail_view)


for mapping in config.get("mappings", []):
    title = mapping["title"]
    path_ = mapping["path"]
    assert path_.startswith("/")
    folder = path.join(root_folder, path_.lstrip("/"))
    index_url = mapping.get("index_url", path_).rstrip("/") + "/"
    assert index_url.startswith("/")
    detail_url = mapping.get("detail_url", path_).rstrip("/") + "/"
    assert detail_url.startswith("/")
    index_template = mapping["index_template"]
    detail_template = mapping["detail_template"]
    index_endpoint = index_url
    detail_endpoint = f"{detail_url}detail"
    index_view_func = index_view_for(title, folder, detail_endpoint, index_template)
    app.add_url_rule(f"{index_url}", index_endpoint, index_view_func, methods=["GET"])
    detail_view_func = detail_view_for(folder, detail_endpoint, detail_template)
    app.add_url_rule(f"{detail_url}<name>/", detail_endpoint, detail_view_func, methods=["GET"])


@app.route("/<path:rel_url>")
@templated("page")
def page(rel_url: str):
    page = load_page(rel_url)
    if not page:
        if rel_url.endswith("/"):
            rel_url += "/index.html"
        return send_from_directory(raw_folder, rel_url)
    context = {"entry": page}
    template = page.get("template")
    if template:
        if not template.endswith(".html"):
            template += ".html"
        return render_template([f"custom/{template}", template], **context)
    return context


@app.errorhandler(404)
@app.route("/404.html")
def page_not_found(e=None):
    return render_template("404.html"), 404
