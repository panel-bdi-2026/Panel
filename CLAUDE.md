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
