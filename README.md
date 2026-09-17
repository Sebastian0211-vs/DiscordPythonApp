# remotePython

A Discord bot for your VPS. Post Python in a private channel and it runs in a
throwaway, locked-down Docker container. The output (and any files the script
saves, like plots) gets posted back as a reply.

````
you:  ```python
      import numpy as np
      print(np.linalg.eigvals([[2, 0], [0, 3]]))
      ```
bot:  ✅ exit 0 · 0.41s
      [2. 3.]
````

## How it works

```
Discord channel ──> bot (container on VPS) ──stdin──> sandbox container ──JSON──> bot ──> reply
                          │                           (no network, read-only,
                          └── /var/run/docker.sock     non-root, limits, timeout)
```

- **Triggers:** a `.py` attachment, or a message with a ` ```python ` / ` ```py ` code block.
- **Who:** only in `ALLOWED_CHANNEL_IDS` (and threads inside them), only for
  `ALLOWED_USER_IDS` / `ALLOWED_ROLE_IDS`. Others get a 🚫 reaction.
- **Output:** stdout and stderr merged. Long output shows its tail inline and the
  full log as `output.txt`. Files written in the working directory
  (`plt.savefig("plot.png")`, `df.to_csv("out.csv")`) are attached (max 9 files, 8 MB).
- **Libraries:** numpy, pandas, scipy, matplotlib, seaborn, sympy, pillow,
  scikit-learn, tabulate. Edit `sandbox/requirements.txt` and rebuild to change.
  Scripts cannot `pip install` at runtime (no network).
- **Reactions:** ⏳ running, ✅ exit 0, ❌ non-zero exit, ⏱️ timeout, 💥 sandbox killed (e.g. out of memory).

### Sandbox restrictions (every run)

| Restriction | Setting |
|---|---|
| Network | `--network none` |
| Filesystem | read-only root, 128 MB `noexec` tmpfs on `/tmp`, nothing from the host mounted |
| User | uid 10001, `--cap-drop ALL`, `no-new-privileges` |
| Resources | 512 MB RAM (no swap), 1 CPU, 128 processes, 256 open files |
| Time | 30 s, then the whole process group is killed; container is always removed |
| Secrets | none of the bot's environment is passed in, so scripts never see the token |

All limits are configurable in `.env`.

## 1. Create the Discord bot

1. <https://discord.com/developers/applications> > **New Application**.
2. **Bot** tab: **Reset Token**, copy it. Turn on **Message Content Intent**. Turn off **Public Bot**.
3. Invite it (replace `APP_ID` with the Application ID from **General Information**):
   `https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot&permissions=274878008384`
   (View Channels, Send Messages, Send Messages in Threads, Attach Files, Add Reactions, Read Message History).
4. In Discord, enable **Settings > Advanced > Developer Mode**, then right-click the
   private channel and yourself / your friends > **Copy ID**.

## 2. Set up the Ubuntu VPS

Install Docker if it isn't there yet:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # log out and back in afterwards
```

Copy the project over from your laptop (works in fish too):

```
rsync -av --exclude .venv --exclude .git --exclude .idea ./ vps:~/remotePython/
```

On the VPS:

```bash
cd ~/remotePython
docker build -t remotepy-sandbox:latest sandbox/   # the image scripts run in
cp .env.example .env && nano .env                  # token, channel IDs, user IDs
docker compose up -d --build                       # start the bot
docker compose logs -f bot                         # should say "Logged in as ..."
```

Test the sandbox without Discord:

```bash
echo 'print("hello from the sandbox")' | docker compose run --rm -T --entrypoint remotepy-run bot
```

### Updating

```bash
docker build -t remotepy-sandbox:latest sandbox/   # only if sandbox/ changed
docker compose up -d --build
```

## 3. Strongly recommended: gVisor

With the default runtime, sandbox containers share the VPS kernel, so a kernel
exploit could escape. [gVisor](https://gvisor.dev) puts a user-space kernel in
between and is the standard fix for running untrusted code:

```bash
sudo apt-get update && sudo apt-get install -y apt-transport-https ca-certificates curl gnupg
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  | sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null
sudo apt-get update && sudo apt-get install -y runsc
sudo runsc install
sudo systemctl reload docker
docker run --rm --runtime=runsc hello-world         # check it works
```

Then set `SANDBOX_RUNTIME=runsc` in `.env` and `docker compose up -d`.

## Security notes

- **The Docker socket is powerful.** The bot container can start any container,
  which is root-equivalent on the VPS. The bot never runs user code itself, only
  passes it on stdin to the sandbox, but keep the bot token secret (reset it in
  the Developer Portal if it leaks) and keep the allowlist tight.
- **Allowlist = trust.** Anyone on the list can use 1 CPU for 30 s per message,
  `MAX_CONCURRENT` at a time. Don't add people you wouldn't give a shell to.
- Script output is posted with all mentions disabled, so `print("@everyone")` pings nobody.
- Every run is logged with the author, channel and SHA-256 of the code (`docker compose logs bot`).

## Development

```
uv sync
uv run pytest            # unit tests; sandbox tests run too if Docker + the image are available
```

`tests/test_sandbox.py` includes hostile scripts (network access, writing to
system paths, fork bomb, 1 GB allocation, infinite output, infinite loop) that
must all be contained.

## Layout

```
sandbox/Dockerfile        image the scripts run in
sandbox/runner.py         runs inside it: executes the script, returns JSON with output + files
src/remotepy/sandbox.py   starts sandbox containers with all restrictions (also the remotepy-run CLI)
src/remotepy/bot.py       Discord bot
src/remotepy/messages.py  code extraction and reply formatting
src/remotepy/config.py    settings from environment variables
compose.yaml, Dockerfile  bot deployment
```
