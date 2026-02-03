@echo off
cd /d C:\code\molt
set MOLTBOOK_API_KEY=moltbook_sk_0MTYGJ3TTognw4rzO-HPo8Kz_j40BLSo

:: Start service (sound is built-in, use --no-sound to disable)
python heartbeat_full.py %*
