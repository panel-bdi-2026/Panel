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
- Formas de conectarse a esa sesión:
  - App de Claude en el celular → pestaña "Code" → sesión "Panel"
    (cuenta `alejandrortega75@gmail.com`).
  - `https://claude.ai/code` en cualquier navegador, misma cuenta.
  - Directo por SSH: `ssh root@100.92.236.44` y luego
    `tmux attach -t claude-remote`.
- Login OAuth hecho con la cuenta de Google `alejandrortega75@gmail.com`
  (la misma de claude.ai, con 2FA activado vía Google).
- Si la sesión de tmux se pierde (verificar con `tmux ls`; si da error
  "No such file or directory" no hay servidor de tmux corriendo), recrearla:
  ```bash
  tmux new -s claude-remote
  cd /opt/panel
  claude remote-control
  ```
  Responder `y` a "Enable Remote Control?" y `1` (same-dir) si pregunta el
  modo de spawn. Para dejarla corriendo basta cerrar la ventana de la
  terminal (tmux sobrevive a la desconexión SSH) — el atajo Ctrl+B+D resultó
  poco confiable en la práctica (una vez mató la sesión completa en vez de
  solo desconectarla), no depender de él.
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
