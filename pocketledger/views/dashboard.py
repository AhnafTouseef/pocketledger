from flask import Blueprint, render_template

from ..db import get_db
from ..services import dashboard as dashboard_svc

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    return render_template("dashboard.html", d=dashboard_svc.build(get_db()))
