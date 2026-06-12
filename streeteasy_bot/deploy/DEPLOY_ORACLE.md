# Run the bot 24/7 free on an Oracle Cloud VM

This runs the bot around the clock so it emails you the moment a new listing
appears -- even when your laptop is off. Uses HTTP fetch mode (no browser), so the
VM stays light and free.

Total time: ~30-45 min the first time. You only do this once.

---

## Part 1 - Create the free VM (in your browser, on Oracle's site)

1. Sign up at [https://www.oracle.com/cloud/free/](https://www.oracle.com/cloud/free/) and choose **Always Free**.
  (Oracle asks for a card to verify identity but does not charge for Always-Free
   resources. Pick a home region close to you, e.g. US East.)
2. In the console: **Menu -> Compute -> Instances -> Create instance**.
3. Settings:
  - **Image:** Canonical **Ubuntu** 22.04 (or 24.04).
  - **Shape:** an **Always Free-eligible** shape -- either `VM.Standard.A1.Flex`
  (Ampere, give it 1 OCPU / 6 GB) or `VM.Standard.E2.1.Micro`.
  - **SSH keys:** choose **Generate a key pair** and **download the private key**
  (you'll need it to connect). Save it as `~/.ssh/oracle_se_bot`.
4. Click **Create**. When it's running, copy the instance's **Public IP address**.

> The bot only makes outbound HTTPS calls (to StreetEasy + Gmail). You do NOT need
> to open any inbound ports beyond the default SSH (port 22).

---

## Part 2 - Connect to the VM

On your Mac terminal:

```bash
chmod 600 ~/.ssh/oracle_se_bot
ssh -i ~/.ssh/oracle_se_bot ubuntu@129.213.51.21
```

(Default username on Ubuntu images is `ubuntu`.)

---

## Part 3 - Put the code on the VM

Easiest is git. If your project is on GitHub:

```bash
git clone <your-repo-url> ~/NYC-Housing-Bot
```

If it's only on your laptop, copy it up instead (run this on your **Mac**, not the VM).
The excludes are important: the Mac `.venv` won't run on Linux, and the DB/caches
should be regenerated fresh on the VM.

```bash
rsync -av -e "ssh -i ~/.ssh/oracle_se_bot" \
  --exclude '.venv' \
  --exclude '.browser_profile' \
  --exclude 'debug' \
  --exclude '__pycache__' \
  --exclude '*.db' --exclude '*.db-*' \
  "/Users/user/Documents/GitHub/NYC Housing Bot/streeteasy_bot" \
  ubuntu@129.213.51.21:~/NYC-Housing-Bot/
```

This copies your `.env` too (so email works immediately on the VM). If you'd
rather not, add `--exclude '.env'` and `setup_vm.sh` will create a blank one to
fill in.

---

## Part 4 - Install + configure (on the VM)

```bash
cd ~/NYC-Housing-Bot/streeteasy_bot
bash deploy/setup_vm.sh          # installs python deps, makes .venv, creates .env
nano .env                        # fill in GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_RECIPIENTS
```

Confirm email works, then seed the baseline:

```bash
./.venv/bin/python main.py --test-email
./.venv/bin/python main.py --once -v      # seeds current listings silently
```

---

## Part 5 - Run it 24/7 with systemd

```bash
sudo cp deploy/streeteasy-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now streeteasy-bot
```

Watch it work:

```bash
journalctl -u streeteasy-bot -f
```

That's it. It now polls forever and survives reboots. To update the bot later:
`git pull` (or rsync again), then `sudo systemctl restart streeteasy-bot`.

---

## Useful commands


| Action                | Command                                 |
| --------------------- | --------------------------------------- |
| Live logs             | `journalctl -u streeteasy-bot -f`       |
| Stop                  | `sudo systemctl stop streeteasy-bot`    |
| Start                 | `sudo systemctl start streeteasy-bot`   |
| Restart after changes | `sudo systemctl restart streeteasy-bot` |
| Status                | `systemctl status streeteasy-bot`       |


## If StreetEasy ever starts blocking the VM

You'd see `Blocked`/back-off messages in the logs. Plain requests from a datacenter
IP could get challenged in the future. If that happens, either:

- move the bot to a home machine / Raspberry Pi (residential IP), or
- switch `fetch_mode: browser` (needs a residential IP + occasional manual solve).

