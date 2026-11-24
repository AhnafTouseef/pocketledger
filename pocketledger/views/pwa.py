"""Installability: manifest, service worker (served from the root for full scope), offline page."""
from flask import Blueprint, Response, current_app, jsonify, render_template, send_from_directory, url_for

bp = Blueprint("pwa", __name__)

# Bump when precached assets change so clients refresh their cache.
CACHE_VERSION = "pl-v1"


@bp.route("/manifest.webmanifest")
def manifest():
    data = {
        "name": "PocketLedger",
        "short_name": "PocketLedger",
        "description": "Personal finance control center",
        "id": "/",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#121413",
        "theme_color": "#121413",
        "icons": [
            {"src": url_for("static", filename="icons/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": url_for("static", filename="icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": url_for("static", filename="icons/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "Add expense", "url": "/transactions/new?type=expense"},
            {"name": "Add income", "url": "/transactions/new?type=income"},
            {"name": "Verify cash", "url": "/reconcile/"},
        ],
    }
    resp = jsonify(data)
    resp.mimetype = "application/manifest+json"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/sw.js")
def service_worker():
    resp = send_from_directory(current_app.static_folder, "js/sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/offline")
def offline():
    return render_template("offline.html")


@bp.route("/healthz")
def health():
    return jsonify(ok=True)
