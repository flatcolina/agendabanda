# Bandas Agenda Pro (Firestore + Google Maps) — Monorepo (Frontend Netlify + Backend Railway)

Este projeto é um sistema **multi-bandas** (multi-org) para gerenciamento de agenda de shows com:
- Cadastro de **Organizações**, **Bandas**, **Locais (venues)** e **Eventos**
- **Logística automática**: distância/tempo entre shows no mesmo dia (Google Maps)
- Frontend: **React + Vite + Tailwind**
- Backend: **FastAPI** (Railway) com verificação de token Firebase

## Estrutura
- `frontend/` → painel web (Netlify)
- `backend/` → API (Railway)

## Deploy rápido (resumo)
1) Configure Firebase (Auth + Firestore) e copie o config para o `.env` do frontend.
2) Crie uma Service Account do Firebase e configure no Railway (`FIREBASE_SERVICE_ACCOUNT_JSON`).
3) Crie uma Google Maps API key e configure no Railway (`GOOGLE_MAPS_API_KEY`).
4) Suba `frontend/` no Netlify e `backend/` no Railway.

Veja o passo a passo completo em `docs/PASSO_A_PASSO.md`.
