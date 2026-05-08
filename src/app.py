"""Coralforge -- release lifecycle manager.

Entry point that wires configuration, core, and HTTP API together.
Designed to replace the app_old.py monolith.
"""

import logging
import os
import sys

_src_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _src_parent not in sys.path:
    sys.path.insert(0, _src_parent)

from flask import Flask

from src.api.coralforge_http import api, init_core
from src.config.app_config import AppConfig
from src.core.app_core import AppCore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("coralforge")


def create_app() -> Flask:
    app = Flask(__name__)

    config = AppConfig()
    config.load()

    core = AppCore(config)
    core.initialize()

    init_core(core)
    app.register_blueprint(api)

    logger.info("Coralforge app created (%d repo(s))", len(config.repos))
    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)