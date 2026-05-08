"""HTTP API blueprint for coralforge.

Thin layer: validates input, calls AppCore, returns JSON responses.
Designed so core can be reused by gRPC, jackfield connector, etc.

See the design doc in this file's header comment for the full API spec.
"""

import logging
from typing import Optional

from flask import Blueprint, jsonify, request

from src.core.app_core import AppCore

logger = logging.getLogger("coralforge.api")

api = Blueprint("api", __name__, url_prefix="/api/v1")

# Assigned at blueprint registration time by app.py
_core: Optional[AppCore] = None


def init_core(core: AppCore) -> None:
    """Inject the AppCore instance into this blueprint."""
    global _core
    _core = core


# ── Helpers ────────────────────────────────────────────────────────

def _require_core():
    if _core is None:
        return jsonify({"error": "core not initialized"}), 503


# ── Health ─────────────────────────────────────────────────────────

@api.route("/healthz")
def healthz():
    core_err = _require_core()
    if core_err:
        return core_err
    return jsonify(_core.health())


# ── Repo queries ───────────────────────────────────────────────────

@api.route("/repo")
def repo_query():
    core_err = _require_core()
    if core_err:
        return core_err

    op = request.args.get("op", "list")

    if op == "list":
        repos = _core.list_repos()
        return jsonify({"repos": repos})

    if op == "query":
        target = request.args.get("target")
        statuses = _core.get_status(target)
        return jsonify({"repos": statuses})

    if op == "stable":
        target = request.args.get("target")
        stables = _core.get_stable(target)
        return jsonify({"stables": stables})

    return jsonify({"error": f"unknown op: {op}"}), 400


# ── Mutations ──────────────────────────────────────────────────────

@api.route("/trigger-release", methods=["PUT"])
def trigger_release():
    core_err = _require_core()
    if core_err:
        return core_err

    bump = request.args.get("type", "patch")
    target = request.args.get("target", "")
    if not target:
        return jsonify({"error": "target required"}), 400

    ok, msg = _core.trigger_release(target, bump)
    status = 200 if ok else 409
    return jsonify({"success": ok, "message": msg}), status


@api.route("/promote", methods=["PUT"])
def promote():
    core_err = _require_core()
    if core_err:
        return core_err

    target = request.args.get("target", "")
    if not target:
        return jsonify({"error": "target required"}), 400

    ok, msg = _core.promote_release(target)
    status = 200 if ok else 409
    return jsonify({"success": ok, "message": msg}), status


@api.route("/merge", methods=["PUT"])
def merge():
    core_err = _require_core()
    if core_err:
        return core_err

    target = request.args.get("target", "")
    if not target:
        return jsonify({"error": "target required"}), 400

    ok, msg = _core.merge_release(target)
    status = 200 if ok else 409
    return jsonify({"success": ok, "message": msg}), status


@api.route("/set-stable", methods=["PUT"])
def set_stable():
    core_err = _require_core()
    if core_err:
        return core_err

    target = request.args.get("target", "")
    version = request.args.get("version", "")
    if not target or not version:
        return jsonify({"error": "target and version required"}), 400

    ok, msg = _core.set_stable(target, version)
    status = 200 if ok else 409
    return jsonify({"success": ok, "message": msg}), status


@api.route("/stop", methods=["DELETE"])
def stop():
    core_err = _require_core()
    if core_err:
        return core_err

    target = request.args.get("target", "")
    version = request.args.get("version", "")
    if not target or not version:
        return jsonify({"error": "target and version required"}), 400

    ok, msg = _core.stop_release(target, version)
    status = 200 if ok else 409
    return jsonify({"success": ok, "message": msg}), status