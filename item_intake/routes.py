"""
Depot smart intake — staff-only endpoints.

    GET  /depot/items/new                         camera scan page
    POST /api/intake/drafts                       start a scan            → 201 {draft}
    POST /api/intake/drafts/<id>/frames           multipart frame, step   → live feedback
    POST /api/intake/drafts/<id>/closeups         multipart photo, kind   → feedback
    POST /api/intake/drafts/<id>/finalize         {route_id, date, trip_id?, found_seat_area?} → 202
    GET  /api/intake/drafts/<id>                  progress / result
    POST /api/intake/drafts/<id>/discard
"""
import functools

from flask import Blueprint, current_app, g, jsonify, make_response, render_template, request

from match_notify import require_staff

from .service import IntakeError

bp = Blueprint("item_intake", __name__)
MAX_UPLOAD = 4 * 1024 * 1024


def _svc():
    return current_app.extensions["item_intake"].service


def _json(payload, status=200):
    resp = make_response(jsonify(payload), status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _handled(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except IntakeError as e:
            return _json({"ok": False, "error": {"code": e.code, "message": e.message}}, e.status)
    return wrapper


def _upload(field):
    f = request.files.get(field)
    if f is None:
        raise IntakeError(400, "missing_file", f"Attach the image as '{field}'.")
    data = f.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise IntakeError(413, "too_large", "That image is too large.")
    return data


@bp.route("/depot/items/new", methods=["GET"])
@require_staff(page=True)
def intake_page():
    cfg = current_app.extensions["item_intake"].config
    return render_template("depot_intake.html", depot=g.staff_depot, cfg=cfg)


@bp.route("/api/intake/drafts", methods=["POST"])
@require_staff
@_handled
def create_draft():
    svc = _svc()
    return _json({"ok": True, "draft": svc.view(svc.create(g.staff_depot))}, 201)


@bp.route("/api/intake/drafts/<draft_id>", methods=["GET"])
@require_staff
@_handled
def get_draft(draft_id):
    svc = _svc()
    return _json({"ok": True, "draft": svc.view(svc.get(draft_id, g.staff_depot["phone"]))})


@bp.route("/api/intake/drafts/<draft_id>/frames", methods=["POST"])
@require_staff
@_handled
def add_frame(draft_id):
    svc = _svc()
    draft = svc.get(draft_id, g.staff_depot["phone"])
    return _json({"ok": True, **svc.add_frame(draft, request.form.get("step", "outside"), _upload("frame"))})


@bp.route("/api/intake/drafts/<draft_id>/closeups", methods=["POST"])
@require_staff
@_handled
def add_closeup(draft_id):
    svc = _svc()
    draft = svc.get(draft_id, g.staff_depot["phone"])
    return _json({"ok": True, **svc.add_closeup(draft, request.form.get("kind", "tag"), _upload("photo"))})


@bp.route("/api/intake/drafts/<draft_id>/finalize", methods=["POST"])
@require_staff
@_handled
def finalize(draft_id):
    svc = _svc()
    draft = svc.get(draft_id, g.staff_depot["phone"])
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or any(not isinstance(v, (str, type(None))) for v in body.values()):
        raise IntakeError(400, "invalid_request", "Send the journey as a JSON object of strings.")
    done = svc.finalize(draft, g.staff_depot, body)
    return _json({"ok": True, "draft": svc.view(done)}, 202)


@bp.route("/api/intake/drafts/<draft_id>/discard", methods=["POST"])
@require_staff
@_handled
def discard(draft_id):
    svc = _svc()
    svc.discard(svc.get(draft_id, g.staff_depot["phone"]))
    return _json({"ok": True})
