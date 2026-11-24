"""WSGI entry point: gunicorn wsgi:app  |  waitress-serve --call wsgi:create_app  |  flask --app wsgi <cmd>"""
from pocketledger import create_app

app = create_app()
