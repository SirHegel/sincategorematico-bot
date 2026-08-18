# Sincategoremático Bot

Servicio local y de bajo consumo para controlar por Telegram el futuro flujo de
noticias, redacción y publicación. La primera versión implementa conexión segura,
propietario único, estado y pausa global.

## Seguridad inicial

El token vive únicamente en
`~/.config/sincategorematico-bot/bot.env`, con permisos `0600`. Nunca se guarda
en este repositorio ni se imprime en los registros.

El código público y la configuración privada no se duplican en dos árboles que
puedan desincronizarse: este repositorio contiene solamente código auditable;
las credenciales y el estado de ejecución permanecen fuera de Git, en las rutas
privadas del usuario indicadas arriba y en `~/.local/state/sincategorematico-bot/`.

1. Revoca cualquier token que se haya compartido en mensajes.
2. Genera un token nuevo con BotFather.
3. Crea primero un código temporal para vincularte como propietario:

   ```bash
   cd /home/sirhegel/Documentos/Repos/sincategorematico-bot
   python3 scripts/create_claim_code.py
   ```

4. Configura el token mediante entrada oculta e inicia el servicio:

   ```bash
   python3 scripts/configure_token.py --activate
   ```

5. Abre `https://t.me/sincategorematicoln_bot`, pulsa **Start** y envía el
   comando `/claim` mostrado por el script.

## Operación

```bash
systemctl --user status sincategorematico-bot.service
journalctl --user -u sincategorematico-bot.service -f
systemctl --user restart sincategorematico-bot.service
```

Bloquear la pantalla no detiene el servicio. Suspender, hibernar o apagar el
equipo sí lo detiene. Para que arranque antes de iniciar sesión se debe habilitar
`linger` una sola vez.

## Pruebas

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
