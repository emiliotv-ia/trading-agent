# TradingAgent AI — Guía de instalación en Render

## Archivos en este repositorio

- `main.py` — el servidor del agente autónomo
- `requirements.txt` — dependencias de Python
- `Procfile` — instrucciones de arranque para Render

---

## Paso 1 — Crear el repositorio en GitHub

1. Andá a https://github.com y hacé clic en el botón verde **New**
2. Nombre del repositorio: `trading-agent`
3. Dejá todo por defecto y hacé clic en **Create repository**
4. En la página que aparece, hacé clic en **uploading an existing file**
5. Arrastrá los 4 archivos (`main.py`, `requirements.txt`, `Procfile`, `README.md`) a la ventana
6. Hacé clic en **Commit changes**

---

## Paso 2 — Crear el servidor en Render

1. Andá a https://render.com y hacé clic en **Get Started for Free**
2. Registrate con tu cuenta de GitHub
3. En el dashboard, hacé clic en **New +** → **Web Service**
4. Conectá tu repositorio `trading-agent`
5. Completá el formulario:
   - **Name:** trading-agent
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn main:app --workers 1 --threads 4 --timeout 120`
6. Plan: **Free**
7. Hacé clic en **Create Web Service**

Render va a tardar 2-3 minutos en instalar todo y arrancar.

---

## Paso 3 — Copiar tu URL

Cuando Render termine, te muestra una URL como:
```
https://trading-agent-xxxx.onrender.com
```

Copiá esa URL — la vas a necesitar para conectar el dashboard.

---

## Endpoints de la API

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/` | GET | Estado del servidor |
| `/state` | GET | Estado completo del agente |
| `/start` | POST | Iniciar el agente |
| `/stop` | POST | Detener el agente |
| `/config` | POST | Actualizar configuración |
| `/reset` | POST | Reiniciar portfolio |
| `/history` | GET | Historial de operaciones |

---

## Nota importante

El plan gratuito de Render "duerme" el servidor si no recibe tráfico por 15 minutos.
El dashboard lo mantiene activo automáticamente haciendo un ping cada 10 minutos.
