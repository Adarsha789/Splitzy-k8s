# Splitzy — Kubernetes / AKS Deployment

Production-grade deployment for Splitzy (Django + DRF backend, React/Vite frontend,
PostgreSQL). Manifests use **Kustomize** with a shared `base/` and two overlays:

| Overlay | Purpose | DB | Secrets | TLS |
|---|---|---|---|---|
| `overlays/minikube` | Local end-to-end test | In-cluster Postgres StatefulSet | Plain Secret from `secret.env` | None (HTTP) |
| `overlays/aks-prod` | Production on AKS | Azure DB for PostgreSQL Flexible Server | Azure Key Vault (CSI driver) | cert-manager + Let's Encrypt |

```
Internet ─► Azure LB ─► NGINX Ingress (TLS) ─┬─ /          ─► frontend Service ─► frontend pods (nginx + React)
                                             └─ /api,/static,─► backend Service ─► backend pods (Django+Gunicorn)
                                                /media,/admin                         │
                                                                          migrate Job ┤ (once per release)
                                                                                      ▼
                                                                   Azure PostgreSQL + Azure Blob (media)
```

---

## Architecture summary — what runs where

**Workloads**
- `splitzy-backend` Deployment — Django/Gunicorn, 2–6 replicas (HPA), liveness `/healthz/`, readiness `/readyz/`.
- `splitzy-frontend` Deployment — nginx serving the React build, 2–4 replicas (HPA).
- `splitzy-migrate` Job — runs `manage.py migrate` once per release (not per pod).
- `splitzy-postgres` StatefulSet — **minikube only**; prod uses managed Azure Postgres.

**Config & secrets**
- `splitzy-config` ConfigMap — non-sensitive env (DB host/port/name, hosts, email host, cookie/debug flags).
- `splitzy-secrets` Secret — `DJANGO_SECRET_KEY`, `POSTGRES_USER/PASSWORD`, `EMAIL_HOST_USER/PASSWORD`, `AZURE_ACCOUNT_KEY`.
  - minikube: generated from `secret.env`.
  - prod: synced from Key Vault by the Secrets Store CSI driver (`SecretProviderClass splitzy-kv`).

**Networking**
- One Ingress (`splitzy`) is the single edge and does path routing — the frontend nginx no longer proxies `/api`.

---

## App changes already made for Kubernetes (Phase 0)

These were required for the app to behave correctly under multiple replicas / ephemeral pods:

1. `core/settings.py`: `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, CORS/CSRF now read from env (hardcoded IPs removed). Added `SECURE_PROXY_SSL_HEADER` and a `COOKIE_SECURE` toggle.
2. WhiteNoise added — backend pods serve their own static files (no shared volume).
3. Azure Blob media storage via `django-storages` (enabled when `AZURE_ACCOUNT_NAME` is set).
4. `core/health.py` + URLs: `/healthz/` (liveness), `/readyz/` (readiness, checks DB).
5. `docker/django/start` no longer runs migrations; `collectstatic` happens at image build. Migrations run via the Job.
6. Frontend API base URL defaults to same-origin `/api` (build arg `VITE_API_URL`).

---

## Prerequisites (tools)

```bash
kubectl   # v1.31+    (has built-in kustomize: `kubectl kustomize`)
minikube  # local cluster
docker    # build images
az        # Azure CLI (for the AKS phase)
helm      # for ingress-nginx + cert-manager installs
```

---

# Phase 1 — Local deploy on minikube

```bash
# 1. Start a cluster and enable the NGINX ingress addon
minikube start --cpus=4 --memory=6g --addons=ingress,metrics-server

# 2. Point your shell's docker at minikube's daemon, then build images INTO it
#    (so they resolve without a registry; tag must be :dev to match the overlay)
eval $(minikube docker-env)
docker build -f docker/django/Dockerfile  -t splitzy-backend:dev  .
docker build -f docker/nginx/Dockerfile   -t splitzy-frontend:dev .

# 3. Create your local secrets file (gitignored) and fill it in
cd k8s/overlays/minikube
cp secret.env.example secret.env
python3 -c "from django.core.management.utils import get_random_secret_key as g; print('DJANGO_SECRET_KEY='+g())"
# paste the line into secret.env, set POSTGRES_USER/PASSWORD + email creds
cd -

# 4. Deploy
kubectl apply -k k8s/overlays/minikube

# 5. Wait for Postgres, then (re)run migrations against it
kubectl -n splitzy rollout status statefulset/splitzy-postgres
kubectl -n splitzy delete job splitzy-migrate --ignore-not-found
kubectl apply -k k8s/overlays/minikube     # recreates the Job
kubectl -n splitzy wait --for=condition=complete job/splitzy-migrate --timeout=120s

# 6. Check everything is healthy
kubectl -n splitzy get pods,svc,ingress,hpa

# 7. Map the hostname to the ingress and open it
echo "$(minikube ip) splitzy.local" | sudo tee -a /etc/hosts
# create a superuser if you want the admin:
kubectl -n splitzy exec deploy/splitzy-backend -- python manage.py createsuperuser
```

Open `http://splitzy.local`. API is at `http://splitzy.local/api/...`, admin at `/admin/`.

> Note: minikube runs over plain HTTP, so the overlay sets `COOKIE_SECURE=False`.
> Production uses HTTPS and `COOKIE_SECURE=True`.

**Tear down:** `minikube delete`

---

# Phase 2 — Provision Azure

Set names once:

```bash
RG=splitzy-rg
LOC=eastus
ACR=splitzyacr$RANDOM          # must be globally unique, lowercase
AKS=splitzy-aks
PG=splitzy-pg$RANDOM           # must be globally unique
KV=splitzy-kv$RANDOM           # must be globally unique
SA=splitzymedia$RANDOM         # storage account, 3-24 lowercase alnum
DOMAIN=splitzy.example.com     # your real domain

az group create -n $RG -l $LOC
```

### 2.1 Container registry (ACR)
```bash
az acr create -g $RG -n $ACR --sku Standard
```

### 2.2 AKS cluster (with ACR attach + Key Vault CSI add-on)
```bash
az aks create -g $RG -n $AKS \
  --node-count 2 --node-vm-size Standard_B2s \
  --enable-managed-identity \
  --attach-acr $ACR \
  --enable-addons azure-keyvault-secrets-provider \
  --enable-secret-rotation \
  --generate-ssh-keys

az aks get-credentials -g $RG -n $AKS   # writes kubeconfig
```

### 2.3 Managed PostgreSQL (Flexible Server)
```bash
az postgres flexible-server create -g $RG -n $PG \
  --tier Burstable --sku-name Standard_B1ms \
  --version 15 --storage-size 32 \
  --admin-user splitzy --admin-password '<STRONG_PASSWORD>' \
  --public-access 0.0.0.0   # see note below about locking this down

az postgres flexible-server db create -g $RG -s $PG -d splitzy
az postgres flexible-server parameter set -g $RG -s $PG --name require_secure_transport --value off
```
The FQDN is `${PG}.postgres.database.azure.com` — put it in `aks-prod/configmap-patch.yaml` (`PG_HOST`).
> Lock down access for real production: use `--public-access None` + a Private Endpoint / VNet
> integration with the AKS subnet instead of `0.0.0.0`.

### 2.4 Storage account + media container (Azure Blob)
```bash
az storage account create -g $RG -n $SA -l $LOC --sku Standard_LRS
az storage container create --account-name $SA -n media --public-access blob
ACCOUNT_KEY=$(az storage account keys list -g $RG -n $SA --query '[0].value' -o tsv)
```
Put the account name (`$SA`) into `AZURE_ACCOUNT_NAME` in `configmap-patch.yaml`;
the key goes into Key Vault below.

### 2.5 Key Vault + secrets
```bash
az keyvault create -g $RG -n $KV -l $LOC --enable-rbac-authorization false

DJANGO_KEY=$(python3 -c "from django.core.management.utils import get_random_secret_key as g; print(g())")
az keyvault secret set --vault-name $KV -n django-secret-key   --value "$DJANGO_KEY"
az keyvault secret set --vault-name $KV -n postgres-user       --value "splitzy"
az keyvault secret set --vault-name $KV -n postgres-password   --value "<STRONG_PASSWORD>"
az keyvault secret set --vault-name $KV -n email-host-user     --value "you@gmail.com"
az keyvault secret set --vault-name $KV -n email-host-password --value "<GMAIL_APP_PASSWORD>"
az keyvault secret set --vault-name $KV -n azure-account-key   --value "$ACCOUNT_KEY"

# Grant the CSI driver's managed identity read access to the vault
IDENTITY_CLIENT_ID=$(az aks show -g $RG -n $AKS \
  --query addonProfiles.azureKeyvaultSecretsProvider.identity.clientId -o tsv)
az keyvault set-policy -n $KV --secret-permissions get --spn $IDENTITY_CLIENT_ID

TENANT_ID=$(az account show --query tenantId -o tsv)
echo "Fill secretproviderclass.yaml -> userAssignedIdentityID=$IDENTITY_CLIENT_ID  keyvaultName=$KV  tenantId=$TENANT_ID"
```

---

# Phase 3 — Cluster add-ons (ingress + TLS)

```bash
# NGINX Ingress Controller
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo add jetstack https://charts.jetstack.io
helm repo update

helm install ingress-nginx ingress-nginx/ingress-nginx \
  -n ingress-nginx --create-namespace

# cert-manager
helm install cert-manager jetstack/cert-manager \
  -n cert-manager --create-namespace --set crds.enabled=true
```

Create the Let's Encrypt ClusterIssuer (`letsencrypt-prod`, referenced by the ingress):

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: you@example.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            class: nginx
EOF
```

Point DNS at the ingress public IP:
```bash
kubectl -n ingress-nginx get svc ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
# create an A record:  splitzy.example.com -> <that IP>
```

---

# Phase 4 — Build, push, deploy to AKS

```bash
az acr login -n $ACR
TAG=$(git rev-parse --short HEAD)

docker build -f docker/django/Dockerfile -t $ACR.azurecr.io/splitzy-backend:$TAG .
docker build -f docker/nginx/Dockerfile  -t $ACR.azurecr.io/splitzy-frontend:$TAG \
  --build-arg VITE_API_URL=/api .
docker push $ACR.azurecr.io/splitzy-backend:$TAG
docker push $ACR.azurecr.io/splitzy-frontend:$TAG
```

Edit `overlays/aks-prod/` placeholders first:
- `kustomization.yaml`: `youracr.azurecr.io` → `$ACR.azurecr.io`, and set both `newTag` to `$TAG`
  (or `cd overlays/aks-prod && kustomize edit set image splitzy-backend=$ACR.azurecr.io/splitzy-backend:$TAG splitzy-frontend=$ACR.azurecr.io/splitzy-frontend:$TAG`).
- `configmap-patch.yaml`: `PG_HOST`, `ALLOWED_HOST`, `CLIENT_DOMAIN`, `CORS_ALLOWED_HOST`, `AZURE_ACCOUNT_NAME`.
- `ingress-patch.yaml`: replace `splitzy.example.com` with `$DOMAIN`.
- `secretproviderclass.yaml`: `<KEYVAULT_NAME>`, `<CLIENT_ID>`, `<TENANT_ID>`.

Deploy:
```bash
kubectl apply -k k8s/overlays/aks-prod
kubectl -n splitzy wait --for=condition=complete job/splitzy-migrate --timeout=180s
kubectl -n splitzy rollout status deploy/splitzy-backend
kubectl -n splitzy rollout status deploy/splitzy-frontend
kubectl -n splitzy get pods,ingress,certificate
```

Verify TLS issued (`kubectl -n splitzy get certificate` → `READY=True`), then browse `https://$DOMAIN`.

---

## Day-2 operations

```bash
# Logs
kubectl -n splitzy logs -l app.kubernetes.io/name=splitzy-backend -f

# Re-run migrations on a new release (Jobs are immutable, so delete first)
kubectl -n splitzy delete job splitzy-migrate --ignore-not-found
kubectl apply -k k8s/overlays/aks-prod

# Roll a new image
cd k8s/overlays/aks-prod
kustomize edit set image splitzy-backend=$ACR.azurecr.io/splitzy-backend:$NEW_TAG
kubectl apply -k .

# Scale manually (HPA still governs min/max)
kubectl -n splitzy scale deploy/splitzy-backend --replicas=4

# Django shell / createsuperuser
kubectl -n splitzy exec -it deploy/splitzy-backend -- python manage.py createsuperuser
```

### CI/CD (GitHub Actions sketch)
Use OIDC (`azure/login`) → `az acr login` → build & push with `:$GITHUB_SHA` →
`kustomize edit set image` → `kubectl apply -k k8s/overlays/aks-prod`. Store the
AKS/ACR names as repo variables; no long-lived credentials needed with OIDC.

---

## Known follow-ups (optional hardening)
- Run backend pods as non-root (add a `USER` to the Django Dockerfile + `runAsNonRoot`).
- NetworkPolicies to restrict pod-to-pod traffic.
- Postgres Private Endpoint instead of public access.
- Async email via Celery + Redis (a Deployment for the worker + managed Redis) —
  currently email sends are synchronous (see `docs/celery_redits.md`).
- Backups/monitoring: Azure Monitor for containers, alerting on the HPA + PDB.
```
