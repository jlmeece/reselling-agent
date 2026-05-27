# VPS Deployment — Reselling Agent + Hermes Gateway

## Overview

Hermes Agent runs as the orchestration layer on Hostinger VPS. The reselling agent's modes are exposed as Hermes skills and controlled via Telegram.

## Prerequisites

- Hostinger VPS (Ubuntu)
- Docker + Docker Compose installed on VPS
- Telegram bot token (create via @BotFather)
- All credentials from `.env.template` ready to fill in

## Setup Steps

1. **SSH into VPS**
   ```bash
   ssh user@your-vps-ip
   ```

2. **Clone the repo**
   ```bash
   git clone https://github.com/jlmeece/reselling-agent.git
   cd reselling-agent
   ```

3. **Create .env from template**
   ```bash
   cp .env.template .env
   nano .env   # Fill in all values
   ```

4. **Upload Google credentials**
   ```bash
   # From local machine:
   scp google_credentials.json user@your-vps-ip:~/reselling-agent/
   ```

5. **Start Hermes**
   ```bash
   docker compose -f deploy/docker-compose.yml up -d
   ```

6. **Verify Telegram connection**
   Send `/status` to your Telegram bot — should respond with last run times.

## Updating the Agent

```bash
git pull
docker compose -f deploy/docker-compose.yml restart
```

## Logs

```bash
docker compose -f deploy/docker-compose.yml logs -f
```
