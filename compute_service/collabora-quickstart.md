# Collabora Online Admin Quickstart (`=PY()`)

Run `=PY()` in Collabora Online Calc backed by the isolated `python-compute` container. The `coolwsd` image must be built with `--enable-python-compute`; no released Collabora Online image includes it yet.

## Setup

Generate a shared Bearer secret, write `.env`, and start both services:

```bash
openssl rand -hex 32 > python_compute_api_key && chmod 600 python_compute_api_key
printf 'COOL_IMAGE=%s\nPY_COMPUTE_KEY=%s\nCOMPUTE_UID=%s\nCOMPUTE_GID=%s\n' your/coolwsd:tag "$(cat python_compute_api_key)" "$(id -u)" "$(id -g)" > .env && chmod 600 .env
docker compose up -d
```

The compute service strips one trailing newline from the key file, so the file and `.env` hold the same key.

## `compose.yaml`

```yaml
services:
  collabora:
    image: ${COOL_IMAGE}            # coolwsd built with --enable-python-compute
    ports: ["9980:9980"]
    environment:
      aliasgroup1: https://wopi.example.com:443   # your WOPI host and usual settings
      extra_params: >-
        --o:security.python_compute.enable=true
        --o:security.python_compute.url=http://python-compute:8000/v1/execute
        --o:security.python_compute.api_key=${PY_COMPUTE_KEY}
        --o:net.lok_allow.host=python-compute
    networks: [web, compute]
    depends_on:
      python-compute: {condition: service_healthy}
    restart: unless-stopped
  python-compute:
    image: ghcr.io/keithcu/writeragent-compute:latest
    user: "${COMPUTE_UID}:${COMPUTE_GID}"
    init: true
    read_only: true
    tmpfs: ["/tmp:rw,size=64m,mode=1777"]
    mem_limit: 1g
    memswap_limit: 1g
    cpus: 1
    pids_limit: 256
    security_opt: ["no-new-privileges:true"]
    cap_drop: [ALL]
    environment:
      PYTHON_COMPUTE_API_KEY_FILE: /run/secrets/python_compute_api_key
    secrets: [python_compute_api_key]
    networks: [compute]
    restart: unless-stopped
secrets:
  python_compute_api_key: {file: ./python_compute_api_key}
networks:
  web:
  compute: {internal: true}
```

- `user:` lets the container read a mode-600 key file, as `start-docker.sh` does with `--user`.
- `internal: true` gives `python-compute` no route out.
- `extra_params` puts the key in `coolwsd`'s argv; for production, set it in a mounted `coolwsd.xml`.

## `coolwsd.xml` (non-Docker installs)

```xml
<net>
  <lok_allow>
    <host desc="The Python compute service.">python-compute</host>
  </lok_allow>
</net>
<security>
  <python_compute>
    <enable type="bool">true</enable>
    <url>http://python-compute:8000/v1/execute</url>
    <api_key>same-secret-as-service</api_key>
  </python_compute>
</security>
```

- A `lok_allow` entry is a regex matched against the whole host, so escape dots (`compute\.example\.com`). Use CIDR when the URL host is an IP.
- `--o:net.lok_allow.host` replaces the first `<host>` already in the section; add a `<host>` line instead if your file already lists hosts.
- `timeout_secs` (`60`) and `max_payload_bytes` (`16777216`, 16 MiB) keep their defaults.

## Smoke check

- `docker compose ps` shows `python-compute` as `healthy`.
- Before wiring `coolwsd`, you can verify the image directly with `compute_service/smoke-docker.sh ghcr.io/keithcu/writeragent-compute:latest`.
- In a spreadsheet, `=PY("1+1")` shows `#BUSY!` and then `2`.

| Cell / coolwsd log | Fix |
|---|---|
| `#DISABLED` | `enable` is false, or the build lacks `--enable-python-compute` |
| `#VALUE!` + log `Rejected python compute request to host not in KIT allowlist` | Add the `lok_allow` host |
| `#VALUE!` + log `Python compute: HTTP 401` (log level `information` or higher) | The `api_key` differs from the service key |
| `#VALUE!` + `Network error reaching Python compute service` | Check `url`, or verify `python-compute` is up |

## Writing `=PY()` in Online: code in a cell

In a formula string literal, every `"` inside the Python code must be doubled (`=PY("x = ""a""")`), and the formula bar shows it doubled.

Instead, put the Python code in a text cell and pass that cell reference as the first argument: `=PY(A1; B1:B10)`. Calc sends `A1`'s text unchanged as `code`, and editing `A1` recalculates the formula. Use **Ctrl+Enter** to insert a new line inside the cell.

For example, put `import numpy as np` on line 1 and `np.mean(data)` on line 2 of `A1`, then enter `=PY(A1; B1:B4)` in `C1`.

Caveats:
- In an `.xlsx` document Calc drops line breaks from a referenced cell, so keep multi-line code in ODS or write it on one line with `;`.
- Start the cell with `'` or format it as Text when Calc would otherwise parse the code as a number or date (such as `1/2`).
- Single-cell `=PY()` shows only the top-left value of a grid until dynamic array spill lands; enter the formula as an array formula to display the full grid.
