# JARVIS Image Gateway

> **Beta status:** deployment is intentionally deferred. Do not claim image
> generation is enabled in an installer until the live checks in
> [`CONTINUE-RU.md`](CONTINUE-RU.md) pass.

Production transport for zero-setup image generation. It is intentionally a
small standard-library Python service, suitable for Timeweb App Platform or a
1 vCPU / 1 GB Russian VPS. The Mac installer contains a revocable client token;
the upstream provider credential exists only in the server environment.

## Why it is separate

- A Base64 value in a public ZIP is **not** secret storage.
- A personal/freemium GigaChat credential may not be transferred to third
  parties. Public releases therefore require an appropriate B2B/provider
  agreement.
- Cloudflare is not a production path: image bodies are much larger than the
  throttling boundary observed for traffic from Russia.
- The 8 GB target Mac cannot run the selected quality class of FLUX reliably.

## API contract

```text
POST /v1/images/generations
Authorization: Bearer <release token>
Content-Type: application/json

{"prompt":"...", "width":1024, "height":1024}
```

Success is raw JPEG, PNG or WebP. Errors are small JSON objects. `GET /health`
contains no credentials. The service never logs prompt bodies or Authorization
headers.

## Recommended: Timeweb Cloud

Timeweb is the simplest production path because one Russian account provides
both an OpenAI-compatible image API and App Platform hosting with a technical
HTTPS domain. Create an AI Gateway key, enable an image-capable model (the
recommended starting point is `black_forest_labs/flux-2-pro`), and deploy this
public repository as a Docker Backend in App Platform:

- repository: `https://github.com/monshepiano/monshe.git`;
- staging branch: `arena/01a0113d-monshe` (switch to `main` after merge);
- project directory and build context: repository root;
- Dockerfile: `Dockerfile`; its command starts only the gateway;
- healthcheck: `/health`;
- environment: Timeweb and rate-limit values from `env.example`; omit
  `JARVIS_GATEWAY_HOST` and `JARVIS_GATEWAY_PORT` because the image binds
  publicly and automatically honours App Platform's `PORT`.

Use the generated `https://…` technical domain as
`JARVIS_IMAGE_GATEWAY_URL`. The selected model name must match the ID shown in
the account's AI Gateway model list; check it live before release. The
provider key and client token are secret App Platform variables.

## Alternative: deploy on a Russian VPS

The following is maintainer deployment, not an end-user setup step.

1. Create a DNS `A` record such as `images.example.ru` for the VPS.
2. Copy this repository's `gateway/image_gateway.py` and
   `app/jarvis/certs/russian_trusted_root_ca.pem` to their matching paths below:

   ```bash
   sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin jarvis-image
   sudo mkdir -p /opt/jarvis-image-gateway/app/jarvis/certs
   sudo cp gateway/image_gateway.py /opt/jarvis-image-gateway/
   sudo cp app/jarvis/certs/russian_trusted_root_ca.pem \
     /opt/jarvis-image-gateway/app/jarvis/certs/
   sudo chown -R root:root /opt/jarvis-image-gateway
   sudo chmod 755 /opt/jarvis-image-gateway/image_gateway.py
   ```

3. Put `env.example` at `/etc/jarvis-image-gateway.env`, replace the upstream
   key and release token values, and lock it down:

   ```bash
   sudo chown root:root /etc/jarvis-image-gateway.env
   sudo chmod 600 /etc/jarvis-image-gateway.env
   ```

4. Install and start the unit:

   ```bash
   sudo cp gateway/jarvis-image-gateway.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now jarvis-image-gateway
   curl http://127.0.0.1:8780/health
   ```

5. Install nginx and Certbot. Copy `nginx.conf.example` to an nginx site,
   replace the domain, enable it, then issue TLS:

   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d images.example.ru
   curl https://images.example.ru/health
   ```

Do not expose port 8780. Permit public ingress only to 80/443. Keep nginx's
`limit_req` in addition to process-level per-minute, per-client/day and global
caps. Provider-side quotas/budget notifications are the final boundary.

## Build the matching personal installer

Run only in the trusted release environment:

```bash
export JARVIS_IMAGE_GATEWAY_URL=https://images.example.ru
export JARVIS_IMAGE_GATEWAY_TOKEN='the same release token'
python3 install/build.py
```

`install/build.py` preserves these values from the previous updater, rotates
them when environment values are supplied, and rejects a half-configured pair.
Never set `GIGACHAT_AUTH_KEY` while building a public ZIP; that variable is only
a backward-compatible personal/dev fallback. A production build should be
considered releasable only after a live image smoke test from the target Mac.

## Rotation

Add the new token beside the old one in
`JARVIS_GATEWAY_CLIENT_TOKENS`, build and distribute the updater with the new
token, then remove the old token after the update window. Restart the service
for environment changes. Upstream access tokens are short-lived and cached
only in process memory.
