# AlexG5 live — Drone CI/CD

Pipeline watches branch **`ci-cd`** (copied from `feat/institutional-alexg-risk`).

## Flow

1. Push to `ci-cd`
2. Drone **dry-run** (Linux): `mt5service.py --dry-run --tick-once` (no MetaTrader5)
3. Drone **build/push** Docker image tags: `ci-cd`, `latest`, short SHA
4. Drone **SSH deploy**: `git pull` on the Windows host → `deploy/scripts/restart-live.ps1`

## This computer (dev)

Worktree: `borex-test` on branch `ci-cd`.

```powershell
cd c:\Users\azeva\OneDrive\Documentos\work\trading\borex-test
$env:BOREX_MAIN_ROOT = (Get-Location).Path
cd deploy\borex_live
# optional local smoke (needs deps):
python mt5service.py --dry-run --strategy alexg5 --tick-once --no-ui
```

## Deploy host (other Windows PC)

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

## Drone secrets (Vault paths)

| Secret | Path | Key |
|--------|------|-----|
| docker_repo / username / password | `borex/docker` | repo, username, password |
| deploy_host / user / ssh_key / port / path | `borex/deploy` | host, user, ssh_key, port, path |

`deploy_path` = absolute path to the git checkout on the Windows host.

## Sync strategy into ci-cd

When `feat/institutional-alexg-risk` moves ahead:

```powershell
git checkout ci-cd
git merge feat/institutional-alexg-risk
git push origin ci-cd
```
