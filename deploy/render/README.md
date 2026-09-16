# KIBAK Pilot en Render

Este directorio describe una configuración manual y reproducible para una
cuenta Render propiedad del cliente. No contiene secretos ni crea recursos.

## Servicios

Crear dos servicios desde el mismo repositorio, rama/tag y commit:

- `kibak-pilot-web`: Web Service, runtime Docker.
- `kibak-pilot-worker`: Background Worker, runtime Docker.

Para el web usar el comando Docker:

```text
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Para el worker:

```text
python -m app.workers.jobs_worker
```

Health check del web: `/health/live`. No crear una base Render: PostgreSQL
pertenece al proyecto Neon del cliente.

## Variables

Usar un Environment Group privado compartido por web y worker y completar los
valores de `web.env.example` y `worker.env.example`. Los valores `sync: false`
deben introducirse en el dashboard de Render, nunca en Git.

`RELEASE_SHA` debe ser el SHA exacto desplegado en ambos servicios. El web lo
expone sanitizado en health y el worker lo registra al arrancar.

## Orden de despliegue

1. Crear Neon y las bases `kibak_master` y la primera base tenant.
2. Introducir secrets y variables en el Environment Group.
3. Ejecutar el comando KIBAK-only de migración como pre-deploy, con backup previo.
4. Desplegar web.
5. Comprobar `/health/live` y `/health/ready` autenticado para el tenant.
6. Desplegar worker y comprobar heartbeat.
7. Ejecutar provisioning interactivo del tenant.

El provisioning requiere `APP_ENV=staging`, `APP_SLUG=kibak`, PostgreSQL y una
base tenant ya creada. No ejecuta OpenAI, Google, IMAP ni SMTP.

## Aislamiento

Esta configuración es exclusivamente para KIBAK. No usar cuentas, proyectos,
volúmenes, secretos, DNS ni bases de GEMAVI/ANCHI. El deploy debe ser manual o
estar limitado a una rama/tag de staging protegida.
