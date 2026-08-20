# Sincategoremático Bot

Bot local para descubrir noticias, redactar borradores con una CLI de IA,
aprobarlos por Telegram y publicarlos en LinkedIn con límites y horarios. Incluye
un motor editorial independiente, un panel web ligado a `127.0.0.1` y una
aplicación de escritorio. El estado y todas las credenciales viven fuera del
repositorio.

## Principios de seguridad

- Una instalación nueva queda **pausada** y en **simulación**. No publica en
  LinkedIn hasta que el propietario vincule una cuenta y active expresamente el
  modo real.
- Antes de enviar, el motor reserva el borrador en la base de datos. Si la
  respuesta de LinkedIn se pierde o el proceso se interrumpe, queda como
  `uncertain` y no se reintenta automáticamente: esto evita publicaciones
  duplicadas.
- Telegram acepta comandos de un único propietario vinculado mediante un código
  local temporal.
- La redacción recibe un entorno mínimo; no hereda los tokens de Telegram,
  LinkedIn ni del panel.
- Los servicios de systemd se ejecutan con aislamiento, límites de recursos y
  permisos de escritura restringidos al estado local y, para el redactor, a la
  sesión local de la CLI de IA.

Los datos privados se guardan aquí:

- `~/.config/sincategorematico-bot/bot.env` — token de Telegram, clave del panel
  y credenciales de la aplicación de LinkedIn; modo `0600`.
- `~/.local/state/sincategorematico-bot/state.db` — propietario, borradores,
  actividad y tokens OAuth de LinkedIn.

No copies esos archivos al repositorio, a un issue ni a un registro público.

## Requisitos

- Linux con Python 3.11 o posterior y systemd de usuario.
- Una cuenta de Telegram y un bot creado con BotFather.
- La CLI de Claude 2.1.235 o posterior, instalada y autenticada localmente para redactar. Sin ella,
  los paneles y Telegram funcionan, pero el motor no genera borradores.
- Para publicar: una aplicación de LinkedIn con OAuth y el producto/permisos
  correspondientes al perfil personal o a la organización elegida.
- Tkinter es opcional y solo se necesita para la aplicación de escritorio.

## Varias cuentas de Claude para redacción

Sin configuración adicional, el motor conserva el comportamiento original y usa la
cuenta Claude predeterminada de la sesión. Opcionalmente puede recibir una lista
ordenada de cuentas locales: si una alcanza su cuota o pierde la autenticación, prueba
la siguiente con exactamente el mismo encargo. Un límite de cuenta no consume los
intentos de la noticia ni provoca que se descarte.

Las cuentas se ejecutan siempre con el mismo aislamiento del redactor: directorio
temporal, `--safe-mode`, sin persistencia de sesión, Chrome, herramientas ni servidores
MCP, y con un entorno que excluye los secretos del bot. El cambio de cuenta solo
modifica `CLAUDE_CONFIG_DIR`; no integra un agente u orquestador con acceso al equipo.

Los directorios deben existir, pertenecer al usuario actual, tener modo `0700`, estar
ya autenticados y usar rutas absolutas canónicas. Regístralos sin copiar ni leer sus
credenciales:

```bash
python3 scripts/configure_writers.py \
  --account principal="$HOME/.local/state/claude-principal" \
  --account reserva="$HOME/.local/state/claude-reserva"
```

El configurador escribe de forma atómica dos archivos locales:

- `~/.config/sincategorematico-bot/writers.json`, modo `0600`, que solo contiene
  identificadores y rutas.
- `~/.config/systemd/user/sincategorematico-engine.service.d/writer-accounts.conf`,
  modo `0644`, que concede escritura únicamente en los directorios escogidos y,
  si se configuró, en el archivo de bloqueo compartido exacto.

No ejecuta `systemctl`. Revisa ambos archivos y, cuando corresponda, aplica la
configuración manualmente:

```bash
systemctl --user daemon-reload
systemctl --user restart sincategorematico-engine.service
```

Se recomiendan cuentas dedicadas al motor. Si otro proceso necesita compartir una,
ambos deben respetar el mismo archivo `flock`. Su directorio padre debe existir,
pertenecer al usuario y tener modo `0700`; si el archivo aún no existe, el configurador
lo crea de forma exclusiva con modo `0600`:

```bash
python3 scripts/configure_writers.py \
  --account compartida="$HOME/.local/state/claude-compartida" \
  --account reserva="$HOME/.local/state/claude-reserva" \
  --shared-lock compartida="$HOME/.local/state/coordinacion/claude.lock"
```

El lock no se entrega a Claude ni aparece en el prompt. Si el otro consumidor no usa
ese mismo lock, configura una cuenta dedicada en vez de compartir el directorio.

## Instalación desde GitHub

Clona en una ruta estable del usuario; no hacen falta rutas particulares ni
editar los archivos de `deploy/`:

```bash
git clone https://github.com/SirHegel/sincategorematico-bot.git
cd sincategorematico-bot
python3 scripts/instalar_servicios.py --solo-renderizar --instalar-hook
```

El instalador sustituye `@PROJECT_ROOT@` y `@HOME@` en las plantillas y escribe
archivos normales, de forma atómica, en:

- `~/.config/systemd/user/`
- `~/.local/share/applications/`

No crea enlaces al clon. Instala unidades con modo `0644` y lanzadores con
`0755`, sin heredar permisos accidentales del clon. El modo
`--solo-renderizar` no recarga ni arranca servicios y sirve para inspeccionar el
resultado antes de activarlo.

Configura las piezas privadas en este orden:

```bash
# Entrada oculta; valida el token con Telegram y crea bot.env.
python3 scripts/configure_token.py

# Genera o conserva una clave local para http://127.0.0.1:8765.
python3 scripts/configure_dashboard.py

# Crea un /claim aleatorio que caduca en 24 horas.
python3 scripts/create_claim_code.py

# Opcional hasta que exista la aplicación OAuth de LinkedIn.
python3 scripts/configure_linkedin.py

# Renderiza de nuevo, recarga systemd y reinicia las tres unidades.
python3 scripts/instalar_servicios.py --instalar-hook
```

Después, abre el bot en Telegram y envía exactamente el `/claim …` mostrado en
la terminal. El código solo sirve una vez y su valor sin cifrar no se guarda.

El instalador devuelve un código distinto de cero si falla el renderizado,
`daemon-reload`, la habilitación, el reinicio o la comprobación de una unidad.
Antes de reiniciar fuerza siempre `publishing_paused=true` y `dry_run=true`, aun
si una base anterior estaba configurada para publicar en real.
Una actualización normal se aplica así:

```bash
git pull --ff-only
python3 scripts/instalar_servicios.py
```

## Vincular LinkedIn

Registra primero una URL de retorno idéntica a esta en la aplicación de
LinkedIn:

```text
http://localhost:8770/callback
```

Luego ejecuta:

```bash
# Perfil personal
python3 scripts/configure_linkedin.py

# Página de empresa; acepta el ID numérico o la URN completa
python3 scripts/configure_linkedin.py --organizacion ID_DE_LA_ORGANIZACION
```

La herramienta pausa el motor y activa simulación antes de abrir OAuth. Usa un
`state` aleatorio contra CSRF, escucha el retorno solo en
localhost, intercambia el código y almacena los tokens en el estado privado. Si
la aplicación no entrega un `refresh_token`, hay que repetir la vinculación
antes de que caduque el acceso. `/linkedin` muestra si el autor, la expiración y
los permisos siguen siendo utilizables.

El cliente usa por defecto la versión `202607` de la API REST. Se puede cambiar
sin modificar código agregando al archivo privado `bot.env` una versión válida
`YYYYMM`, por ejemplo:

```text
SINCATEGOREMATICO_LINKEDIN_API_VERSION=202607
```

Mantén ese valor en una versión admitida por LinkedIn y reinicia el motor después
de cambiarlo.

## Flujo de publicación

El arranque seguro combina dos controles distintos:

- **Pausa:** `/pause` detiene ingesta, redacción y publicación. `/resume` vuelve
  a habilitar el motor.
- **Destino:** `/publicacion simulacion` conserva cualquier aprobado en la cola y
  verifica el flujo sin enviar un POST. `/publicacion real` permite el envío solo
  si OAuth, autor, permisos, horario, separación y límite diario son válidos.

Secuencia recomendada para la primera puesta en marcha:

1. Vincula Telegram y LinkedIn, pero conserva simulación y pausa.
2. Usa `/resume`, revisa los borradores y aprueba uno.
3. Comprueba la simulación en `/status` y en el panel.
4. Activa `/publicacion real` únicamente cuando quieras enviar a LinkedIn.

Estados importantes de un borrador:

- `pending`: espera revisión.
- `approved`: aprobado y todavía no enviado.
- `publishing`: reserva durable tomada justo antes del envío.
- `published`: LinkedIn confirmó un identificador de publicación.
- `failed`: error definido, por ejemplo autorización rechazada.
- `uncertain`: LinkedIn pudo haber recibido el POST, pero no existe confirmación
  suficiente.

Mientras exista un `uncertain`, la publicación real completa queda bloqueada.
Después de revisar LinkedIn, usa `/confirmar N <URN_o_URL>` si el post sí existe:
lo contabiliza sin repetir el POST. Usa `/reintentar N` solo si comprobaste que
no existe. Los límites temporales (`429`) aplican una espera global a toda la
cola; los errores ambiguos nunca se repiten solos.

Telegram ofrece `/help`, `/status`, `/cola`, `/ver N`, `/tema …`, `/fuentes`,
`/agregar`, `/quitar`, `/limite`, `/franja`, `/aprobacion`, `/publicacion`,
`/linkedin`, `/pause`, `/resume`, `/reintentar N` y `/confirmar N <URN_o_URL>`.

## Paneles y operación

El panel web solo escucha en `http://127.0.0.1:8765`. Usa cookie `HttpOnly`,
validación de origen, límite de intentos y una política CSP restrictiva. La clave
del panel se muestra en la terminal al configurarlo y no aparece en el panel.

```bash
systemctl --user status sincategorematico-bot.service
systemctl --user status sincategorematico-engine.service
systemctl --user status sincategorematico-dashboard.service
journalctl --user -u sincategorematico-engine.service -f
systemctl --user restart sincategorematico-engine.service
```

Bloquear la pantalla no detiene las unidades. Suspender, hibernar o apagar sí.
Para mantener servicios de usuario después de cerrar sesión, un administrador
puede habilitar `linger` para esa cuenta.

## Evitar secretos en Git

`.gitignore` excluye formatos privados frecuentes, pero no sustituye una revisión.
El escáner incluido analiza contenido sin imprimir jamás la coincidencia:

```bash
# Archivos preparados para el próximo commit (es lo que ejecuta el hook)
tools/scan-secretos.sh --staged

# Todo archivo versionado o no ignorado del árbol actual
tools/scan-secretos.sh --todo

# Activación manual del hook en este clon, si no se usó --instalar-hook
git config core.hooksPath .githooks
```

Si detecta una credencial real, retírala del índice y rótala. Borrarla en un
commit posterior no la elimina de la historia ya publicada.

## Pruebas

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py src/sincategorematico_bot/*.py
node --check web/app.js
tools/scan-secretos.sh --todo
git diff --check
```
