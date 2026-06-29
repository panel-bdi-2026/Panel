# Notas operativas — trading-dashboard

Hechos confirmados por el usuario sobre el despliegue real. No inventar ni
asumir variantes de esto sin volver a confirmar.

## Idioma

Responder SIEMPRE en español, en todo mensaje de chat, sin excepción. El
usuario ya lo pidió varias veces.

## Acceso al droplet de producción

- Host: `100.92.236.44` (alcanzable solo por la red privada de Tailscale, no
  expuesto a internet público).
- Usuario SSH: **`root`** (confirmado 2026-06-22). `trading` NO es el usuario
  de login SSH: es el usuario de sistema sin privilegios que corre el backend
  y IB Gateway (ver `trading-dashboard/deploy/README.md`). No usar `trading@`
  para conectarse por SSH.
- Repo clonado en `/opt/panel` en el servidor.
- Servicio systemd del backend: `trading-dashboard`.

## Acceso remoto vía Claude Code (Remote Control)

- Hay una sesión persistente de Claude Code corriendo en el droplet, dentro
  de una sesión de `tmux` llamada `claude-remote`, con working directory
  `/opt/panel`.
- Se inició con `claude remote-control` (modo spawn: `same-dir`, las
  sesiones nuevas comparten `/opt/panel`). **Nunca** usar
  `--dangerously-skip-permissions` con `claude` en este droplet —
  confirmación obligatoria para cada acción, siempre.
- **Importante (confirmado 2026-06-29): NO existe una sesión que se llame
  literalmente "Panel" por defecto.** El nombre auto-generado de la sesión
  es `{hostname-del-droplet}-{palabras-random}` (ej.
  `panel-trading-v2-optimized-cray`, porque el hostname del droplet es
  `panel-trading-v2`). "Panel" es el nombre que el usuario le puso a mano
  (vía rename) a una sesión nueva — no busques una sesión con ese nombre
  asumiendo que aparecerá sola; si renombraron la sesión actual y se pierde,
  la próxima va a tener un nombre auto-generado distinto hasta que se
  renombre otra vez.
- **Forma confiable de conectarse a una sesión nueva o perdida: código QR /
  URL que muestra la terminal**, no buscar por nombre en la lista de
  sesiones:
  1. Conectarse a la sesión de tmux (`ssh root@100.92.236.44` y luego
     `tmux attach -t claude-remote`) y confirmar que `claude remote-control`
     esté corriendo y conectado (debe verse `✓ Connected · <nombre> · ...`
     y una línea `Capacity: X/32`).
  2. Apretar la barra espaciadora para mostrar el código QR en la terminal.
  3. Escanear el QR desde el celular (app de Claude), o copiar la URL
     `https://claude.ai/code?environment=env_...` que muestra la terminal y
     abrirla en el navegador con la cuenta `alejandrortega75@gmail.com`.
  4. La sesión nueva aparece con nombre auto-generado (ej. "prueba" si se
     escribió eso al conectar) — renombrarla a algo memorable si se quiere
     (ej. "Panel") una vez confirmado que responde.
  - Una vez creada y sincronizada así, también queda visible y usable desde
    `https://claude.ai/code` en el navegador y desde el resto de
    dispositivos con la misma cuenta — no hace falta repetir el QR por cada
    dispositivo.
- **Cómo distinguir una sesión sana de una "huérfana" (orphaned)**:
  - Sana: ícono de computadora con punto verde, responde a mensajes en
    segundos.
  - Huérfana/rota: ícono de nube ☁, se queda trabada en "Configurando un
    contenedor en la nube" indefinidamente, los mensajes quedan en cola sin
    procesarse, y el `claude remote-control` de la terminal muestra
    `Capacity: 0/32` (cero sesiones live conectadas a ese proceso). Esto
    pasa cuando el proceso original que registró esa sesión ya murió o se
    reinició — la sesión vieja queda apuntando a un proceso que ya no
    existe. **No se puede revivir una sesión huérfana**: hay que
    abandonarla y crear una nueva por QR/URL (ver arriba). Reintentar
    "resume" o reabrir la misma URL solo vuelve a mostrar la sesión rota.
  - Un ícono de triángulo naranja transitorio (unos 20-30 segundos,
    típicamente justo después de un rename o reconexión) es normal y no
    indica error — confirmar con un mensaje de prueba antes de asumir que
    algo falló.
- **Dos escenarios distintos de "se perdió la sesión", con fixes distintos**:
  - **Solo murió el proceso `claude remote-control`, pero el servidor de
    tmux sigue vivo** (`tmux ls` muestra la sesión `claude-remote` con su
    timestamp de creación original sin cambios; al hacer
    `tmux attach -t claude-remote` se ve un prompt de bash plano sin la TUI
    de Claude Code). Causa típica: el droplet estuvo sin red por ~10
    minutos o más y el proceso cerró solo. Fix — NO hace falta recrear
    tmux, solo relanzar el proceso dentro de la misma sesión:
    ```bash
    cd /opt/panel
    claude remote-control
    ```
    Esto reconecta al instante mostrando `✓ Connected · ...`. La sesión
    vieja en el cliente (si quedó huérfana) sigue sin servirse — toca crear
    una nueva por QR (ver arriba).
  - **El servidor de tmux completo ya no existe** (`tmux ls` da error "No
    such file or directory: /tmp/tmux-.../default"). Ahí sí hace falta
    recrear todo desde cero:
    ```bash
    tmux new -s claude-remote
    cd /opt/panel
    claude remote-control
    ```
    Responder `y` a "Enable Remote Control?" y `1` (same-dir) si pregunta el
    modo de spawn. Para dejarla corriendo basta cerrar la ventana de la
    terminal (tmux sobrevive a la desconexión SSH) — el atajo Ctrl+B+D
    resultó poco confiable en la práctica (una vez mató la sesión completa
    en vez de solo desconectarla), no depender de él.
- Formas de conectarse a la sesión ya establecida y sana (una vez conectada
  por primera vez vía QR/URL como se describe arriba):
  - App de Claude en el celular → pestaña "Code" → buscarla por el nombre
    que se le haya puesto (no asumir que se llama "Panel" si no se renombró
    así explícitamente).
  - `https://claude.ai/code` en cualquier navegador, misma cuenta.
  - Directo por SSH: `ssh root@100.92.236.44` y luego
    `tmux attach -t claude-remote`.
- Login OAuth hecho con la cuenta de Google `alejandrortega75@gmail.com`
  (la misma de claude.ai, con 2FA activado vía Google).
- Acceso SSH por llave configurado además de lo anterior: llave generada en
  la laptop (`laptop-panel`, ed25519) agregada a `~/.ssh/authorized_keys`
  del droplet. El sshd del droplet acepta **solo autenticación por llave
  pública** (`PasswordAuthentication no`), no hay fallback de password.
- Pendiente (diferido a propósito hasta estar más cerca de operar en vivo):
  crear un usuario Linux sin privilegios de root para correr Remote
  Control, en vez de usar `root` directamente.

## Pasos para desplegar un cambio ya pusheado a la rama

```bash
ssh root@100.92.236.44
cd /opt/panel
git fetch origin
git pull origin claude/investment-dashboard-trades-x2vkn8
cd trading-dashboard/backend
.venv/bin/pip install -r requirements.txt
cd /opt/panel
chown -R trading:trading /opt/panel
systemctl restart trading-dashboard
systemctl status trading-dashboard
```

El paso de `pip install` es obligatorio en cada deploy, no solo cuando "se
sabe" que cambió una dependencia: el 2026-06-22 un deploy sin este paso dejó
el servicio en crash-loop (`ModuleNotFoundError: No module named
'anthropic'`) porque se había agregado `anthropic` a `requirements.txt` en un
commit previo. `pip install -r requirements.txt` no hace nada (rápido) si no
hay paquetes nuevos, así que no tiene costo correrlo siempre.

Si no levanta: `journalctl -u trading-dashboard -n 50 --no-pager`.

Después de reiniciar, hacer hard refresh en el navegador (`Ctrl+Shift+R`)
para descartar el `index.html` cacheado.

## Rama de trabajo

Todo el desarrollo de este proyecto va en `claude/investment-dashboard-trades-x2vkn8`.
Nunca pushear a otra rama sin permiso explícito.
