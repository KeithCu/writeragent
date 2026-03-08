"""
LocalWriter.org — Flask app for the project website.
"""
from flask import Flask, render_template
from functools import lru_cache

app = Flask(__name__)

RELEASE_URL = "https://github.com/balisujohn/localwriter/releases"
GITHUB_URL = "https://github.com/balisujohn/localwriter"
KOFI_URL = "https://ko-fi.com/johnbalis"
CONFIG_EXAMPLES_URL = f"{GITHUB_URL}/blob/main/CONFIG_EXAMPLES.md"


@app.route("/")
@lru_cache(maxsize=1)
def index():
    return render_template("index.html", release_url=RELEASE_URL, github_url=GITHUB_URL, kofi_url=KOFI_URL)


@app.route("/install/")
@lru_cache(maxsize=1)
def install():
    return render_template("install.html", release_url=RELEASE_URL, github_url=GITHUB_URL, config_examples_url=CONFIG_EXAMPLES_URL)


@app.route("/privacy/")
@lru_cache(maxsize=1)
def privacy():
    return render_template("privacy.html", github_url=GITHUB_URL)


@app.route("/local/")
@lru_cache(maxsize=1)
def local():
    return render_template("local.html", github_url=GITHUB_URL)


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html", github_url=GITHUB_URL), 404
