#!/bin/bash
source "$HOME/.nvm/nvm.sh"
cd "$(dirname "$0")/whatsapp"
echo "Starting WhatsApp bot..."
node bot.js
