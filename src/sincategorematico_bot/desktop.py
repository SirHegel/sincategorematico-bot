from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from .config import load_config
from .storage import StateStore

ROOT = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("SINCATEGOREMATICO_STATE_PATH", Path.home() / ".local/state/sincategorematico-bot/state.db"))
CONFIG = Path(os.environ.get("SINCATEGOREMATICO_CONFIG_PATH", ROOT / "config.toml"))


class DesktopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Sincategoremático · Centro de control")
        self.geometry("1060x700")
        self.minsize(760, 560)
        self.configure(bg="#080a15")
        self._build()
        self.refresh()

    def _build(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#080a15")
        style.configure("Card.TFrame", background="#15182b")
        style.configure("TLabel", background="#080a15", foreground="#f5f6ff", font=("Sans", 11))
        style.configure("Muted.TLabel", foreground="#9399b2")
        style.configure("Card.TLabel", background="#15182b", foreground="#f5f6ff")
        style.configure("Primary.TButton", padding=12, background="#7062ff", foreground="white", borderwidth=0)
        style.configure("Secondary.TButton", padding=12, background="#242841", foreground="white", borderwidth=0)
        root = ttk.Frame(self, padding=32); root.pack(fill="both", expand=True)
        ttk.Label(root, text="SINCATEGOREMÁTICO", foreground="#9b91ff", font=("Sans", 12, "bold")).pack(anchor="w")
        ttk.Label(root, text="Centro de operaciones", font=("Sans", 34, "bold")).pack(anchor="w", pady=(5, 6))
        ttk.Label(root, text="Control local del flujo editorial · ninguna credencial se muestra aquí", style="Muted.TLabel").pack(anchor="w")
        cards = ttk.Frame(root); cards.pack(fill="x", pady=28)
        self.values = [tk.StringVar(value="—") for _ in range(3)]
        for i, (title, value) in enumerate(zip(("BOT TELEGRAM", "PUBLICACIONES", "LÍMITE DIARIO"), self.values)):
            card = ttk.Frame(cards, style="Card.TFrame", padding=22); card.grid(row=0, column=i, padx=(0 if i == 0 else 7, 0), sticky="nsew"); cards.columnconfigure(i, weight=1)
            ttk.Label(card, text=title, style="Card.TLabel", foreground="#9399b2", font=("Sans", 9, "bold")).pack(anchor="w")
            ttk.Label(card, textvariable=value, style="Card.TLabel", font=("Sans", 22, "bold")).pack(anchor="w", pady=(10, 0))
        body = ttk.Frame(root); body.pack(fill="both", expand=True)
        controls = ttk.Frame(body, style="Card.TFrame", padding=24); controls.pack(side="left", fill="both", padx=(0, 14))
        ttk.Label(controls, text="MOTOR EDITORIAL", style="Card.TLabel", font=("Sans", 18, "bold")).pack(anchor="w")
        ttk.Label(controls, text="Los cambios se reflejan también en Telegram.", style="Card.TLabel", foreground="#9399b2", wraplength=280).pack(anchor="w", pady=(8, 28))
        ttk.Button(controls, text="▶  Reanudar flujo", style="Primary.TButton", command=lambda: self.control(False)).pack(fill="x", pady=5)
        ttk.Button(controls, text="Ⅱ  Pausar publicaciones", style="Secondary.TButton", command=lambda: self.control(True)).pack(fill="x", pady=5)
        ttk.Button(controls, text="↻  Actualizar", style="Secondary.TButton", command=self.refresh).pack(fill="x", pady=(28, 5))
        activity = ttk.Frame(body, style="Card.TFrame", padding=24); activity.pack(side="left", fill="both", expand=True)
        ttk.Label(activity, text="ACTIVIDAD RECIENTE", style="Card.TLabel", font=("Sans", 18, "bold")).pack(anchor="w")
        self.activity = tk.Listbox(activity, bg="#101322", fg="#dfe2f3", selectbackground="#2a2f4d", borderwidth=0, highlightthickness=0, activestyle="none")
        self.activity.pack(fill="both", expand=True, pady=(16, 0))

    def control(self, paused: bool) -> None:
        store = StateStore(STATE); store.set("publishing_paused", paused)
        store.add_activity("control", f"Publicaciones {'pausadas' if paused else 'reanudadas'} desde aplicación local"); store.close(); self.refresh()

    def refresh(self) -> None:
        config, store = load_config(CONFIG), StateStore(STATE)
        self.values[0].set("Conectado" if store.get_bool("telegram_initialized") else "Pendiente")
        self.values[1].set("En pausa" if store.get_bool("publishing_paused", default=True) else "En marcha")
        self.values[2].set(f"{config.max_posts_per_day} piezas")
        self.activity.delete(0, tk.END)
        for item in store.recent_activity(30):
            stamp = datetime.fromtimestamp(int(item["created_at"])).strftime("%d/%m · %H:%M")
            self.activity.insert(tk.END, f"  {stamp}    {item['message']}")
        if not self.activity.size(): self.activity.insert(tk.END, "  Aún no hay actividad registrada")
        store.close(); self.after(15000, self.refresh)


def main() -> None:
    DesktopApp().mainloop()


if __name__ == "__main__":
    main()
