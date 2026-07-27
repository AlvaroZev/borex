# AlexG7 live — Drone CI/CD

Pipeline watches branch **`ci-cd`** (default strategy: **alexg7**).

## Flow

1. Push to `ci-cd`
2. Drone **dry-run** (Linux): `mt5service.py --dry-run --strategy alexg7 --tick-once` (no MetaTrader5)
3. Drone **build/push** Docker image tags: `ci-cd`, `latest`, short SHA
4. Drone **SSH deploy**: `git pull` on the Windows host → `deploy/scripts/restart-live.ps1`

## This computer (dev)

Worktree: `borex-test` on branch `ci-cd`.

```powershell
cd c:\Users\azeva\OneDrive\Documentos\work\trading\borex-test
$env:BOREX_MAIN_ROOT = (Get-Location).Path
cd deploy\borex_live
# optional local smoke (needs deps):
python mt5service.py --dry-run --strategy alexg7 --leverage 5000 --rr-factor 2.5 --min-rr 3.0 --tick-once --no-ui
```

## Deploy host (other Windows PC)

One-shot bootstrap (clone + venv + .env template):

```powershell
# copy this script onto the host, or clone first then:
cd C:\borex   # after clone
powershell -ExecutionPolicy Bypass -File deploy\scripts\bootstrap-host.ps1 -RepoDir C:\borex
```

Manual steps if you prefer:

1. Install MT5, enable Algo Trading, log into demo.
2. Clone repo, checkout `ci-cd`, set deploy path secret to that folder.
3. Copy `deploy/borex_live/.env.example` → `.env` (DATABASE_URL + MT5_*).
4. Create Python 3.11 venv and install:

```powershell
cd deploy\borex_live
py -3.11 -m venv .venv311
.\.venv311\Scripts\Activate.ps1
pip install -r requirements.txt
# also need strategy package from repo root:
pip install -r ..\..\requirements.txt
```

5. Default deploy mode is **native** (required for real MT5 IPC):

```powershell
$env:BOREX_DEPLOY_MODE = "native"   # default
.\deploy\scripts\restart-live.ps1
```

Docker mode (`BOREX_DEPLOY_MODE=docker`) pulls the CI image. Live MT5 orders need **Windows Python + local `terminal64.exe`**; Linux containers cannot import `MetaTrader5`.

Enable OpenSSH Server on the host so Drone's `appleboy/drone-ssh` step can reach it.

## Drone secrets (Vault paths)

`.drone.yml` pulls these via external secret paths. Fill them in your Drone secret store
(Vault or the Drone UI → Repo → Secrets), matching these names:

| Secret name (Drone) | Vault path | Key | Example |
|---------------------|------------|-----|---------|
| `docker_repo` | `borex/docker` | `repo` | `youruser/alexg-live` |
| `docker_username` | `borex/docker` | `username` | Docker Hub user |
| `docker_password` | `borex/docker` | `password` | Docker Hub token |
| `deploy_host` | `borex/deploy` | `host` | Windows MT5 PC hostname/IP |
| `deploy_user` | `borex/deploy` | `user` | SSH username |
| `deploy_ssh_key` | `borex/deploy` | `ssh_key` | private key PEM |
| `deploy_port` | `borex/deploy` | `port` | `22` |
| `deploy_path` | `borex/deploy` | `path` | absolute path to git checkout |

`deploy_path` = absolute path to the git checkout on the Windows host.

CLI (if `drone` + `DRONE_SERVER`/`DRONE_TOKEN` are set):

```powershell
drone secret add --repository AlvaroZev/borex --name docker_repo --data "youruser/alexg-live"
drone secret add --repository AlvaroZev/borex --name docker_username --data "youruser"
drone secret add --repository AlvaroZev/borex --name docker_password --data "ghp_or_hub_token"
drone secret add --repository AlvaroZev/borex --name deploy_host --data "192.168.x.x"
drone secret add --repository AlvaroZev/borex --name deploy_user --data "Administrator"
drone secret add --repository AlvaroZev/borex --name deploy_port --data "22"
drone secret add --repository AlvaroZev/borex --name deploy_path --data "C:\borex"
# for the key file:
Get-Content $env:USERPROFILE\.ssh\id_rsa -Raw | drone secret add --repository AlvaroZev/borex --name deploy_ssh_key --data -
```

If your Drone instance uses Vault only (not repo secrets), write the same
values under `borex/docker` and `borex/deploy` instead.

## Sync strategy into ci-cd

When `feat/institutional-alexg-risk` moves ahead:

```powershell
git checkout ci-cd
git merge feat/institutional-alexg-risk
git push origin ci-cd
```
