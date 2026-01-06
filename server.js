import 'dotenv/config'
import crypto from 'crypto'
import express from 'express'
import helmet from 'helmet'
import rateLimit from 'express-rate-limit'
import { requireAuth, getFirestore, admin } from './auth.js'
import { readConsultasFromSheets, readReservasFromSheets } from './sheets.js'

const app = express()

// Estamos atrás de proxies (Netlify / Railway). Necessário para express-rate-limit
// identificar o IP corretamente quando existe X-Forwarded-For.
app.set('trust proxy', 1)

 

app.use(express.json({ limit: '1mb' }))

app.use(helmet({
  crossOriginResourcePolicy: { policy: 'cross-origin' },
}))

// CORS
// - Por padrão, NÃO usamos cookies/credenciais (mais simples e evita erro com '*').
// - Se você realmente precisar, setar CORS_CREDENTIALS=true e listar origins exatos em ALLOWED_ORIGINS.
const CORS_CREDENTIALS = String(process.env.CORS_CREDENTIALS || 'false').toLowerCase() === 'true'

const normalizeOrigin = (s) => String(s || '')
  .trim()
  .replace(/^['"]+|['"]+$/g, '') // remove aspas acidentais
  .replace(/\/+$/g, '')

const parseAllowedOrigins = (raw) => {
  return String(raw || '')
    .replace(/\r/g, '')
    .split(/[,;\n]+/g) // aceita vírgula, ponto-e-vírgula e quebra de linha
    .map(s => normalizeOrigin(s))
    .filter(Boolean)
}

const allowedOrigins = parseAllowedOrigins(process.env.ALLOWED_ORIGINS)

// Suporta:
// - lista exata: https://meu-site.netlify.app
// - wildcard por domínio: *.netlify.app  (ou .netlify.app)
// - coringa total: *
const isOriginAllowed = (origin) => {
  const o = normalizeOrigin(origin)
  if (!o) return true // server-to-server ou sem Origin
  if (allowedOrigins.length === 0) return true
  if (allowedOrigins.includes('*')) return true

  let host = ''
  try { host = new URL(o).hostname } catch { host = '' }

  for (const entry of allowedOrigins) {
    if (!entry) continue
    if (entry.startsWith('*.')) {
      const suffix = entry.slice(1) // ".netlify.app"
      if (host && host.endsWith(suffix)) return true
      continue
    }
    if (entry.startsWith('.')) {
      if (host && host.endsWith(entry)) return true
      continue
    }
    if (o === entry) return true
  }
  return false
}


// CORS headers (middleware próprio) para garantir preflight (OPTIONS) sempre OK.
// Isso evita casos onde o preflight cai em 404/500 sem headers e o navegador bloqueia.
app.use((req, res, next) => {
  const origin = req.header('Origin')
  const ok = isOriginAllowed(origin)

  if (ok) {
    const normalized = origin ? normalizeOrigin(origin) : '*'
    const allowOrigin = (allowedOrigins.includes('*') && !CORS_CREDENTIALS) ? '*' : (normalized || '*')

    res.setHeader('Access-Control-Allow-Origin', allowOrigin)
    res.setHeader('Vary', 'Origin')
    res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,PATCH,DELETE,OPTIONS')

    const reqHeaders = req.header('Access-Control-Request-Headers')
    res.setHeader('Access-Control-Allow-Headers', reqHeaders || 'Content-Type, Authorization')

    res.setHeader('Access-Control-Max-Age', '600')
    if (CORS_CREDENTIALS) {
      res.setHeader('Access-Control-Allow-Credentials', 'true')
    }
  }

  if (req.method === 'OPTIONS') {
    return res.status(204).end()
  }
  return next()
})


app.use(rateLimit({
  windowMs: 60 * 1000,
  max: 120,
  standardHeaders: true,
  legacyHeaders: false,
}))

app.get('/health', (_, res) => res.json({ ok: true }))

const COLLECTION_ENVIADOS = process.env.SENT_COLLECTION || 'consultas_enviadas'

// Config do template do WhatsApp (salvo no Firestore via backend, sem mexer na planilha)
const CONFIG_COLLECTION = process.env.CONFIG_COLLECTION || 'app_config'
const WHATSAPP_TEMPLATE_DOC = process.env.WHATSAPP_TEMPLATE_DOC || 'whatsapp_template'

// Template padrão (editável na página "Mensagem WhatsApp")
// Variáveis disponíveis: {NOME}, {TELEFONE}, {CHECKIN}, {CHECKOUT}, {HOSPEDES}, {DATA_CONSULTA}, {APARTAMENTOS}
const DEFAULT_WHATSAPP_TEMPLATE = [
  'Olá {NOME} 👋',
  '',
  'Segue o resumo da sua consulta:',
  '',
  '🧾 Consulta: {DATA_CONSULTA}',
  '📅 Check-in: {CHECKIN}',
  '📅 Check-out: {CHECKOUT}',
  '👥 Hóspedes: {HOSPEDES}',
  '',
  '🏠 Apartamentos encontrados:',
  '{APARTAMENTOS}',
].join('\n')


// ===== Telegram Mini App (WebApp) auth =====
// O Mini App não usa Firebase Auth. Em vez disso, valida o initData assinado pelo Telegram.
// Referência: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || ''
const TELEGRAM_ALLOWED_IDS = new Set(
  String(process.env.TELEGRAM_ALLOWED_IDS || '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean)
)
const TELEGRAM_ALLOWLIST_REQUIRED = String(process.env.TELEGRAM_ALLOWLIST_REQUIRED || 'true').toLowerCase() === 'true'
const TELEGRAM_ALLOWLIST_COLLECTION = process.env.TELEGRAM_ALLOWLIST_COLLECTION || 'telegram_allowlist'

function parseInitData(initDataRaw) {
  const params = new URLSearchParams(String(initDataRaw || '').trim())
  const obj = {}
  for (const [k, v] of params.entries()) obj[k] = v
  return obj
}

function computeTelegramHash(dataObj, botToken) {
  const { hash, ...rest } = dataObj
  const pairs = Object.keys(rest)
    .sort()
    .map(k => `${k}=${rest[k]}`)
  const dataCheckString = pairs.join('\n')

  const secretKey = crypto.createHash('sha256').update(botToken).digest()
  return crypto.createHmac('sha256', secretKey).update(dataCheckString).digest('hex')
}

function verifyTelegramInitData(initDataRaw) {
  if (!initDataRaw) return { ok: false, error: 'Missing initData' }
  if (!TELEGRAM_BOT_TOKEN) return { ok: false, error: 'Missing TELEGRAM_BOT_TOKEN' }

  const dataObj = parseInitData(initDataRaw)
  const theirHash = String(dataObj.hash || '')
  if (!theirHash) return { ok: false, error: 'Missing hash' }

  const ourHash = computeTelegramHash(dataObj, TELEGRAM_BOT_TOKEN)
  if (ourHash.length !== theirHash.length) return { ok: false, error: 'Invalid initData hash' }
  const ok = crypto.timingSafeEqual(Buffer.from(ourHash), Buffer.from(theirHash))
  if (!ok) return { ok: false, error: 'Invalid initData hash' }

  // user vem em JSON dentro de "user"
  let user = null
  try {
    user = dataObj.user ? JSON.parse(dataObj.user) : null
  } catch {
    user = null
  }
  const userId = user?.id ? String(user.id) : null
  return { ok: true, user, userId, dataObj }
}

async function requireTelegramMiniApp(req, res, next) {
  try {
    const initDataRaw =
      req.body?.initData ||
      req.headers['x-telegram-init-data'] ||
      req.query?.initData

    const v = verifyTelegramInitData(initDataRaw)
    if (!v.ok) return res.status(401).json({ error: v.error })

    // allowlist por env (mais simples)
    if (TELEGRAM_ALLOWED_IDS.size > 0) {
      if (!v.userId || !TELEGRAM_ALLOWED_IDS.has(String(v.userId))) {
        return res.status(403).json({ error: 'Sem acesso (allowlist)' })
      }
    } else if (TELEGRAM_ALLOWLIST_REQUIRED) {
      // allowlist por Firestore: telegram_allowlist/{userId} {allowed:true}
      const db = getFirestore()
      const doc = await db.collection(TELEGRAM_ALLOWLIST_COLLECTION).doc(String(v.userId)).get()
      if (!doc.exists || doc.data()?.allowed !== true) {
        return res.status(403).json({ error: 'Sem acesso (allowlist)' })
      }
    }

    req.telegramUser = {
      id: v.userId,
      username: v.user?.username || null,
      first_name: v.user?.first_name || null,
      last_name: v.user?.last_name || null,
    }
    req.telegramInitData = String(initDataRaw || '')
    return next()
  } catch (err) {
    const msg = err?.message || String(err)
    console.error('[miniapp auth] error:', err?.stack || msg)
    return res.status(401).json({ error: 'Miniapp auth failed' })
  }
}
// cache simples em memória
let cache = { at: 0, data: null }
const cacheSeconds = Math.max(0, Number(process.env.CACHE_SECONDS || '10') || 10)

async function annotateEnviados(consultas) {
  const db = getFirestore()
  const col = db.collection(COLLECTION_ENVIADOS)

  // getAll aceita varargs; vamos em chunks para evitar limites.
  const out = consultas.map(c => ({ ...c, enviado: false, enviadoEm: null, enviadoPor: null }))
  const refs = out.map(c => col.doc(c.id))
  const chunkSize = 200
  for (let i = 0; i < refs.length; i += chunkSize) {
    const chunk = refs.slice(i, i + chunkSize)
    const snaps = await db.getAll(...chunk)
    for (let j = 0; j < snaps.length; j++) {
      const snap = snaps[j]
      if (!snap.exists) continue
      const idx = i + j
      const data = snap.data() || {}
      const ts = data.sentAt?.toDate ? data.sentAt.toDate() : null
      out[idx].enviado = true
      out[idx].enviadoEm = ts ? ts.getTime() : null
      out[idx].enviadoPor = data.sentByEmail || data.sentByUid || null
    }
  }
  return out
}

app.get('/api/reservas', requireAuth, async (req, res) => {
  try {
    const now = Date.now()
    if (cache.data && (now - cache.at) < cacheSeconds * 1000) {
      return res.json(cache.data)
    }

    // Default: retorna consultas agrupadas (um card por consulta)
    // ?raw=1 retorna as linhas originais da planilha
    const raw = String(req.query.raw || '').trim() === '1'
    const data = raw ? await readReservasFromSheets() : await readConsultasFromSheets()

    const payload = raw ? data : await annotateEnviados(data)

    cache = { at: now, data: payload }
    return res.json(payload)
  } catch (err) {
    const msg = err?.message || String(err)
    console.error('[api/reservas] error:', err?.stack || msg)
    return res.status(500).json({ error: msg })
  }
})

// Template WhatsApp (GET/PUT)

async function getCurrentWhatsAppTemplate() {
  const db = getFirestore()
  const ref = db.collection(CONFIG_COLLECTION).doc(WHATSAPP_TEMPLATE_DOC)
  const snap = await ref.get()
  if (!snap.exists) return DEFAULT_WHATSAPP_TEMPLATE
  const data = snap.data() || {}
  const t = String(data.template || '').trim()
  return t || DEFAULT_WHATSAPP_TEMPLATE
}

// ===== Mini App endpoints (Telegram) =====
// Front chama /admin/api/miniapp/reservas enviando {initData}
app.post('/api/miniapp/reservas', requireTelegramMiniApp, async (req, res) => {
  try {
    const now = Date.now()

    // cache independente do Firebase Auth
    if (cache.data && (now - cache.at) < cacheSeconds * 1000) {
      const template = await getCurrentWhatsAppTemplate()
      return res.json({ reservas: cache.data, template })
    }

    const data = await readConsultasFromSheets()
    const payload = await annotateEnviados(data)

    cache = { at: now, data: payload }

    const template = await getCurrentWhatsAppTemplate()
    return res.json({ reservas: payload, template })
  } catch (err) {
    const msg = err?.message || String(err)
    console.error('[api/miniapp/reservas] error:', err?.stack || msg)
    return res.status(500).json({ error: msg })
  }
})

app.post('/api/miniapp/consultas/:id/enviado', requireTelegramMiniApp, async (req, res) => {
  try {
    const id = String(req.params.id || '').trim()
    if (!id) return res.status(400).json({ error: 'Missing id' })

    const db = getFirestore()
    const doc = db.collection(COLLECTION_ENVIADOS).doc(id)

    await doc.set({
      sentAt: admin.firestore.FieldValue.serverTimestamp(),
      sentByTelegramId: req.telegramUser?.id || null,
      sentByTelegramUsername: req.telegramUser?.username || null,
      source: 'telegram',
    }, { merge: true })

    cache = { at: 0, data: null }
    return res.json({ ok: true })
  } catch (err) {
    const msg = err?.message || String(err)
    console.error('[api/miniapp/enviado] error:', err?.stack || msg)
    return res.status(500).json({ error: msg })
  }
})

app.delete('/api/miniapp/consultas/:id/enviado', requireTelegramMiniApp, async (req, res) => {
  try {
    const id = String(req.params.id || '').trim()
    if (!id) return res.status(400).json({ error: 'Missing id' })

    const db = getFirestore()
    await db.collection(COLLECTION_ENVIADOS).doc(id).delete()

    cache = { at: 0, data: null }
    return res.json({ ok: true })
  } catch (err) {
    const msg = err?.message || String(err)
    console.error('[api/miniapp/enviado delete] error:', err?.stack || msg)
    return res.status(500).json({ error: msg })
  }
})

app.get('/api/whatsapp-template', requireAuth, async (req, res) => {
  try {
    const db = getFirestore()
    const ref = db.collection(CONFIG_COLLECTION).doc(WHATSAPP_TEMPLATE_DOC)
    const snap = await ref.get()

    if (!snap.exists) {
      return res.json({
        template: DEFAULT_WHATSAPP_TEMPLATE,
        updatedAt: null,
        updatedBy: null,
        isDefault: true,
      })
    }

    const data = snap.data() || {}
    const template = String(data.template || '').trim() || DEFAULT_WHATSAPP_TEMPLATE
    const updatedAt = data.updatedAt?.toDate ? data.updatedAt.toDate().getTime() : null
    const updatedBy = data.updatedByEmail || data.updatedByUid || null

    return res.json({ template, updatedAt, updatedBy, isDefault: !String(data.template || '').trim() })
  } catch (err) {
    const msg = err?.message || String(err)
    console.error('[api/whatsapp-template] error:', err?.stack || msg)
    return res.status(500).json({ error: msg })
  }
})

app.put('/api/whatsapp-template', requireAuth, async (req, res) => {
  try {
    const template = String(req.body?.template || '')
    if (!template.trim()) {
      return res.status(400).json({ error: 'Template vazio' })
    }
    if (template.length > 8000) {
      return res.status(400).json({ error: 'Template muito grande (limite 8000 caracteres)' })
    }

    const db = getFirestore()
    const ref = db.collection(CONFIG_COLLECTION).doc(WHATSAPP_TEMPLATE_DOC)
    await ref.set({
      template,
      updatedAt: admin.firestore.FieldValue.serverTimestamp(),
      updatedByUid: req.user?.uid || null,
      updatedByEmail: req.user?.email || null,
    }, { merge: true })

    return res.json({ ok: true })
  } catch (err) {
    const msg = err?.message || String(err)
    console.error('[api/whatsapp-template put] error:', err?.stack || msg)
    return res.status(500).json({ error: msg })
  }
})

app.post('/api/consultas/:id/enviado', requireAuth, async (req, res) => {
  try {
    const id = String(req.params.id || '').trim()
    if (!id) return res.status(400).json({ error: 'Missing id' })

    const db = getFirestore()
    const doc = db.collection(COLLECTION_ENVIADOS).doc(id)

    await doc.set({
      sentAt: admin.firestore.FieldValue.serverTimestamp(),
      sentByUid: req.user?.uid || null,
      sentByEmail: req.user?.email || null,
      sentByName: req.user?.name || null,
    }, { merge: true })

    // invalida cache
    cache = { at: 0, data: null }
    return res.json({ ok: true })
  } catch (err) {
    const msg = err?.message || String(err)
    console.error('[api/enviado] error:', err?.stack || msg)
    return res.status(500).json({ error: msg })
  }
})

app.delete('/api/consultas/:id/enviado', requireAuth, async (req, res) => {
  try {
    const id = String(req.params.id || '').trim()
    if (!id) return res.status(400).json({ error: 'Missing id' })

    const db = getFirestore()
    await db.collection(COLLECTION_ENVIADOS).doc(id).delete()

    cache = { at: 0, data: null }
    return res.json({ ok: true })
  } catch (err) {
    const msg = err?.message || String(err)
    console.error('[api/enviado delete] error:', err?.stack || msg)
    return res.status(500).json({ error: msg })
  }
})

const port = Number(process.env.PORT || 8080)
app.listen(port, () => {
  console.log(`[backend] listening on :${port}`)
})
