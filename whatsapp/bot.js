const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = require('@whiskeysockets/baileys')
const { Boom } = require('@hapi/boom')
const axios = require('axios')
const qrcode = require('qrcode-terminal')
const pino = require('pino')

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8001'
const PR_REGEX = /https:\/\/github\.com\/[\w.\-]+\/[\w.\-]+\/pull\/\d+/

const logger = pino({ level: 'info' })

async function sendInChunks(sock, jid, text) {
    const MAX = 3800
    if (text.length <= MAX) {
        await sock.sendMessage(jid, { text })
        return
    }
    const chunks = []
    let remaining = text
    while (remaining.length > 0) {
        let chunk = remaining.slice(0, MAX)
        const lastNewline = chunk.lastIndexOf('\n')
        if (lastNewline > MAX * 0.7) chunk = chunk.slice(0, lastNewline)
        chunks.push(chunk)
        remaining = remaining.slice(chunk.length)
    }
    for (let i = 0; i < chunks.length; i++) {
        const header = chunks.length > 1 ? `*[Part ${i+1}/${chunks.length}]*\n` : ''
        await sock.sendMessage(jid, { text: header + chunks[i] })
        await new Promise(r => setTimeout(r, 400))
    }
}

async function handleCommand(sock, jid, text) {
    const cmd = text.trim().toLowerCase()

    if (cmd === '!help') {
        await sock.sendMessage(jid, { text: `*PR Review Bot Commands*\n\n• Send a GitHub PR URL to get a review\n• !status — check system health\n• !index owner/repo — trigger repo indexing\n• !help — show this message` })
        return true
    }

    if (cmd === '!status') {
        try {
            const { data } = await axios.get(`${BACKEND_URL}/health`, { timeout: 10000 })
            const indexed = data.indexed_repos.map(r => `  • ${r.repo} (${r.file_count} files, SHA: ${r.sha.slice(0,8)})`).join('\n') || '  (none)'
            const msg = `*System Status*\n\n🤖 vLLM: ${data.vllm === 'up' ? '✅' : '❌'}\n🔢 Ollama: ${data.ollama === 'up' ? '✅' : '❌'}\n\n*Indexed Repos:*\n${indexed}`
            await sock.sendMessage(jid, { text: msg })
        } catch(e) {
            await sock.sendMessage(jid, { text: `❌ Could not reach backend: ${e.message}` })
        }
        return true
    }

    if (cmd.startsWith('!index ')) {
        const repo = text.slice(7).trim()
        try {
            const { data } = await axios.post(`${BACKEND_URL}/index`, { repo_full_name: repo }, { timeout: 10000 })
            await sock.sendMessage(jid, { text: `✅ Indexing started for *${repo}*. Use !status to check progress.` })
        } catch(e) {
            const msg = e.response?.data?.detail || e.message
            await sock.sendMessage(jid, { text: `❌ Index failed: ${msg}` })
        }
        return true
    }

    return false
}

async function startBot() {
    const { state, saveCreds } = await useMultiFileAuthState('./auth')
    const { version } = await fetchLatestBaileysVersion()

    const sock = makeWASocket({
        version,
        auth: state,
        logger: pino({ level: 'silent' }),
        printQRInTerminal: false,
        defaultQueryTimeoutMs: undefined,
        getMessage: async (key) => {
            return { conversation: '' }
        }
    })

    sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
        if (qr) {
            console.log('\n📱 Scan this QR code in WhatsApp:\n')
            qrcode.generate(qr, { small: true })
        }
        if (connection === 'close') {
            const shouldReconnect = (lastDisconnect?.error instanceof Boom)
                ? lastDisconnect.error.output?.statusCode !== DisconnectReason.loggedOut
                : true
            console.log('Connection closed. Reconnecting:', shouldReconnect)
            if (shouldReconnect) {
                setTimeout(startBot, 3000)
            }
        } else if (connection === 'open') {
            console.log('✅ WhatsApp connected! Send yourself a GitHub PR URL to get a review.')
        }
    })

    sock.ev.on('creds.update', saveCreds)

    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return

        for (const msg of messages) {
            if (!msg.key.fromMe) continue  // ONLY respond to my own messages

            const text = msg.message?.conversation
                || msg.message?.extendedTextMessage?.text
                || ''

            if (!text) continue

            const jid = msg.key.remoteJid

            // Handle commands
            if (text.startsWith('!')) {
                await handleCommand(sock, jid, text)
                continue
            }

            // Handle PR URL
            const prMatch = text.match(PR_REGEX)
            if (!prMatch) continue

            const prUrl = prMatch[0]
            console.log(`\n📋 PR review requested: ${prUrl}`)

            try {
                await sock.sendMessage(jid, {
                    text: `🔍 *PR Review Started*\n\nFetching PR and searching codebase for similar files...\nThis takes ~45-60 seconds ⏳`
                })
            } catch(e) {
                console.error(`❌ sendMessage failed (ack):`, e.message)
                continue
            }

            const startTime = Date.now()
            try {
                const { data } = await axios.post(
                    `${BACKEND_URL}/review`,
                    { pr_url: prUrl },
                    { timeout: 240000 }
                )

                const elapsed = ((Date.now() - startTime) / 1000).toFixed(1)
                const header = `✅ *Review Complete* (${elapsed}s)\n📁 Files: ${data.files_reviewed.length} | 🔎 Similar files found: ${data.similar_files_found}\n\n---\n\n`

                await sendInChunks(sock, jid, header + data.review)
                console.log(`✅ Review sent for ${prUrl} in ${elapsed}s`)

            } catch(e) {
                const errMsg = e.response?.data?.detail || e.message
                console.error(`❌ Review failed for ${prUrl}:`, errMsg)
                try {
                    await sock.sendMessage(jid, { text: `❌ *Review Failed*\n${errMsg}` })
                } catch(e2) {
                    console.error(`❌ sendMessage failed (error reply):`, e2.message)
                }
            }
        }
    })
}

startBot()
