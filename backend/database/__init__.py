"""
database/__init__.py
=====================
Exports the shared SQLAlchemy db object.  Import from here so every
module uses the same instance and Flask's application-context binding works.
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
