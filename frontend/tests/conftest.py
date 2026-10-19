"""Prepara o ambiente dos testes do frontend: módulos em frontend/backend e cwd lá
(templates/ e static/ são caminhos relativos), sem subir os servidores."""
import os
import sys

APP_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(APP_DIR))
os.chdir(os.path.abspath(APP_DIR))
os.environ.setdefault("PAYMENT_MODE", "mock")
