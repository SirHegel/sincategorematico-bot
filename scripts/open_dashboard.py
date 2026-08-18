#!/usr/bin/env python3
from pathlib import Path
import webbrowser

from configure_token import ENV_PATH, read_environment

values = read_environment(ENV_PATH)
key = values.get("SINCATEGOREMATICO_DASHBOARD_TOKEN", "")
print("Panel: http://127.0.0.1:8765")
print(f"Clave privada: {key or 'No configurada'}")
webbrowser.open("http://127.0.0.1:8765")
input("La clave queda visible aquí. Pulsa Enter para cerrar…")
