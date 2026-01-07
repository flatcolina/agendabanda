# AgendaBandas (Monorepo)

Este repositório contém:
- **Backend (Railway)**: FastAPI simples (rota `/health`) na raiz do projeto.
- **Frontend (Netlify)**: React + Vite + Tailwind em `frontend/`.

> **Versão atual (MVP):** cadastro de **Bandas** e **Agenda de Eventos** (sem Google Maps / logística).

## Backend (Railway)
- O Railway usa o `Dockerfile` da raiz.
- Variáveis (opcional):
  - `ALLOWED_ORIGINS` (ex: `https://seusite.netlify.app,http://localhost:5173`)

Teste:
- `GET /health` → `{ "ok": true }`

## Frontend (Netlify)
No Netlify:
- **Base directory:** `frontend`
- **Build command:** `npm run build`
- **Publish directory:** `dist`

Variáveis no Netlify:
- `VITE_FIREBASE_API_KEY`
- `VITE_FIREBASE_AUTH_DOMAIN`
- `VITE_FIREBASE_PROJECT_ID`
- `VITE_FIREBASE_STORAGE_BUCKET`
- `VITE_FIREBASE_MESSAGING_SENDER_ID`
- `VITE_FIREBASE_APP_ID`

> O frontend acessa o Firestore diretamente via Firebase Web SDK.

