# remotePython

A Discord app you install on **your account** (not on a server) that runs Python
in a throwaway, locked-down Docker container on your VPS. It works in private
group chats and DMs, and the output (plus any files the script saves, like plots)
is posted in the chat for everyone there to see.

## Using it

| Where | What |
|---|---|
| `/run` | opens a box to paste code (code fences are fine), then posts the result |
| `/run file:script.py` | runs an attached `.py` file |
| `/run data1:data.csv` | adds a data file (up to `data1`, `data2`, `data3`), with either of the above |
| Right-click a message > **Apps** > **Run Python** | runs the ` ```python ` block or `.py` file in that message; its other attachments become data files |
| `/ids` | shows your user ID and the current chat's ID, only to you |

A user-installed app can't read chat messages, so it never runs code on its own:
somebody on the allowlist has to trigger it with one of the above.

- **Who can run code:** only `ALLOWED_USER_IDS`, optionally only in `ALLOWED_CHANNEL_IDS`.
  Anyone else gets a private "not allowed" message.
- **Data files:** placed next to the script, so open them by name
  (`pd.read_csv("data.csv")`). Extra `.py` files can be imported (`import helper`).
  8 MB total per run.
- **Output:** stdout and stderr merged. Long output shows its tail inline and the
  full log as `output.txt`. Files the script creates or changes in its folder are attached
  (max 8 files, 8 MB), and `plt.show()` saves each figure as `figure_1.png`, `figure_2.png`, ...
  Code pasted or uploaded through `/run` is attached too, so the chat can see what ran.
- **Libraries:** numpy, pandas, scipy, matplotlib, seaborn, sympy, pillow,
  scikit-learn, tabulate, ipython. Edit `sandbox/requirements.txt` and rebuild to change.
  Scripts cannot `pip install` at runtime (no network).

## How it works

```
/run or "Run Python" ──> bot (container on VPS) ──stdin──> sandbox container ──JSON──> bot ──> message in the chat
                               │                           (no network, read-only,
                               └── /var/run/docker.sock     non-root, limits, timeout)
```

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

## 1. Create the Discord app

1. <https://discord.com/developers/applications> > **New Application**.
2. **Installation** tab:
   - **Installation Contexts:** tick **User Install**, untick **Guild Install**.
   - **Install Link:** **None**, then save.
3. **Bot** tab: **Reset Token** and copy it. Turn off **Public Bot**. No privileged
   intents are needed.
4. Install it on your account. Open this link, replacing `APP_ID` with the
   Application ID from **General Information**, and choose **Add to my apps**:
   `https://discord.com/oauth2/authorize?client_id=APP_ID&integration_type=1&scope=applications.commands`
5. Get your user ID: **Settings > Advanced > Developer Mode** on, then right-click
   your name > **Copy User ID**.

Friends in the group chat see every result without installing anything. If a friend
should also be able to *run* code, they open the same link and you add their ID to
`ALLOWED_USER_IDS`. If Discord refuses the install for them, turn **Public Bot** on:
anyone could then install the app, but only allowlisted IDs can run code.

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
cp .env.example .env && nano .env                  # token and your user ID
docker compose up -d --build                       # start the bot
docker compose logs -f bot                         # should say "Synced 3 global commands"
```

The commands can take a minute to show up in Discord the first time. To lock it to
one group chat, run `/ids` there, put the chat ID in `ALLOWED_CHANNEL_IDS` and run
`docker compose up -d` again.

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
sandbox/runner.py         runs inside it: executes the script with its data files, returns JSON with output + files
sandbox/mpl_autosave.py   matplotlib backend that turns plt.show() into saved PNGs
src/remotepy/sandbox.py   starts sandbox containers with all restrictions (also the remotepy-run CLI)
src/remotepy/bot.py       Discord app: /run, /ids and the "Run Python" message command
src/remotepy/messages.py  code extraction and reply formatting
src/remotepy/config.py    settings from environment variables
compose.yaml, Dockerfile  bot deployment
```
