import { google } from 'googleapis'
import crypto from 'node:crypto'

function loadJsonEnv(name) {
  const raw = process.env[name]
  if (!raw) throw new Error(`Variável de ambiente ausente: ${name}`)
  try {
    return JSON.parse(raw)
  } catch (e) {
    throw new Error(`JSON inválido em ${name}: ${e?.message || e}`)
  }
}

function loadSheetsCreds() {
  // Preferência: credencial dedicada para Sheets.
  if (process.env.GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON) {
    return loadJsonEnv('GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON')
  }

  // Fallback: usa o mesmo service account do Firebase Admin.
  if (process.env.FIREBASE_SERVICE_ACCOUNT_JSON) {
    return loadJsonEnv('FIREBASE_SERVICE_ACCOUNT_JSON')
  }

  throw new Error(
    'Variável de ambiente ausente: GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON (ou FIREBASE_SERVICE_ACCOUNT_JSON)'
  )
}

function normalizeHeader(s) {
  return String(s || '')
    .trim()
    .toLowerCase()
    .normalize('NFD').replace(/\p{Diacritic}/gu, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
}

function guessKey(h) {
  const n = normalizeHeader(h)

  const has = (...parts) => parts.every(p => n.includes(p))

  // Campos principais
  if (n === 'nome' || has('nome')) return 'nome'

  // Telefone pode vir separado em DDI + Telefone
  if (n === 'ddi' || has('ddi') || has('codigo', 'pais') || has('pais')) return 'ddi'
  if (n === 'telefone' || n === 'celular' || has('telefone') || has('celular') || has('whatsapp')) return 'telefone'

  // Check-in / Check-out
  // Aceita "Check-in", "Chk-In", "Entrada", etc.
  if (n === 'checkin' || has('check', 'in') || has('chk', 'in') || has('entrada') || has('check-in') || has('chk-in')) return 'checkin'
  if (n === 'checkout' || has('check', 'out') || has('chk', 'out') || has('saida') || has('check-out') || has('chk-out')) return 'checkout'

  // Unidade / apartamento
  if (n === 'flat' || has('flat') || has('unidade') || has('apartamento') || has('imovel')) return 'flat'

  // Valor
  if (n === 'valor' || has('valor') || has('total') || has('preco') || has('diaria')) return 'valor'

  // Campos opcionais (se existirem na planilha)
  if (n === 'qnt' || n === 'quant' || has('qnt') || has('qtd') || has('quantidade')) return 'qnt'
  if (n === 'realizada' || has('realizada') || has('data', 'realizada')) return 'realizada'
  if (n === 'hora' || has('hora')) return 'hora'

  return null
}

function formatDateString(v) {
  const s = String(v || '').trim()
  if (!s) return ''

  // yyyy-mm-dd -> dd/mm/yyyy
  const m1 = s.match(/^([0-9]{4})-([0-9]{2})-([0-9]{2})$/)
  if (m1) return `${m1[3]}/${m1[2]}/${m1[1]}`

  // dd/mm/yyyy
  const m2 = s.match(/^([0-9]{2})\/([0-9]{2})\/([0-9]{4})$/)
  if (m2) return s

  return s
}

function parseAnyDateToUTC(v) {
  const s = String(v || '').trim()
  if (!s) return null

  // yyyy-mm-dd
  let m = s.match(/^([0-9]{4})-([0-9]{2})-([0-9]{2})$/)
  if (m) return new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])))

  // dd/mm/yyyy or dd-mm-yyyy
  m = s.match(/^([0-9]{2})[\/\-]([0-9]{2})[\/\-]([0-9]{4})$/)
  if (m) return new Date(Date.UTC(Number(m[3]), Number(m[2]) - 1, Number(m[1])))

  // fallback: Date.parse
  const t = Date.parse(s)
  if (!Number.isNaN(t)) return new Date(t)
  return null
}

function parseConsultaDateTimeToTs(realizada, hora) {
  const d = parseAnyDateToUTC(realizada)
  if (!d) return null
  const h = String(hora || '').trim()
  if (!h) return d.getTime()

  const m = h.match(/^([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?$/)
  if (!m) return d.getTime()
  const hh = Math.min(23, Math.max(0, Number(m[1])))
  const mm = Math.min(59, Math.max(0, Number(m[2])))
  const ss = Math.min(59, Math.max(0, Number(m[3] || '0')))
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), hh, mm, ss)
}

function parseISODateToTs(iso) {
  const d = parseAnyDateToUTC(iso)
  return d ? d.getTime() : null
}

function formatTsToISO(ts) {
  if (!ts && ts !== 0) return ''
  const d = new Date(ts)
  const y = d.getUTCFullYear()
  const m = String(d.getUTCMonth() + 1).padStart(2, '0')
  const day = String(d.getUTCDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

export async function readReservasFromSheets() {
  const sheetId = process.env.GOOGLE_SHEETS_ID
  // Default: o usuário organiza as consultas na aba "Agenda_Patusco".
  // Observação: quem já define GOOGLE_SHEETS_RANGE no Railway continuará prevalecendo.
  const range = process.env.GOOGLE_SHEETS_RANGE || 'Agenda_Patusco!A1:J'
  if (!sheetId) throw new Error('GOOGLE_SHEETS_ID ausente')

  const creds = loadSheetsCreds()
  const auth = new google.auth.JWT({
    email: creds.client_email,
    key: creds.private_key,
    scopes: ['https://www.googleapis.com/auth/spreadsheets.readonly'],
  })

  const sheets = google.sheets({ version: 'v4', auth })
  const resp = await sheets.spreadsheets.values.get({ spreadsheetId: sheetId, range })
  const values = resp.data.values || []
  if (values.length < 2) return []

  const headers = values[0]
  const keyByIndex = headers.map(guessKey)

  const out = []
  for (let i = 1; i < values.length; i++) {
    const row = values[i]
    const obj = { nome:'', ddi:'', telefone:'', checkin:'', checkout:'', flat:'', valor:'', qnt:'', realizada:'', hora:'' }

    for (let j = 0; j < keyByIndex.length; j++) {
      const key = keyByIndex[j]
      if (!key) continue
      const cell = row[j] ?? ''
      obj[key] = String(cell).trim()
    }

    // Normalização de datas
    obj.checkin = formatDateString(obj.checkin)
    obj.checkout = formatDateString(obj.checkout)

    // Ignora linha vazia (sem nome indiquendo que é válida)
    if (!obj.nome && !obj.telefone && !obj.flat) continue

    out.push(obj)
  }
  return out
}

function digitsOnly(s) {
  return String(s || '').replace(/\D+/g, '')
}

function buildGroupKey(r) {
  // A “consulta” é identificada pelos campos que se repetem entre linhas.
  // Pelo seu padrão: Nome + Realizada + Hora + Chk-In/Chk-out + Qnt.
  // Incluímos telefone/ddi para evitar colisões quando nomes repetem.
  const parts = [
    r.nome || '',
    r.realizada || '',
    r.hora || '',
    r.checkin || '',
    r.checkout || '',
    r.qnt || '',
    digitsOnly(r.ddi || ''),
    digitsOnly(r.telefone || ''),
  ]
  return parts.map(p => String(p).trim()).join('|')
}

function stableIdFromKey(key) {
  return crypto.createHash('sha1').update(String(key)).digest('hex').slice(0, 16)
}

export function groupReservasByConsulta(rows) {
  const byKey = new Map()

  for (const r of rows || []) {
    const key = buildGroupKey(r)
    if (!key.replace(/\|/g, '').trim()) continue

    const consultaTs = parseConsultaDateTimeToTs(r.realizada, r.hora)
    const checkinTs = parseISODateToTs(r.checkin)

    let g = byKey.get(key)
    if (!g) {
      g = {
        id: stableIdFromKey(key),
        nome: r.nome || '',
        ddi: r.ddi || '',
        telefone: r.telefone || '',
        realizada: r.realizada || '',
        hora: r.hora || '',
        consultaTs: consultaTs,
        checkinTs: checkinTs,
        checkin: r.checkin || '',
        checkout: r.checkout || '',
        qnt: r.qnt || '',
        apartamentos: [],
      }
      byKey.set(key, g)
    }

    const flat = (r.flat || '').trim()
    const valor = (r.valor || '').trim()
    if (flat || valor) {
      const exists = g.apartamentos.some(a => a.flat === flat && a.valor === valor)
      if (!exists) g.apartamentos.push({ flat, valor })
    }
  }

  // Ordenação default: consultas mais novas -> mais antigas
  const out = Array.from(byKey.values())
  out.sort((a, b) => {
    const ca = (typeof a.consultaTs === 'number' ? a.consultaTs : -1)
    const cb = (typeof b.consultaTs === 'number' ? b.consultaTs : -1)
    if (ca !== cb) return cb - ca

    const ia = (typeof a.checkinTs === 'number' ? a.checkinTs : -1)
    const ib = (typeof b.checkinTs === 'number' ? b.checkinTs : -1)
    if (ia !== ib) return ib - ia

    return String(a.nome || '').localeCompare(String(b.nome || ''))
  })
  return out
}

export async function readConsultasFromSheets() {
  const rows = await readReservasFromSheets()
  return groupReservasByConsulta(rows)
}
