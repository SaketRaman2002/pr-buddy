const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = require('@whiskeysockets/baileys')
const { Boom } = require('@hapi/boom')
const axios = require('axios')
const qrcode = require('qrcode-terminal')
const pino = require('pino')

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8001'
const PR_REGEX = /https:\/\/github\.com\/[\w.\-]+\/[\w.\-]+\/pull\/\d+/

const logger = pino({ level: 'info' })

async function handleCommand(sock, jid, text) {
    const cmd = text.trim().toLowerCase()

    if (cmd === '!help') {
        await sock.sendMessage(jid, { text: `*PR Review Bot Commands*\n\n• Send a GitHub PR URL — review posted as draft on the PR (open the PR to submit)\n• !status — check system health\n• !index owner/repo — trigger repo indexing\n• !sync owner/repo — sync team reviews for context learning\n• !feedback owner/repo — index submitted reviews as learning examples\n• !help — show this message` })
        return true
    }

    if (cmd === '!status') {
        try {
            const { data } = await axios.get(`${BACKEND_URL}/health`, { timeout: 10000 })
            const indexed = data.indexed_repos.map(r => `  • ${r.repo} (${r.file_count} files, SHA: ${r.sha.slice(0,8)})`).join('\n') || '  (none)'
            const team = data.team_members?.length ? data.team_members.join(', ') : '(none configured)'
            const msg = `*System Status*\n\nvLLM: ${data.vllm === 'up' ? '✅' : '❌'}\nOllama: ${data.ollama === 'up' ? '✅' : '❌'}\n\n*Indexed Repos:*\n${indexed}\n\n*Team Members:* ${team}`
            await sock.sendMessage(jid, { text: msg })
        } catch(e) {
            await sock.sendMessage(jid, { text: `❌ Could not reach backend: ${e.message}` })
        }
        return true
    }

    if (cmd.startsWith('!index ')) {
        const repo = text.slice(7).trim()
        try {
            await axios.post(`${BACKEND_URL}/index`, { repo_full_name: repo }, { timeout: 10000 })
            await sock.sendMessage(jid, { text: `✅ Indexing started for *${repo}*. Use !status to check progress.` })
        } catch(e) {
            const msg = e.response?.data?.detail || e.message
            await sock.sendMessage(jid, { text: `❌ Index failed: ${msg}` })
        }
        return true
    }

    if (cmd.startsWith('!sync ')) {
        const repo = text.slice(6).trim()
        try {
            await axios.post(`${BACKEND_URL}/sync-team-reviews`, { repo_full_name: repo }, { timeout: 10000 })
            await sock.sendMessage(jid, { text: `✅ Team review sync started for *${repo}*. Reviews will be used as context in future reviews.` })
        } catch(e) {
            const msg = e.response?.data?.detail || e.message
            await sock.sendMessage(jid, { text: `❌ Sync failed: ${msg}` })
        }
        return true
    }

    if (cmd.startsWith('!feedback ')) {
        const repo = text.slice(10).trim()
        try {
            // min_age_hours: 0 — user is explicitly asking, bypass the age guard
            const { data } = await axios.post(
                `${BACKEND_URL}/feedback/collect`,
                { repo_full_name: repo, min_age_hours: 0 },
                { timeout: 30000 }
            )
            const n = data.new_examples_indexed
            const msg = n > 0
                ? `✅ *${n}* submitted review(s) indexed as learning examples for *${repo}*`
                : `ℹ️ No new submitted reviews found for *${repo}* (drafts may still be pending or already indexed)`
            await sock.sendMessage(jid, { text: msg })
        } catch(e) {
            const msg = e.response?.data?.detail || e.message
            await sock.sendMessage(jid, { text: `❌ Feedback collection failed: ${msg}` })
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
            console.log('✅ WhatsApp connected! Send yourself a GitHub PR URL to post a draft review.')
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
                    text: `🔍 *PR Review Started*\n\nFetching PR, building context, generating review...\nThis takes ~45-60 seconds ⏳`
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
                const teamCtx = data.team_context_used ? '✅ team style context applied' : '⚠️ no team context yet (run !sync)'

                const summary = [
                    `✅ *Draft Review Posted* (${elapsed}s)`,
                    ``,
                    `📋 PR: ${prUrl}`,
                    `📁 Files reviewed: ${data.files_reviewed.length}`,
                    `🔎 Similar files found: ${data.similar_files_found}`,
                    `👥 Team context: ${teamCtx}`,
                    ``,
                    `➡️ Open the PR on GitHub to review the draft and submit when ready.`,
                ].join('\n')

                await sock.sendMessage(jid, { text: summary })
                console.log(`✅ Draft review posted for ${prUrl} in ${elapsed}s`)

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
