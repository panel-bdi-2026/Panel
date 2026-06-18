# Despliegue 24/7 en la nube (sin depender de la laptop)

Esta carpeta tiene los artefactos para correr el backend y el login a IBKR
de forma headless en un servidor que administrás vos, en vez de depender de
que la laptop quede encendida. Arquitectura recomendada:

```
Tu celular/laptop  --(Tailscale, red privada)-->  VM en la nube
                                                     +-- IB Gateway (via IBC, login automatico)
                                                     +-- backend del dashboard (systemd)
```

Ningún puerto queda expuesto a internet: el dashboard solo es alcanzable
desde tus propios dispositivos conectados a tu red privada de Tailscale.

## Por qué esta combinación

- **Servidor: Oracle Cloud "Always Free"** — instancias ARM Ampere A1 (hasta
  4 OCPU / 24 GB RAM) gratis para siempre, no es un trial. Si tu cuenta/región
  no tiene capacidad ARM disponible (problema conocido de Oracle, probá otra
  región primero), un VPS chico de Hetzner o DigitalOcean (~USD 4-6/mes) sirve
  igual — los pasos de abajo no cambian, salvo la nota sobre ARM del Paso 3.
- **[IBC](https://github.com/IbcAlpha/IBC)** — automatiza el login a IB
  Gateway (contesta los diálogos de Java por vos) y permite que corra toda la
  semana con un solo login, reiniciándose solo después de la 1:00 AM ET del
  domingo (el reinicio forzado semanal de IBKR). **Para que no genere falsas
  expectativas: esto no es 100% desatendido.** IBKR sigue pidiendo confirmar
  ese login semanal con un push de la app IBKR Mobile en tu teléfono. El resto
  de la semana no hace falta tocar nada, pero una vez por semana vas a
  necesitar el teléfono a mano.
- **[Tailscale](https://tailscale.com)** — red privada (WireGuard) gratis para
  uso personal. Accedés al dashboard como si estuvieras en la misma LAN que el
  servidor, sin abrir ningún puerto público.

## Paso 1 — Crear la VM

1. Lanzá una instancia "Always Free" en Oracle Cloud: forma
   `VM.Standard.A1.Flex` (ARM Ampere), Ubuntu 22.04/24.04, 2-4 OCPU y 12-24 GB
   RAM (sobra para esto).
2. En el Security List/NSG de la VM dejá **solo el puerto 22 (SSH)** abierto,
   idealmente restringido a tu IP. No abras el 8000 ni ningún otro: Tailscale
   no necesita puertos entrantes abiertos a internet, conecta saliente.
3. Conectate por SSH con el usuario por defecto de la imagen (`ubuntu` en
   Oracle).

> **"Out of capacity for shape VM.Standard.A1.Flex"**: es el error más común
> al crear la VM gratis — Oracle no tiene Ampere libre en ese AD en ese
> momento. No es un error tuyo. Probá pedir **menos recursos** (1 OCPU / 6 GB
> entra mucho más fácil que 4 OCPU / 24 GB) y reintentá: la capacidad se
> libera y se ocupa constantemente. Para no estar dándole a "Create" a mano
> (y para no chocar con el rate limit *"Too many requests"*), usá el script
> [`scripts/oci_launch_retry.sh`](scripts/oci_launch_retry.sh): reintenta solo
> con backoff hasta que entra una instancia. Necesita la [OCI CLI](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm)
> configurada (`oci setup config`); copiá `scripts/oci_launch.env.example` a
> `scripts/oci_launch.env`, completá los OCID de tu tenancy (el ejemplo trae
> los comandos `oci ...` para obtener cada uno) y corré
> `bash scripts/oci_launch_retry.sh`. Tu `oci_launch.env` no se versiona
> (está en `.gitignore`).

A partir de aquí, las instrucciones asumen que este repo se clona en
`/opt/panel` en el servidor (ajustá las rutas si usás otra).

## Paso 2 — Preparar el sistema

Copiá este repo al servidor (`git clone` o `scp`) en `/opt/panel` y corré el
script de setup como root:

```bash
sudo bash /opt/panel/trading-dashboard/deploy/setup_oracle_vm.sh
```

Podés abrirlo y leerlo antes de correrlo, es corto. Qué hace:

- Instala dependencias: Python, `Xvfb` (pantalla virtual para IB Gateway),
  las librerías de X11 que el AWT de Java necesita en tiempo de ejecución
  (`libxtst6`, `libxrender1`, `libxi6` — sin ellas IBC falla con
  `UnsatisfiedLinkError` y exit code 1100), `ufw`, `curl`, `unzip`, `git`.
- Crea un usuario de sistema sin privilegios (`trading`) para correr todo —
  ni el backend ni IB Gateway corren como root.
- Configura `ufw` para bloquear todo el tráfico entrante salvo SSH y, una vez
  que Tailscale esté activo, el puerto 8000 *solo* a través de la interfaz
  `tailscale0` (nunca a la interfaz pública).
- Instala Tailscale (el `tailscale up` inicial lo corrés vos a mano, porque
  pide autenticarte en el navegador).

Después de correrlo:

```bash
sudo tailscale up
# segui el link que imprime para autenticarte con tu cuenta de Tailscale
```

## Paso 3 — Instalar IB Gateway + IBC

La descarga de IB Gateway no se puede scriptear (hay que aceptar los
términos de IBKR a mano), así que este paso es manual:

1. Descargá **IB Gateway** desde la página oficial de IBKR (TWS/Gateway →
   instalador Standalone/Offline). **Si tu VM es ARM** (como las Ampere de
   Oracle), usá el instalador "Standalone" basado en `.jar` (corre sobre
   cualquier JVM, incluida una ARM) en vez del `.sh` que trae una JVM
   embebida para x86_64 — instalá antes un JDK con
   `sudo apt install default-jdk`.
2. Descargá la última release de IBC para Linux desde
   <https://github.com/IbcAlpha/IBC/releases/latest> y descomprimila en
   `/opt/ibc`:
   ```bash
   sudo unzip IBCLinux-*.zip -d /opt/ibc
   cd /opt/ibc && sudo chmod +x *.sh
   sudo chown -R trading:trading /opt/ibc
   ```
3. Copiá [`ibc/config.ini.example`](ibc/config.ini.example) a
   `/opt/ibc/config.ini` y completá `IbLoginId`/`IbPassword` con tus
   credenciales reales y `TradingMode=paper` (o `live` cuando corresponda).
   **Esa copia con credenciales reales vive solo en el servidor — nunca la
   subas a git ni la pongas dentro de este repo.**
4. Abrí `/opt/ibc/gatewaystart.sh` con un editor y apuntá la variable de
   configuración (el archivo trae comentarios que indican cuál) a
   `/opt/ibc/config.ini`. Los nombres exactos de variables pueden variar
   entre versiones de IBC — confirmá contra el `userguide.md`/PDF que viene
   dentro del zip que descargaste. Esta guía cubre la arquitectura general,
   no el manual línea por línea de IBC.

## Paso 4 — IB Gateway como servicio (systemd + Xvfb)

IB Gateway es una app Java con interfaz gráfica; `Xvfb` le da una "pantalla"
virtual sin necesitar monitor ni VNC. El wrapper
[`scripts/start_ibgateway.sh`](scripts/start_ibgateway.sh) arranca `Xvfb` y
después `gatewaystart.sh`.

```bash
sudo cp /opt/panel/trading-dashboard/deploy/systemd/ibgateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ibgateway
sudo journalctl -u ibgateway -f   # para ver el login en vivo la primera vez
```

La primera vez vas a necesitar aprobar el login desde la app IBKR Mobile en
tu teléfono (ver la nota de 2FA más arriba). Después de eso, el servicio
reintenta solo si se cae (`Restart=always`).

## Paso 5 — Backend del dashboard como servicio

```bash
cd /opt/panel/trading-dashboard/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# editá .env: IB_HOST=127.0.0.1, IB_PORT=4002 (IB Gateway paper), API_KEY=algo-fuerte
sudo cp /opt/panel/trading-dashboard/deploy/systemd/trading-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trading-dashboard
```

## Paso 6 — Acceder desde tu celular/laptop

Instalá Tailscale también en tu celular/laptop (mismo login de Tailscale que
el servidor) y abrí en el navegador:

```
http://<IP-o-nombre-tailscale-del-servidor>:8000
```

Tailscale te muestra esa IP/nombre en su app, o corriendo
`tailscale status` en el servidor.

## Costo

- Oracle Cloud Always Free: USD 0/mes (siempre, no es un trial).
- Tailscale: gratis para uso personal (hasta 100 dispositivos).
- Fallback si Oracle no tiene capacidad ARM: Hetzner CX22 o un Droplet básico
  de DigitalOcean, ~USD 4-6/mes.

## Checklist de seguridad del servidor

- [ ] Solo el puerto 22 abierto a internet (idealmente restringido a tu IP).
- [ ] El backend y IB Gateway corren como usuario sin privilegios
      (`trading`), nunca como root.
- [ ] El puerto 8000 del backend NO está abierto en `ufw` para la interfaz
      pública, solo para `tailscale0`.
- [ ] SSH con clave, no con contraseña.
- [ ] `TRADING_MODE=paper` confirmado en `.env` hasta terminar las pruebas.
