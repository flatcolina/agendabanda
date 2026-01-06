import admin from 'firebase-admin'

function loadJsonEnv(name) {
  const raw = process.env[name]
  if (!raw) throw new Error(`Variável de ambiente ausente: ${name}`)
  try {
    return JSON.parse(raw)
  } catch (e) {
    throw new Error(`JSON inválido em ${name}: ${e?.message || e}`)
  }
}

let initialized = false

export function initFirebaseAdmin() {
  if (initialized) return
  const serviceAccount = loadJsonEnv('FIREBASE_SERVICE_ACCOUNT_JSON')
  admin.initializeApp({
    credential: admin.credential.cert(serviceAccount),
  })
  initialized = true
}

export function getFirestore() {
  initFirebaseAdmin()
  return admin.firestore()
}

export { admin }

export async function requireAuth(req, res, next) {
  try {
    initFirebaseAdmin()
    const authHeader = req.headers.authorization || ''
    const m = authHeader.match(/^Bearer\s+(.+)$/i)
    if (!m) return res.status(401).json({ error: 'Missing Bearer token' })

    const decoded = await admin.auth().verifyIdToken(m[1])
    req.user = decoded
    return next()
  } catch (err) {
    return res.status(401).json({ error: 'Invalid token' })
  }
}
