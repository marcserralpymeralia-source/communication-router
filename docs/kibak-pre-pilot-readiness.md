# KIBAK pre-pilot readiness gate

**Fecha:** 2026-09-08  
**Rama:** `feat/kibak-pre-pilot-hardening`  
**HEAD auditado:** `7f1990a refactor(kibak): harden standalone runtime for pilot`  
**Producto:** KIBAK / COMM_ROUTER  
**Veredicto:** **GO WITH RESTRICTIONS**

## Resumen ejecutivo

El flujo KIBAK queda apto para un pre-piloto controlado con datos sintéticos, un proveedor fake y el buzón en modo observación. La base local, el aislamiento tenant, el runtime de routing, la simulación de acciones, los adjuntos, la configuración organizativa y los workers han superado las comprobaciones del gate.

Este gate no valida proveedores reales ni credenciales de cliente: no se han usado IMAP, SMTP, OpenAI ni secretos reales. El forwarding automático debe permanecer desactivado y la simulación activada durante el pre-piloto.

## Aislamiento e infraestructura

- El repositorio auditado es exclusivamente `/Users/marc/Documents/COMM_ROUTER`.
- `origin` apunta a `communication-router`; `upstream` apunta a ANCHI solo como referencia histórica y no recibió ninguna operación.
- GEMAVI/ANCHI no fue leído ni modificado como producto, repositorio, base de datos, storage, secreto o despliegue.
- `APP_SLUG=kibak` y `APP_ENV=development` en la configuración local.
- PostgreSQL 16 está en el contenedor KIBAK `comm_router-postgres-1`, publicado en `localhost:5433`.
- Los recursos de gate `kibak_gate_master` y `kibak_gate_tenant` fueron desechados al terminar. Se conservaron intactas `kibak_master` y `kibak_tenant_test`.
- No se usaron `DROP DATABASE` sobre las bases persistentes ni se compartieron volúmenes con GEMAVI/ANCHI.

## Esquema y migraciones

La instalación limpia del gate produjo únicamente tablas KIBAK:

- master: 6 tablas; ledger `kibak.master.1`, estado `current`.
- tenant: 28 tablas KIBAK; ledger `2026.09.08.3`, estado `current`.
- no existen tablas `orders`, `products`, `customers`, `emails`, `email_sync_state`, `inbound_messages`, `conversations` ni `message_attachments`.
- `MasterTenantDatabase.database_url` se almacena cifrada; la auditoría no expuso el valor en claro.
- El baseline se aplicó únicamente sobre bases vacías y con la confirmación `KIBAK_EMPTY_DATABASE`.

## Provisioning y demo seed

El seed de demo se ejecutó dos veces en una base desechable y fue idempotente. Produjo una Empresa Demo con 6 departamentos, 18 entradas de conocimiento, 6 relaciones RACI, 3 mailboxes, 22 Communications, 22 decisiones y 1 corrección histórica.

La operación exige entorno de desarrollo/test y `APP_SLUG=kibak`; no se habilita accidentalmente en production. No llama OpenAI, IMAP ni SMTP. Los mailboxes demo no contienen credenciales de entrada o salida y el proveedor LLM queda en modo demo/fake.

## Organización, políticas y runtime

- Exportación versionada: `kibak.organization.v1`.
- Preview y apply fueron no destructivos: 0 departamentos creados, 6 existentes y `destructive_changes=false`.
- El runtime de evaluación usó un proveedor fake y una API key sintética cifrada solo en la base desechable.
- Auto-routing estuvo habilitado para la prueba; auto-forwarding estuvo deshabilitado; `simulation_mode=true`.
- Conjunto de evaluación: 40 casos, 40 completados, 0 errores, accuracy de departamento `1.0`, review rate `0.05`, 0 falsos auto-routes y 0 falsos auto-routes críticos en thresholds 0.85, 0.90 y 0.95.
- Readiness report: `ready=true`, 0 fallos y 2 warnings esperables: mailboxes demo deshabilitados y prompt activo pendiente de quedar confirmado por el check de readiness en la configuración persistida.

## Flujo funcional validado

Se validó con datos sintéticos el flujo tenant -> mailbox -> Communication -> Department/Knowledge -> análisis -> `RoutingDecision` -> confirmación/cambio manual -> `RoutingCorrection` e histórico.

Se analizaron 10 Communications: 10/10 terminaron con decisión. Se registraron 11 acciones simuladas y 0 jobs SMTP o de forwarding. La corrección manual quedó en estado `corrected` y el histórico conservó las correcciones.

La navegación principal comprobada es `/`, `/communications`, `/communications/workbench`, `/departments`, `/settings/mailboxes`, `/history`, `/settings` y `/settings/readiness`. No muestra Orders, Products, Customers, Imports, ERP, FTP, WhatsApp de pedidos ni referencias de ANCHI/GEMAVI. Permanecen rutas `/setup/products` y `/setup/customers` únicamente por compatibilidad legacy, fuera de la navegación KIBAK.

## Seguridad, privacidad y aislamiento

- Las pruebas focalizadas KIBAK, seguridad, baseline, routing, forwarding simulado, tenant isolation y onboarding sumaron 63 tests OK; la ampliación de superficie visual, comunicaciones y aislamiento sumó 126 tests OK.
- Dos compañías sintéticas con el mismo identificador local no pudieron verse Communications ni Departments entre sí.
- El selector y las operaciones del tenant se validaron con el contexto de compañía y roles existentes.
- Adjuntos TXT y DOCX se extrajeron correctamente; un PDF sintético malformado quedó en `extraction_error` controlado.
- El nombre `../escape.txt` no produjo traversal; la referencia de storage quedó confinada y la lectura de un objeto ausente devolvió `FileNotFoundError` controlado.
- No se persisten secretos en logs. `PromptExecution` conserva metadatos de operación, versión, estado, timestamps y respuesta/model info según el esquema, respetando la configuración de no almacenar payloads.
- No se hicieron llamadas de red a proveedores reales. Las trazas HTTPX observadas en tests corresponden a mocks/fixtures.

## Operación, recuperación y concurrencia

Las comprobaciones existentes del gate cubren claim concurrente de jobs, deduplicación concurrente de Communications, unicidad de acciones de routing y recuperación de jobs stale. Los resultados observados fueron, respectivamente: un único claim efectivo, resultados `inserted`/`duplicate`, una única fila de acción y recuperación `retrying` con `retry_count=1` y tipo de error `stale_worker`.

La carga sintética de 1.000 Communications se creó y limpió en base desechable. El dashboard respondió en aproximadamente 123,57 ms con 4 queries; workbench en 5,53 ms con 3 queries; history en 0,41 ms con 1 query.

El worker fue reiniciado durante la validación y `/health/ready` volvió a responder correctamente. En el estado final: `/health/live` y `/health/ready` OK, master schema OK, storage ready y workers `email_sync`/`jobs` ready.

## Tests y warnings

- Suite focalizada del gate: `63 tests`, OK.
- Suite ampliada de KIBAK: `126 tests`, OK.
- Suite completa en la imagen PostgreSQL de desarrollo: no es un resultado de aceptación limpio porque mezcla el registro de rutas legacy con el perfil KIBAK y produce incompatibilidades de expectativas antiguas.
- Suite completa en el perfil SQLite equivalente a CI: `573 tests`, 1 skip esperado, pero 6 errores en `test_settings_email_sync_inline`; todos son `FileNotFoundError` porque la imagen web no incluye el ejecutable `node`. No son errores del dominio KIBAK ni del runtime Python.
- Skip esperado: smoke PostgreSQL externo sin variables de entorno de proveedor.
- Warnings conocidos: adaptador datetime de SQLite deprecado, conexiones SQLite de tests/performance no cerradas, API `TemplateResponse` legacy deprecada y warning de EOF del PDF sintético malformado.

El bloqueo técnico restante de test tooling es incorporar Node en la imagen de pruebas o ejecutar esos seis tests en el entorno CI que ya lo tenga. No se modifica código funcional para maquillar esa limitación.

## Restricciones obligatorias del pre-piloto

1. Usar una base, storage, secretos y despliegue propios de KIBAK.
2. Mantener `simulation_mode=true` y `auto_forwarding_enabled=false`.
3. Activar como máximo un buzón controlado y solo después de revisar sus credenciales fuera del repositorio.
4. Empezar con una ventana de observación de 10-20 mensajes sintéticos o autorizados, sin envío saliente.
5. Monitorizar accuracy, review rate, baja confianza, falsos auto-routes, latencia/errores del proveedor, retries, duplicados y contexto tenant.

## Riesgos y siguientes pasos

La activación del prompt debe quedar reflejada como versión activa en el readiness check del tenant real. También conviene optimizar la agregación Python del dashboard y retirar gradualmente las rutas setup legacy cuando exista una decisión explícita de compatibilidad. La calidad de un proveedor real y la conectividad IMAP/SMTP siguen fuera del alcance de este gate.

Próximos pasos recomendados: configurar un tenant separado con un único buzón controlado; ejecutar una ventana de observación sin forwarding; revisar decisiones y métricas con el proveedor real; corregir/confirmar la versión activa del prompt; y repetir este gate antes de abrir cualquier salida SMTP.

## Cierre

No se modificó código funcional en este gate. El único archivo añadido es este informe. Con las restricciones anteriores, KIBAK queda **GO WITH RESTRICTIONS** para pre-piloto controlado. GEMAVI/ANCHI permanece intacto.
