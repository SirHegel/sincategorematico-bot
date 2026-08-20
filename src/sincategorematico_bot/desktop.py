from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import time
import tkinter as tk
from tkinter import simpledialog, ttk
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import load_config
from .linkedin import linkedin_credentials_usable, normalize_post_reference
from .runtime import TIME_PATTERN, apply_defaults, snapshot
from .storage import StateStore

ROOT = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("SINCATEGOREMATICO_STATE_PATH", Path.home() / ".local/state/sincategorematico-bot/state.db"))
CONFIG = Path(os.environ.get("SINCATEGOREMATICO_CONFIG_PATH", ROOT / "config.toml"))

ESTADOS = {
    "pending": "por revisar",
    "approved": "aprobado",
    "published": "publicado",
    "rejected": "descartado",
    "failed": "fallido",
    "publishing": "enviando",
    "uncertain": "resultado incierto",
    "discarded": "en reescritura",
}


def engine_card_status(data: dict[str, object], *, now: int | None = None) -> tuple[str, str]:
    """Texto veraz de la tarjeta del motor, independiente de Tk para probarlo."""

    engine = dict(data["engine"])  # type: ignore[arg-type]
    counts = dict(data["counts"])  # type: ignore[arg-type]
    queue = f"{counts['pending']} por revisar · {counts['approved']} aprobados"
    uncertain = int(counts.get("uncertain", 0))
    if uncertain:
        queue += f" · {uncertain} inciertos"
    if engine["alive"]:
        return ("En pausa" if data["paused"] else "En marcha", queue)

    heartbeat = int(engine["heartbeat_at"])
    if not heartbeat:
        pulse = "SIN PULSO · nunca iniciado"
    else:
        age = (int(time.time()) if now is None else int(now)) - heartbeat
        pulse = (
            f"SIN PULSO · último hace {age // 60} min"
            if age >= 0
            else "SIN PULSO · reloj inconsistente"
        )
    return "Motor detenido", f"{pulse} · {queue}"


class DesktopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__(className="Sincategorematico")
        self.title("Sincategoremático · Centro de control")
        self._app_icon = tk.PhotoImage(file=ROOT / "web/logo.png")
        self.iconphoto(True, self._app_icon)
        self.geometry("1180x780")
        self.minsize(860, 620)
        self.configure(bg="#080a15")
        self.drafts: list[dict[str, object]] = []
        self._refresh_job: str | None = None
        self._build()
        self.refresh()

    # -- interfaz ---------------------------------------------------------

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
        style.configure("TCombobox", fieldbackground="#101322", background="#242841", foreground="white")
        root = ttk.Frame(self, padding=28); root.pack(fill="both", expand=True)
        ttk.Label(root, text="SINCATEGOREMÁTICO", foreground="#9b91ff", font=("Sans", 12, "bold")).pack(anchor="w")
        ttk.Label(root, text="Centro de operaciones", font=("Sans", 32, "bold")).pack(anchor="w", pady=(5, 6))
        self.gate = tk.StringVar(value="—")
        ttk.Label(root, textvariable=self.gate, style="Muted.TLabel").pack(anchor="w")

        cards = ttk.Frame(root); cards.pack(fill="x", pady=24)
        self.values = [tk.StringVar(value="—") for _ in range(4)]
        self.notes = [tk.StringVar(value="") for _ in range(4)]
        titles = ("BOT TELEGRAM", "PUBLICACIONES", "LÍMITE DIARIO", "LINKEDIN")
        for index, title in enumerate(titles):
            card = ttk.Frame(cards, style="Card.TFrame", padding=20)
            card.grid(row=0, column=index, padx=(0 if index == 0 else 7, 0), sticky="nsew")
            cards.columnconfigure(index, weight=1)
            ttk.Label(card, text=title, style="Card.TLabel", foreground="#9399b2", font=("Sans", 9, "bold")).pack(anchor="w")
            ttk.Label(card, textvariable=self.values[index], style="Card.TLabel", font=("Sans", 19, "bold")).pack(anchor="w", pady=(8, 0))
            ttk.Label(card, textvariable=self.notes[index], style="Card.TLabel", foreground="#9399b2", font=("Sans", 9), wraplength=220).pack(anchor="w")

        body = ttk.Frame(root); body.pack(fill="both", expand=True)
        self._build_controls(body)
        self._build_queue(body)

    def _build_controls(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent, style="Card.TFrame", padding=22)
        controls.pack(side="left", fill="y", padx=(0, 14))
        ttk.Label(controls, text="MOTOR EDITORIAL", style="Card.TLabel", font=("Sans", 17, "bold")).pack(anchor="w")
        ttk.Label(controls, text="Los cambios se reflejan también en Telegram.", style="Card.TLabel", foreground="#9399b2", wraplength=260).pack(anchor="w", pady=(6, 20))
        ttk.Button(controls, text="▶  Reanudar flujo", style="Primary.TButton", command=lambda: self.control(False)).pack(fill="x", pady=4)
        ttk.Button(controls, text="Ⅱ  Pausar motor completo", style="Secondary.TButton", command=lambda: self.control(True)).pack(fill="x", pady=4)

        ttk.Label(controls, text="CONFIGURACIÓN", style="Card.TLabel", foreground="#9399b2", font=("Sans", 9, "bold")).pack(anchor="w", pady=(22, 6))
        self.max_posts = tk.IntVar(value=4)
        self.timezone = tk.StringVar(value="America/Bogota")
        self.window_start = tk.StringVar(value="07:30")
        self.window_end = tk.StringVar(value="20:30")
        self.approval = tk.StringVar(value="Aprobación manual")
        self.mode = tk.StringVar(value="Simulación")
        tk.Spinbox(controls, from_=1, to=50, textvariable=self.max_posts, bg="#101322", fg="white", buttonbackground="#242841", relief="flat").pack(fill="x", pady=3)
        ttk.Entry(controls, textvariable=self.timezone).pack(fill="x", pady=3)
        franja = ttk.Frame(controls, style="Card.TFrame"); franja.pack(fill="x", pady=3)
        ttk.Entry(franja, textvariable=self.window_start, width=8).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Entry(franja, textvariable=self.window_end, width=8).pack(side="left", expand=True, fill="x")
        ttk.Combobox(controls, textvariable=self.approval, state="readonly", values=("Aprobación manual", "Aprobación automática")).pack(fill="x", pady=3)
        ttk.Combobox(controls, textvariable=self.mode, state="readonly", values=("Simulación", "Publicar en LinkedIn")).pack(fill="x", pady=3)
        ttk.Button(controls, text="✓  Guardar configuración", style="Primary.TButton", command=self.save_settings).pack(fill="x", pady=4)
        self.status = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.status, style="Card.TLabel", foreground="#1dd9aa", wraplength=260, font=("Sans", 9)).pack(anchor="w", pady=(6, 0))
        ttk.Button(controls, text="↻  Actualizar", style="Secondary.TButton", command=self.refresh).pack(fill="x", pady=(18, 0))

    def _build_queue(self, parent: ttk.Frame) -> None:
        right = ttk.Frame(parent); right.pack(side="left", fill="both", expand=True)
        queue = ttk.Frame(right, style="Card.TFrame", padding=22); queue.pack(fill="both", expand=True)
        ttk.Label(queue, text="COLA EDITORIAL", style="Card.TLabel", font=("Sans", 17, "bold")).pack(anchor="w")
        self.queue = tk.Listbox(queue, bg="#101322", fg="#dfe2f3", selectbackground="#2a2f4d", borderwidth=0, highlightthickness=0, activestyle="none", height=7, exportselection=False)
        self.queue.pack(fill="x", pady=(12, 8))
        self.queue.bind("<<ListboxSelect>>", lambda _event: self.show_selected())
        self.preview = tk.Text(queue, bg="#101322", fg="#c7cbe0", borderwidth=0, highlightthickness=0, wrap="word", height=9, font=("Sans", 10))
        self.preview.pack(fill="both", expand=True)
        self.preview.configure(state="disabled")
        actions = ttk.Frame(queue, style="Card.TFrame"); actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="✅  Publicar", style="Primary.TButton", command=lambda: self.decide("approved")).pack(side="left", expand=True, fill="x", padx=(0, 5))
        ttk.Button(actions, text="↻  Reintentar", style="Secondary.TButton", command=lambda: self.decide("retry")).pack(side="left", expand=True, fill="x", padx=(0, 5))
        ttk.Button(actions, text="✓  Ya publicado", style="Secondary.TButton", command=self.confirm_published).pack(side="left", expand=True, fill="x", padx=(0, 5))
        ttk.Button(actions, text="🗑  Descartar", style="Secondary.TButton", command=lambda: self.decide("rejected")).pack(side="left", expand=True, fill="x")

        activity = ttk.Frame(right, style="Card.TFrame", padding=22); activity.pack(fill="both", expand=True, pady=(14, 0))
        ttk.Label(activity, text="ACTIVIDAD RECIENTE", style="Card.TLabel", font=("Sans", 17, "bold")).pack(anchor="w")
        self.activity = tk.Listbox(activity, bg="#101322", fg="#dfe2f3", selectbackground="#2a2f4d", borderwidth=0, highlightthickness=0, activestyle="none", height=6)
        self.activity.pack(fill="both", expand=True, pady=(12, 0))

    # -- acciones ---------------------------------------------------------

    def control(self, paused: bool) -> None:
        store = StateStore(STATE); store.set("publishing_paused", paused)
        store.add_activity("control", f"Motor {'pausado' if paused else 'reanudado'} desde aplicación local"); store.close(); self.refresh()

    def save_settings(self) -> None:
        maximum = max(1, min(50, int(self.max_posts.get())))
        timezone = self.timezone.get().strip() or "America/Bogota"
        inicio, fin = self.window_start.get().strip(), self.window_end.get().strip()
        if not TIME_PATTERN.match(inicio) or not TIME_PATTERN.match(fin):
            self.status.set("La franja debe escribirse como hh:mm")
            return
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            self.status.set("La zona horaria no existe; usa por ejemplo America/Bogota")
            return
        store = StateStore(STATE)
        real = self.mode.get().startswith("Publicar")
        if real and not linkedin_credentials_usable(
            access_token=store.get("linkedin_access_token") or "",
            author_urn=store.get("linkedin_author_urn") or "",
            expires_at=store.get_int("linkedin_expires_at") or 0,
            scope=store.get("linkedin_scope") or "",
        ):
            store.close()
            self.status.set("Vincula LinkedIn antes de salir de simulación")
            return
        store.set("max_posts_per_day", maximum)
        store.set("timezone", timezone)
        store.set("publish_window_start", inicio)
        store.set("publish_window_end", fin)
        store.set("approval_required", self.approval.get().endswith("manual"))
        store.set("dry_run", not real)
        store.add_activity("settings", f"Configuración actualizada: {maximum} piezas/día · {inicio}–{fin} · {timezone}")
        store.close()
        self.status.set("Configuración guardada")
        self.refresh()

    def selected_draft(self) -> dict[str, object] | None:
        selection = self.queue.curselection()
        if not selection or selection[0] >= len(self.drafts):
            return None
        return self.drafts[selection[0]]

    def show_selected(self) -> None:
        draft = self.selected_draft()
        self.preview.configure(state="normal")
        self.preview.delete("1.0", tk.END)
        if draft is not None:
            texto = str(draft["preview"])
            if draft["link"]:
                texto += f"\n\nEnlace: {draft['link']}"
            if draft["url"]:
                texto += f"\n\nPublicado: {draft['url']}"
            if draft["error"]:
                texto += f"\n\nError: {draft['error']}"
            self.preview.insert("1.0", texto)
        self.preview.configure(state="disabled")

    def decide(self, state: str) -> None:
        draft = self.selected_draft()
        if draft is None:
            self.status.set("Selecciona un borrador de la cola")
            return
        if state == "retry":
            store = StateStore(STATE)
            retried = store.retry_draft(int(draft["id"]))
            if retried:
                store.add_activity(
                    "control", f"Borrador #{draft['id']} puesto de nuevo en cola desde aplicación local"
                )
            store.close()
            self.status.set(
                f"Borrador #{draft['id']} listo para reintentar"
                if retried else "Solo un resultado incierto o fallido puede reintentarse"
            )
            self.refresh()
            return
        if str(draft["state"]) != "pending":
            self.status.set("Ese borrador ya no está pendiente")
            return
        store = StateStore(STATE)
        store.set_draft_state(int(draft["id"]), state)
        store.add_activity(
            "control",
            f"Borrador #{draft['id']} {'aprobado' if state == 'approved' else 'descartado'} desde aplicación local",
        )
        store.close()
        self.status.set(f"Borrador #{draft['id']} {'aprobado' if state == 'approved' else 'descartado'}")
        self.refresh()

    def confirm_published(self) -> None:
        draft = self.selected_draft()
        if draft is None or str(draft["state"]) != "uncertain":
            self.status.set("Selecciona un borrador con resultado incierto")
            return
        reference = simpledialog.askstring(
            "Conciliar publicación",
            "Pega la URN o URL de la publicación que comprobaste en LinkedIn:",
            parent=self,
        )
        if reference is None:
            return
        urn = normalize_post_reference(reference)
        if urn is None:
            self.status.set("La URN o URL de LinkedIn no es válida")
            return
        store = StateStore(STATE)
        confirmed = store.reconcile_draft_as_published(int(draft["id"]), urn)
        if confirmed:
            store.add_activity(
                "control",
                f"Borrador #{draft['id']} conciliado como publicado desde aplicación local",
            )
        store.close()
        self.status.set(
            f"Borrador #{draft['id']} confirmado sin repetir el envío"
            if confirmed
            else "El borrador ya cambió de estado"
        )
        self.refresh()

    # -- refresco ---------------------------------------------------------

    def refresh(self) -> None:
        config, store = load_config(CONFIG), StateStore(STATE)
        apply_defaults(store, config)
        data = snapshot(store, config)
        store.close()

        counts = data["counts"]
        linkedin = data["linkedin"]
        self.gate.set(f"Ahora mismo: {data['gate']}")
        self.values[0].set("Conectado" if data["initialized"] else "Pendiente")
        self.notes[0].set("Propietario verificado" if data["owner"] else "Sin propietario vinculado")
        engine_value, engine_note = engine_card_status(data)
        self.values[1].set(engine_value)
        self.notes[1].set(engine_note)
        self.values[2].set(f"{data['max_posts']} piezas")
        self.notes[2].set(f"{data['window']} · {data['timezone']}")
        self.values[3].set("Vinculado" if linkedin["linked"] else "Sin vincular")
        self.notes[3].set(
            f"{linkedin['kind']} · caduca en {linkedin['days_left']} días"
            if linkedin["linked"]
            else "Ejecuta configure_linkedin.py"
        )

        self.max_posts.set(int(data["max_posts"]))
        self.timezone.set(str(data["timezone"]))
        inicio, _, fin = str(data["window"]).partition("–")
        self.window_start.set(inicio); self.window_end.set(fin)
        self.approval.set("Aprobación manual" if data["approval_required"] else "Aprobación automática")
        self.mode.set("Simulación" if data["dry_run"] else "Publicar en LinkedIn")

        seleccionado = self.selected_draft()
        self.drafts = list(data["drafts"])
        self.queue.delete(0, tk.END)
        for draft in self.drafts:
            estado = ESTADOS.get(str(draft["state"]), str(draft["state"]))
            self.queue.insert(tk.END, f"  #{draft['id']}  ·  {estado}  ·  {str(draft['title'])[:70]}")
        if not self.drafts:
            self.queue.insert(tk.END, "  La cola está vacía")
        elif seleccionado is not None:
            for index, draft in enumerate(self.drafts):
                if draft["id"] == seleccionado["id"]:
                    self.queue.selection_set(index)
                    break
        self.show_selected()

        self.activity.delete(0, tk.END)
        for item in data["activity"]:
            stamp = datetime.fromtimestamp(int(item["created_at"])).strftime("%d/%m · %H:%M")
            self.activity.insert(tk.END, f"  {stamp}    {item['message']}")
        if not self.activity.size():
            self.activity.insert(tk.END, "  Aún no hay actividad registrada")
        if self._refresh_job is not None:
            try:
                self.after_cancel(self._refresh_job)
            except tk.TclError:
                pass
        self._refresh_job = self.after(15000, self.refresh)


def main() -> None:
    DesktopApp().mainloop()


if __name__ == "__main__":
    main()
