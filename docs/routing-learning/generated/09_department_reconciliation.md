# Department reconciliation

- Source: **SYNTHETIC_TENANT_REVIEW_FIXTURE**; tenant identifiers are intentionally omitted.
- Current tenant departments inspected: **14**.
- Department identity is `department_id`; destination email is an attribute used for the initial reconciliation only.
- Similar names and same local-parts across different domains are not automatically equivalent.
- Addresses use reserved `.test` domains and are not deliverable.

## EXISTENTES

- **Mantenimiento**: current `mantenimiento@ingesco.test`; observed `mantenimiento@ingesco.test`; active=True; match=EXACT_DESTINATION; cases=66.
- **Operaciones**: current `operaciones@ingesco.test`; observed `operaciones@ingesco.test`; active=True; match=EXACT_DESTINATION; cases=13 (`104, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 125`).
- **Central**: current `central@ingesco.test`; observed `central@ingesco.test`; active=True; match=EXACT_DESTINATION; cases=6.
- **Facturación**: current `facturacion@ingesco.test`; observed `facturacion@ingesco.test`; active=True; match=EXACT_DESTINATION; cases=6 (`13, 14, 95, 102, 107, 108`).
- **Calidad**: current `calidad@ingesco.test`; observed `calidad@ingesco.test`; active=True; match=EXACT_DESTINATION; cases=5.
- **Información**: current `informacion@ingesco.test`; observed `informacion@ingesco.test`; active=True; match=EXACT_DESTINATION; cases=5.
- **Clientes**: current `clientes@ingesco.test`; observed `clientes@ingesco.test`; active=True; match=EXACT_DESTINATION; cases=4.
- **Prevención**: current `prevencion@ingesco.test`; observed `prevencion@ingesco.test`; active=True; match=EXACT_DESTINATION; cases=4 (`118, 119, 120, 125`).
- **Comercial**: current `comercial@ingesco.test`; observed `comercial@ingesco.test`; active=True; match=EXACT_DESTINATION; cases=2.
- **Informática**: current `informatica@quibac.test`; observed `informatica@quibac.test`; active=True; match=EXACT_DESTINATION; cases=2.
- **Mantenimiento Integral**: current `mantenimientointegral@quibac.test`; observed `mantenimientointegral@quibac.test`; active=True; match=EXACT_DESTINATION; cases=2.
- **Ofitec**: current `ofitec@quibac.test`; observed `ofitec@quibac.test`; active=True; match=EXACT_DESTINATION; cases=1.
- **Ingeniería**: current `ingenieria@ingesco.test`; observed `ingenieria@ingesco.test`; active=True; match=EXACT_DESTINATION; cases=1.

## REALMENTE NUEVOS

- `inspeccion@ingesco.test`; cases=4 (`21, 22, 23, 24`); propuesta mínima: validar nombre organizativo antes de crear.
- `secretaria@ingesco.test`; cases=4 (`121, 122, 123, 124`); propuesta mínima: validar nombre organizativo antes de crear.

## AMBIGUOS

- Observado `mantenimientointegral@ingesco.test`; no hay destino exacto. Candidato por local-part: **Mantenimiento Integral** (`mantenimientointegral@quibac.test`). No fusionar dominios; cases=3 (`58, 59, 60`).

## Departamentos actuales sin destino observado

- **Técnico**; activo=True.
