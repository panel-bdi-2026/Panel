# Notas operativas — trading-dashboard

Hechos confirmados por el usuario sobre el despliegue real. No inventar ni
asumir variantes de esto sin volver a confirmar.

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
chown -R trading:trading /opt/panel
systemctl restart trading-dashboard
systemctl status trading-dashboard
```

Si no levanta: `journalctl -u trading-dashboard -n 50 --no-pager`.

Después de reiniciar, hacer hard refresh en el navegador (`Ctrl+Shift+R`)
para descartar el `index.html` cacheado.

## Rama de trabajo

Todo el desarrollo de este proyecto va en `claude/investment-dashboard-trades-x2vkn8`.
Nunca pushear a otra rama sin permiso explícito.
