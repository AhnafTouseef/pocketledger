from flask import Blueprint, render_template, request

from ..db import get_db
from ..services import analytics as svc

bp = Blueprint("analytics", __name__, url_prefix="/analytics")


@bp.route("/")
def index():
    conn = get_db()
    data = svc.build(conn, request.args.get("range"), request.args.get("start"), request.args.get("end"))
    return render_template("analytics/index.html", a=data)
