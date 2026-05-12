"""HTTP API for normalized Coralforge CI orchestration."""

from __future__ import annotations

import logging
from typing import Optional

from flask import Blueprint, jsonify, request

from src.core.app_core import AppCore

logger = logging.getLogger("coralforge.api")

api = Blueprint("api", __name__, url_prefix="/api/v1")
_core: Optional[AppCore] = None


def init_core(core: AppCore) -> None:
    global _core
    _core = core


def _require_core():
    if _core is None:
        return jsonify({"error": "core not initialized"}), 503
    return None


@api.route("/healthz")
def healthz():
    core_err = _require_core()
    if core_err:
        return core_err
    return jsonify(_core.health())


@api.route("/repos", methods=["GET"])
def list_repos():
    core_err = _require_core()
    if core_err:
        return core_err
    return jsonify({"repos": _core.list_repo_definitions()})


@api.route("/runs", methods=["GET"])
def list_runs():
    core_err = _require_core()
    if core_err:
        return core_err

    target = request.args.get("target")
    run_type = request.args.get("run_type")
    limit = int(request.args.get("limit", "20"))
    refresh = request.args.get("refresh") == "1"

    runs = _core.list_runs(target, run_type, limit)
    if refresh:
        hydrated = []
        for run in runs:
            hydrated.append(_core.get_run(run["run_id"], refresh=True) or run)
        runs = hydrated
    return jsonify({"runs": runs})


@api.route("/runs", methods=["POST"])
def trigger_run():
    core_err = _require_core()
    if core_err:
        return core_err

    payload = request.get_json(silent=True) or {}
    target = payload.get("target") or request.args.get("target")
    run_type = payload.get("run_type") or request.args.get("run_type")
    ref = payload.get("ref") or request.args.get("ref")
    actor = payload.get("actor") or request.headers.get("X-Coralforge-Actor", "api")
    provider = payload.get("provider") or request.args.get("provider")
    inputs = payload.get("inputs")

    if not target or not run_type:
        return jsonify({"error": "target and run_type are required"}), 400

    try:
        run = _core.trigger_run(
            repo_name=target,
            run_type=run_type,
            ref=ref,
            actor=actor,
            provider_name=provider,
            inputs=inputs,
        )
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        logger.exception("trigger_run failed")
        return jsonify({"error": str(exc)}), 409
    return jsonify(run), 202


@api.route("/runs/<run_id>", methods=["GET"])
def get_run(run_id: str):
    core_err = _require_core()
    if core_err:
        return core_err

    run = _core.get_run(run_id, refresh=request.args.get("refresh") == "1")
    if run is None:
        return jsonify({"error": "run not found"}), 404
    return jsonify(run)


@api.route("/runs/<run_id>/logs", methods=["GET"])
def get_logs(run_id: str):
    core_err = _require_core()
    if core_err:
        return core_err

    logs = _core.get_logs(run_id, refresh=request.args.get("refresh") == "1")
    if logs is None:
        return jsonify({"error": "run not found"}), 404
    return jsonify(logs)


@api.route("/runs/<run_id>/provider", methods=["GET"])
def get_provider(run_id: str):
    core_err = _require_core()
    if core_err:
        return core_err

    payload = _core.get_provider_metadata(run_id)
    if payload is None:
        return jsonify({"error": "run not found"}), 404
    return jsonify(payload)


@api.route("/repo")
def repo_query():
    core_err = _require_core()
    if core_err:
        return core_err

    op = request.args.get("op", "list")
    target = request.args.get("target")

    if op == "list":
        return jsonify({"repos": _core.list_repo_definitions()})
    if op == "query":
        return jsonify({"repos": _core.get_status(target)})
    if op == "stable":
        return jsonify({"stables": _core.get_stable(target)})
    if op == "all":
        return jsonify({"repos": _core.get_all_versions(target)})
    return jsonify({"error": f"unknown op: {op}"}), 400


@api.route("/trigger-release", methods=["PUT"])
def trigger_release():
    core_err = _require_core()
    if core_err:
        return core_err

    bump = request.args.get("type", "patch")
    target = request.args.get("target", "")
    if not target:
        return jsonify({"error": "target required"}), 400
    ok, message = _core.trigger_release(target, bump)
    return jsonify({"success": ok, "message": message}), 200 if ok else 409


@api.route("/promote", methods=["PUT"])
def promote():
    core_err = _require_core()
    if core_err:
        return core_err

    target = request.args.get("target", "")
    if not target:
        return jsonify({"error": "target required"}), 400
    ok, message = _core.promote_release(target)
    return jsonify({"success": ok, "message": message}), 200 if ok else 409


@api.route("/merge", methods=["PUT"])
def merge():
    core_err = _require_core()
    if core_err:
        return core_err

    target = request.args.get("target", "")
    if not target:
        return jsonify({"error": "target required"}), 400
    ok, message = _core.merge_release(target)
    return jsonify({"success": ok, "message": message}), 200 if ok else 409


@api.route("/set-stable", methods=["PUT"])
def set_stable():
    core_err = _require_core()
    if core_err:
        return core_err

    target = request.args.get("target", "")
    version = request.args.get("version", "")
    if not target or not version:
        return jsonify({"error": "target and version required"}), 400
    ok, message = _core.set_stable(target, version)
    return jsonify({"success": ok, "message": message}), 200 if ok else 409


@api.route("/stop", methods=["DELETE"])
def stop():
    core_err = _require_core()
    if core_err:
        return core_err

    target = request.args.get("target", "")
    version = request.args.get("version", "")
    if not target or not version:
        return jsonify({"error": "target and version required"}), 400
    ok, message = _core.stop_release(target, version)
    return jsonify({"success": ok, "message": message}), 200 if ok else 409
