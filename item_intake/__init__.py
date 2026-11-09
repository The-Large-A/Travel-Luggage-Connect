"""
Phase 2, Feature C — depot smart intake: "Add new lost item" with a camera scan.

Staff press one button, the laptop webcam or phone camera opens, they turn the
item in front of it (then show the inside and any tag), and the system builds
the found-item record, its description, its tag QR and its ownership questions
automatically. OpenCV picks the keyframes and reads colours / text regions;
CLIP, OCR and an offline captioner (Florence-2) supply the meaning.

Wire into the app with:

    from item_intake import init_item_intake
    init_item_intake(app, db, create_found=create_found_report,
                     classifier=image_sim, ocr=ocr_ext)
"""
import logging
import os
from dataclasses import dataclass

from passenger_auth.timeutil import utcnow

from .captioner import build_captioner
from .config import IntakeConfig
from .routes import bp
from .service import IntakeService, ensure_intake_indexes

__all__ = ["init_item_intake", "IntakeConfig", "IntakeService"]
logger = logging.getLogger(__name__)


@dataclass
class IntakeState:
    config: IntakeConfig
    service: IntakeService


def init_item_intake(app, db, create_found, classifier=None, ocr=None, config=None, clock=None,
                     captioner=None, upload_root=None, environ=None):
    config = config or IntakeConfig.from_env(environ)

    def run_in_app(fn):
        def wrapped():
            with app.app_context():
                return fn()
        return wrapped

    service = IntakeService(db, config, clock or utcnow,
                            upload_root or os.path.join(app.root_path, "static"),
                            classifier, ocr, captioner or build_captioner(config), create_found, run_in_app)
    ensure_intake_indexes(db)
    app.extensions["item_intake"] = IntakeState(config=config, service=service)
    app.config.setdefault("FEATURES", set()).add("item_intake")
    app.register_blueprint(bp)
    logger.info("Smart intake enabled (description writer: %s).", config.vision_provider)
    return app.extensions["item_intake"]
